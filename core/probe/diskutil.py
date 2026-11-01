"""`diskutil list -plist <diskN>` — partition type names and volume names (K7).

ioreg (core.probe.ioreg) is the source of offsets and raw types; diskutil adds what
ioreg does not know reliably — the volume name — and a second opinion on the
partition set and sizes, which the snapshot cross-checks.

Key allowlists were collected on 2026-09-25 (macOS 26.6.2) from every rig fixture,
mounted and unmounted. Unfamiliar keys stop the parser (rules.md §S9).
"""

from __future__ import annotations

import plistlib
import re
from dataclasses import dataclass
from typing import Any

from core.probe.command import run_read_only
from core.probe.errors import UnfamiliarInput
from core.probe.strict import check_keys, get, optional

TOP_KEYS = {"AllDisks", "AllDisksAndPartitions", "VolumesFromDisks", "WholeDisks"}
# MountPoint and VolumeName: a file system over the whole disk, no partition table (T-030).
WHOLE_KEYS = {"Content", "DeviceIdentifier", "MountPoint", "OSInternal", "Partitions", "Size", "VolumeName"}
PARTITION_KEYS = {"Content", "DeviceIdentifier", "DiskUUID", "MountPoint", "Size", "VolumeName", "VolumeUUID"}

_WHOLE_NAME = re.compile(r"^disk[0-9]+$")


@dataclass(frozen=True)
class DuPartition:
    bsd_name: str
    size: int
    content: str  # diskutil's name for the partition type, e.g. "Microsoft Basic Data"
    disk_uuid: str | None  # GPT partition GUID; absent on MBR
    volume_name: str | None  # absent when diskutil recognizes no volume


@dataclass(frozen=True)
class DuDisk:
    bsd_name: str
    size: int
    content: str
    partitions: tuple[DuPartition, ...]
    volume_name: str | None  # of a file system over the whole disk; absent otherwise


def read_list(bsd_name: str) -> Any:
    """Run `diskutil list -plist BSD_NAME` and return the raw plist."""
    if not _WHOLE_NAME.match(bsd_name):
        raise UnfamiliarInput(f"not a whole-disk identifier: {bsd_name!r}")
    return plistlib.loads(run_read_only(["/usr/sbin/diskutil", "list", "-plist", bsd_name]))


def parse_list(plist: Any, bsd_name: str) -> DuDisk:
    what = f"diskutil list {bsd_name}"
    if not isinstance(plist, dict):
        raise UnfamiliarInput(f"{what}: top level is not a dict")
    check_keys(plist, TOP_KEYS, what)
    if get(plist, "WholeDisks", list, what) != [bsd_name]:
        raise UnfamiliarInput(f"{what}: WholeDisks is not [{bsd_name!r}]")
    entries = get(plist, "AllDisksAndPartitions", list, what)
    if len(entries) != 1 or not isinstance(entries[0], dict):
        raise UnfamiliarInput(f"{what}: expected one AllDisksAndPartitions entry")
    whole = entries[0]
    check_keys(whole, WHOLE_KEYS, what)
    if get(whole, "DeviceIdentifier", str, what) != bsd_name:
        raise UnfamiliarInput(f"{what}: DeviceIdentifier is not {bsd_name!r}")

    partitions = []
    for entry in whole.get("Partitions", []):
        if not isinstance(entry, dict):
            raise UnfamiliarInput(f"{what}: partition entry is not a dict")
        pwhat = f"{what} partition {entry.get('DeviceIdentifier')!r}"
        check_keys(entry, PARTITION_KEYS, pwhat)
        partitions.append(
            DuPartition(
                bsd_name=get(entry, "DeviceIdentifier", str, pwhat),
                size=get(entry, "Size", int, pwhat),
                content=get(entry, "Content", str, pwhat),
                disk_uuid=optional(entry, "DiskUUID", str, pwhat),
                volume_name=optional(entry, "VolumeName", str, pwhat),
            )
        )
    listed = [bsd_name] + [p.bsd_name for p in partitions]
    if sorted(get(plist, "AllDisks", list, what)) != sorted(listed):
        raise UnfamiliarInput(f"{what}: AllDisks disagrees with the partition list")
    return DuDisk(
        bsd_name=bsd_name,
        size=get(whole, "Size", int, what),
        content=get(whole, "Content", str, what),
        partitions=tuple(partitions),
        volume_name=optional(whole, "VolumeName", str, what),
    )
