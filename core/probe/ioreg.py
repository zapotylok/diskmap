"""Partition map from the IORegistry: `ioreg -a -c IOMedia -r -l`, no root needed (K7).

For a whole disk the tree is: IOMedia (whole) → partition-scheme driver → one IOMedia
per partition. Offsets (`Base`), sizes and raw GPT type GUIDs come from here; this is
the only source of the map that works without root on a real disk.

Key allowlists below were collected on 2026-09-25 (macOS 26.6.2, arm64) from every
rig fixture — mounted and unmounted — and read-only from disk0. A key outside them
stops the parser (rules.md §S9); it is added only after checking what it means.
`Live` was added on 2026-09-27 (D24): see `_live`.
Children of a partition (its content: APFS container, BSD client) are not read.
"""

from __future__ import annotations

import plistlib
import re
from dataclasses import dataclass
from typing import Any

from core.probe.command import run_read_only
from core.probe.errors import StalePartitions, UnfamiliarInput
from core.probe.strict import check_keys, get, optional

IOREG_ARGV = ["/usr/sbin/ioreg", "-a", "-c", "IOMedia", "-r", "-l"]

GPT = "GUID_partition_scheme"
MBR = "FDisk_partition_scheme"
# A whole disk without a partition table — blank, or one file system over the whole
# disk: `Content` is empty, `Leaf` true, no scheme driver under it (rig, T-030).
NO_SCHEME = ""
SCHEME_CLASS = {GPT: "IOGUIDPartitionScheme", MBR: "IOFDiskPartitionScheme"}

_IOSERVICE = {
    "IOObjectClass",
    "IOObjectRetainCount",
    "IORegistryEntryChildren",
    "IORegistryEntryID",
    "IORegistryEntryName",
    "IOServiceBusyState",
    "IOServiceBusyTime",
    "IOServiceState",
}
_MEDIA = _IOSERVICE | {
    "BSD Major",
    "BSD Minor",
    "BSD Name",
    "BSD Unit",
    "Content",
    "Content Hint",
    "Ejectable",
    "IOBusyInterest",
    "IOGeneralInterest",
    "Leaf",
    "Open",
    "Preferred Block Size",
    "Removable",
    "Size",
    "Whole",
    "Writable",
}
_DRIVER = _IOSERVICE | {
    "CFBundleIdentifier",
    "CFBundleIdentifierKernel",
    "IOClass",
    "IOMatchCategory",
    "IOMatchedAtBoot",
    "IOPersonalityPublisher",
    "IOProbeScore",
    "IOProviderClass",
}

# TierType marks an APFS physical store; on a whole disk when the store is the whole disk
# (macOS 15 cryptex images, K20) — as on a GPT partition below.
WHOLE_KEYS = _MEDIA | {"IOMediaIcon", "TierType"}
APFS_CONTAINER_SCHEME = "AppleAPFSContainerScheme"
# kIOMediaLiveKey (IOPartitionScheme.h): set only when the scheme re-read the table while
# a partition of the disk was open (D24). Seen on GPT and MBR.
_PARTITION = _MEDIA | {"Base", "IORegistryEntryLocation", "Partition ID", "Live"}
PARTITION_KEYS = {
    GPT: _PARTITION | {"GPT Attributes", "UUID", "TierType"},
    MBR: _PARTITION,
}
SCHEME_KEYS = {
    GPT: _DRIVER | {"Content Mask", "IOPropertyMatch", "UUID"},
    MBR: _DRIVER | {"Content Mask", "IOPropertyMatch", "Content Table"},
}
# Children of a whole disk that are not the partition scheme and are not read.
WHOLE_CHILD_CLASSES_IGNORED = {"IOMediaBSDClient"}

_GUID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
_BYTE = re.compile(r"^0x[0-9A-F]{2}$")
_WHOLE_NAME = re.compile(r"^disk[0-9]+$")


@dataclass(frozen=True)
class IoPartition:
    bsd_name: str
    partition_id: int
    base: int  # bytes from the start of the disk
    size: int  # bytes
    content: str  # GPT: raw type GUID; MBR: the kernel's name for the type byte
    gpt_attributes: int | None  # GPT only
    uuid: str | None  # GPT partition GUID; MBR has none


@dataclass(frozen=True)
class IoDisk:
    bsd_name: str
    size: int
    block_size: int
    scheme: str  # GPT, MBR or NO_SCHEME
    scheme_uuid: str | None  # GPT disk GUID
    mbr_type_names: dict[str, int] | None  # MBR only: kernel's name → type byte
    partitions: tuple[IoPartition, ...]


def read_iomedia() -> list[Any]:
    """Run ioreg and return the raw plist (a list of root IOMedia trees)."""
    return plistlib.loads(run_read_only(IOREG_ARGV))


def parse_disk(roots: Any, bsd_name: str) -> IoDisk:
    """Extract the partition map of whole disk BSD_NAME from ioreg output."""
    if not _WHOLE_NAME.match(bsd_name):
        raise UnfamiliarInput(f"not a whole-disk identifier: {bsd_name!r}")
    if not isinstance(roots, list):
        raise UnfamiliarInput(f"ioreg: expected a list of trees, got {type(roots).__name__}")
    found = [r for r in roots if isinstance(r, dict) and r.get("BSD Name") == bsd_name]
    if len(found) != 1:
        # A synthesized whole disk (APFS container) is nested under its physical
        # store, not a root, and has no partition map of its own.
        raise UnfamiliarInput(f"ioreg: {bsd_name} is not exactly one root IOMedia (found {len(found)})")
    whole = found[0]
    what = f"ioreg {bsd_name}"

    check_keys(whole, WHOLE_KEYS, what)
    if get(whole, "IOObjectClass", str, what) != "IOMedia":
        raise UnfamiliarInput(f"{what}: IOObjectClass is not IOMedia")
    if get(whole, "Whole", bool, what) is not True:
        raise UnfamiliarInput(f"{what}: Whole is not true")
    scheme = get(whole, "Content", str, what)
    if scheme not in SCHEME_CLASS and scheme != NO_SCHEME:
        raise UnfamiliarInput(f"{what}: unfamiliar partition scheme Content={scheme!r}")
    size = get(whole, "Size", int, what)
    block_size = get(whole, "Preferred Block Size", int, what)
    if block_size <= 0 or size <= 0 or size % block_size:
        raise UnfamiliarInput(f"{what}: Size={size} is not a positive multiple of block size {block_size}")
    if scheme == NO_SCHEME:
        # The one child allowed besides the ignored: an APFS container over the whole
        # disk (K20) — then the disk is not a leaf.
        containers = 0
        for child in _children(whole, what):
            cls = get(child, "IOObjectClass", str, what)
            if cls == APFS_CONTAINER_SCHEME:
                containers += 1
            elif cls not in WHOLE_CHILD_CLASSES_IGNORED:
                raise UnfamiliarInput(f"{what}: no partition scheme, yet a child of class {cls!r}")
        if containers > 1 or get(whole, "Leaf", bool, what) is not (containers == 0):
            raise UnfamiliarInput(f"{what}: Leaf does not match Content={scheme!r} and {containers} APFS container(s)")
        return IoDisk(bsd_name, size, block_size, NO_SCHEME, None, None, ())
    if get(whole, "Leaf", bool, what):
        raise UnfamiliarInput(f"{what}: Leaf does not match Content={scheme!r}")

    drivers = []
    for child in _children(whole, what):
        cls = get(child, "IOObjectClass", str, what)
        if cls == SCHEME_CLASS[scheme]:
            drivers.append(child)
        elif cls not in WHOLE_CHILD_CLASSES_IGNORED:
            raise UnfamiliarInput(f"{what}: unfamiliar child class {cls!r}")
    if len(drivers) != 1:
        raise UnfamiliarInput(f"{what}: expected one {SCHEME_CLASS[scheme]}, found {len(drivers)}")
    driver = drivers[0]
    dwhat = f"{what} {SCHEME_CLASS[scheme]}"
    check_keys(driver, SCHEME_KEYS[scheme], dwhat)
    if get(driver, "Content Mask", str, dwhat) != scheme:
        raise UnfamiliarInput(f"{dwhat}: Content Mask differs from the disk's Content {scheme!r}")

    scheme_uuid = None
    mbr_type_names = None
    if scheme == GPT:
        scheme_uuid = _guid(driver, "UUID", dwhat)
    else:
        mbr_type_names = _mbr_type_names(get(driver, "Content Table", dict, dwhat), dwhat)

    partitions = []
    stale = []
    for child in _children(driver, dwhat):
        partitions.append(_partition(child, scheme, bsd_name, block_size))
        if not _live(child, f"{what} partition {partitions[-1].bsd_name!r}"):
            stale.append(partitions[-1].bsd_name)
    if stale:
        raise StalePartitions(
            f"{bsd_name}: the partition table changed while the disk was in use, and ioreg marks "
            f"{', '.join(sorted(stale))} out of date (Live=false): slice names no longer match "
            f"table entries. Eject and reattach {bsd_name}, then run again."
        )
    return IoDisk(
        bsd_name=bsd_name,
        size=size,
        block_size=block_size,
        scheme=scheme,
        scheme_uuid=scheme_uuid,
        mbr_type_names=mbr_type_names,
        partitions=tuple(sorted(partitions, key=lambda p: p.base)),
    )


def _partition(obj: Any, scheme: str, whole: str, block_size: int) -> IoPartition:
    if not isinstance(obj, dict):
        raise UnfamiliarInput(f"ioreg {whole}: partition entry is not a dict")
    name = obj.get("BSD Name")
    what = f"ioreg {whole} partition {name!r}"
    check_keys(obj, PARTITION_KEYS[scheme], what)
    if get(obj, "IOObjectClass", str, what) != "IOMedia":
        raise UnfamiliarInput(f"{what}: IOObjectClass is not IOMedia")
    bsd_name = get(obj, "BSD Name", str, what)
    if not re.match(rf"^{whole}s[0-9]+$", bsd_name):
        raise UnfamiliarInput(f"{what}: not a slice of {whole}")
    if get(obj, "Whole", bool, what) is not False:
        raise UnfamiliarInput(f"{what}: Whole is not false")
    if get(obj, "Preferred Block Size", int, what) != block_size:
        raise UnfamiliarInput(f"{what}: block size differs from the whole disk's")
    base = get(obj, "Base", int, what)
    size = get(obj, "Size", int, what)
    if base % block_size or size % block_size or size <= 0:
        raise UnfamiliarInput(f"{what}: Base={base} Size={size} not block-aligned")
    content = get(obj, "Content", str, what)
    if scheme == GPT:
        if not _GUID.match(content):
            raise UnfamiliarInput(f"{what}: GPT Content {content!r} is not a type GUID")
        gpt_attributes = get(obj, "GPT Attributes", int, what)
        uuid = _guid(obj, "UUID", what)
    else:
        gpt_attributes = None
        uuid = None
    return IoPartition(
        bsd_name=bsd_name,
        partition_id=get(obj, "Partition ID", int, what),
        base=base,
        size=size,
        content=content,
        gpt_attributes=gpt_attributes,
        uuid=uuid,
    )


def _live(obj: dict[str, Any], what: str) -> bool:
    """False only for a partition object that is out of date with the on-disk table.

    Measured on the rig (2026-09-27, D24): absent after attach and after any table
    rewrite with nothing on the disk open (partitionDisk unmounts first). When the table
    changes while a partition is open (eraseVolume next to a mounted volume), the scheme
    updates the objects in place and marks all of them: True where the object's BSD
    unit still equals its entry index, False where an inserted entry shifted the index —
    Base, Size, Content and Partition ID were already the new ones, only diskNsM is off.
    Unmount does not clear it; eject and reattach does.
    """
    return optional(obj, "Live", bool, what) is not False


def _mbr_type_names(table: dict[str, Any], what: str) -> dict[str, int]:
    """Invert the kernel's `Content Table` ("0x07" → "Windows_NTFS") to name → byte."""
    names: dict[str, int] = {}
    for key, name in table.items():
        if not isinstance(key, str) or not _BYTE.match(key) or not isinstance(name, str):
            raise UnfamiliarInput(f"{what}: unfamiliar Content Table entry {key!r}: {name!r}")
        if name in names:
            # Two bytes with one name: the byte cannot be recovered from the name.
            raise UnfamiliarInput(f"{what}: Content Table maps {name!r} to more than one byte")
        names[name] = int(key, 16)
    return names


def _children(obj: dict[str, Any], what: str) -> list[dict[str, Any]]:
    children = obj.get("IORegistryEntryChildren", [])
    if not isinstance(children, list) or not all(isinstance(c, dict) for c in children):
        raise UnfamiliarInput(f"{what}: IORegistryEntryChildren is not a list of dicts")
    return children


def _guid(obj: dict[str, Any], key: str, what: str) -> str:
    value = get(obj, key, str, what)
    if not _GUID.match(value):
        raise UnfamiliarInput(f"{what}: {key!r}={value!r} is not a GUID")
    return value
