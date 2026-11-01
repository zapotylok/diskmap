"""Everything the system-disk guard reads, in one read-only pass (T-010, K6).

Four sources, none of them needs root:

  diskutil list -plist           every whole disk with its partitions / APFS volumes
                                 and their mount points — snapshots included: `/` is a
                                 sealed snapshot of the System volume, not the volume
  diskutil apfs list -plist      containers → physical stores; volumes → roles
  diskutil info -plist diskN     per whole disk: Internal, bus, virtual/physical, RAID
  diskutil info -plist /         the boot volume — a second path to it, independent
                                 of the mount points in `list`

A global source (list, apfs, `/`) that does not parse stops everything: without it no
disk can be judged. A whole disk whose own `info` does not parse is kept as Unreadable,
so the guard can protect that one disk and the map still shows the rest (K6).

Key allowlists were collected on 2026-09-26 (macOS 26.6.2, arm64) from this Mac's
internal disk and its synthesized containers, two USB sticks, a USB NVMe enclosure and
a rig disk image with an APFS container. An unfamiliar key stops the parser (§S9).
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.model.apfs import ApfsContainer
from core.probe.apfs import parse_apfs, read_apfs
from core.probe.command import run_read_only
from core.probe.diskutil import TOP_KEYS as LIST_TOP_KEYS
from core.probe.errors import UnfamiliarInput
from core.probe.strict import check_keys, dicts, get, optional, slice_id

LIST_WHOLE_KEYS = {
    "APFSPhysicalStores",
    "APFSVolumes",
    "Content",
    "DeviceIdentifier",
    "MountPoint",  # a file system over the whole disk, no partition table (T-030)
    "OSInternal",
    "Partitions",
    "Size",
    "VolumeName",  # likewise
}
LIST_VOLUME_KEYS = {
    "CapacityInUse",
    "Content",
    "DeviceIdentifier",
    "DiskUUID",
    "MountPoint",
    "MountedSnapshots",
    "OSInternal",
    "Size",
    "VolumeName",
    "VolumeUUID",
}
LIST_STORE_KEYS = {"DeviceIdentifier"}
LIST_SNAPSHOT_KEYS = {"Sealed", "SnapshotBSD", "SnapshotMountPoint", "SnapshotName", "SnapshotUUID"}

# `diskutil info` of a whole disk and of the boot volume. The two sets overlap but are
# not the same; both are exact unions of what was seen, not guesses.
_INFO_COMMON = {
    "AESHardware",
    "APFSContainerReference",
    "APFSPhysicalStores",
    "Bootable",
    "BusProtocol",
    "CanBeMadeBootable",
    "CanBeMadeBootableRequiresDestroy",
    "Content",
    "DeviceBlockSize",
    "DeviceIdentifier",
    "DeviceNode",
    "DeviceTreePath",
    "DiskUUID",
    "Ejectable",
    "EjectableMediaAutomaticUnderSoftwareControl",
    "EjectableOnly",
    "FreeSpace",
    "GlobalPermissionsEnabled",
    "IOKitSize",
    "IORegistryEntryName",
    "Internal",
    "MediaName",
    "MediaType",
    "MountPoint",
    "OSInternalMedia",
    "ParentWholeDisk",
    "PartitionMapPartition",
    "RAIDMaster",
    "RAIDSlice",
    "Removable",
    "RemovableMedia",
    "RemovableMediaOrExternalDevice",
    "SMARTDeviceSpecificKeysMayVaryNotGuaranteed",
    "SMARTStatus",
    "Size",
    "SolidState",
    "SupportsGlobalPermissionsDisable",
    "SystemImage",
    "TotalSize",
    "VolumeName",
    "VolumeSize",
    "WholeDisk",
    "Writable",
    "WritableMedia",
    "WritableVolume",
}
INFO_WHOLE_KEYS = _INFO_COMMON | {
    "LowLevelFormatSupported",
    "OS9DriversInstalled",
    "VirtualOrPhysical",
    # a file system over the whole disk, no partition table (T-030)
    "FilesystemName",
    "FilesystemType",
    "FilesystemUserVisibleName",
    "VolumeAllocationBlockSize",
    "VolumeUUID",
}
INFO_BOOT_KEYS = _INFO_COMMON | {
    "APFSContainerFree",
    "APFSContainerSize",
    "APFSSnapshot",
    "APFSSnapshotName",
    "APFSSnapshotUUID",
    "APFSVolumeGroupID",
    "BooterDeviceIdentifier",
    "CapacityInUse",
    "Encryption",
    "EncryptionThisVolumeProper",
    "FileVault",
    "FilesystemName",
    "FilesystemType",
    "FilesystemUserVisibleName",
    "Locked",
    "MacOSSystemAPFSEFIDriverVersion",
    "RecoveryDeviceIdentifier",
    "Sealed",
    "VolumeAllocationBlockSize",
    "VolumeUUID",
}
INFO_STORE_KEYS = {"APFSPhysicalStore"}
VIRTUAL_OR_PHYSICAL = {"Physical", "Virtual", "Unknown"}

_WHOLE = re.compile(r"^disk[0-9]+$")


@dataclass(frozen=True)
class Volume:
    """A partition, or an APFS volume of a synthesized disk."""

    bsd_name: str
    content: str | None  # partition type name; APFS volumes have none
    mount_points: tuple[str, ...]  # its own and those of its mounted snapshots
    snapshot_mount_points: tuple[str, ...] = ()  # those of its mounted snapshots alone


@dataclass(frozen=True)
class ListedDisk:
    bsd_name: str
    content: str
    volumes: tuple[Volume, ...]
    physical_stores: tuple[str, ...]  # non-empty only for a synthesized APFS container
    mount_point: str | None = None  # of a file system over the whole disk (no partition table)


@dataclass(frozen=True)
class DiskInfo:
    bsd_name: str
    internal: bool
    external: bool  # RemovableMediaOrExternalDevice
    bus: str
    virtual_or_physical: str
    physical_stores: tuple[str, ...]
    raid: bool  # RAIDMaster or RAIDSlice


@dataclass(frozen=True)
class Unreadable:
    """`diskutil info` of this whole disk did not parse; nothing about it is known."""

    bsd_name: str
    reason: str


@dataclass(frozen=True)
class BootVolume:
    bsd_name: str  # what is mounted at `/` — a snapshot on a sealed system
    parent_whole_disk: str
    container: str
    physical_stores: tuple[str, ...]


@dataclass(frozen=True)
class RawView:
    """The four tool outputs, unparsed — what fixtures record."""

    list: Any
    apfs: Any
    boot: Any
    info: Mapping[str, Any]  # whole disk → `diskutil info -plist`


@dataclass(frozen=True)
class SystemView:
    disks: tuple[ListedDisk, ...]
    info: Mapping[str, DiskInfo | Unreadable]
    containers: tuple[ApfsContainer, ...]
    boot: BootVolume


def probe() -> SystemView:
    """Read the whole system now. Read-only; nothing is cached (K6)."""
    return build(read_raw())


def read_raw() -> RawView:
    listing = _plist(["/usr/sbin/diskutil", "list", "-plist"])
    if not isinstance(listing, dict):
        raise UnfamiliarInput("diskutil list: top level is not a dict")
    wholes = _whole_names(listing)
    return RawView(
        list=listing,
        apfs=read_apfs(),
        boot=_plist(["/usr/sbin/diskutil", "info", "-plist", "/"]),
        info={d: _plist(["/usr/sbin/diskutil", "info", "-plist", d]) for d in wholes},
    )


def build(raw: RawView) -> SystemView:
    """Pure part of probe(): raw tool plists in, SystemView out."""
    disks = _parse_list(raw.list)
    names = [d.bsd_name for d in disks]
    if sorted(raw.info) != sorted(names):
        raise UnfamiliarInput("diskutil info was not read for exactly the listed whole disks")
    info: dict[str, DiskInfo | Unreadable] = {}
    for name in names:
        try:
            info[name] = _parse_info(raw.info[name], name)
        except UnfamiliarInput as e:
            # Not a default: the guard turns this into a protection reason (K6).
            info[name] = Unreadable(name, str(e))
    return SystemView(
        disks=tuple(disks),
        info=info,
        containers=parse_apfs(raw.apfs),
        boot=_parse_boot(raw.boot),
    )


def _plist(argv: list[str]) -> Any:
    return plistlib.loads(run_read_only(argv))


def _whole_names(listing: dict[str, Any]) -> list[str]:
    names = get(listing, "WholeDisks", list, "diskutil list")
    for n in names:
        if type(n) is not str or not _WHOLE.match(n):
            raise UnfamiliarInput(f"diskutil list: not a whole-disk identifier: {n!r}")
    return names


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if type(value) is not list or not all(type(v) is str for v in value):
        raise UnfamiliarInput(f"{what}: expected a list of strings")
    return tuple(value)


def _parse_list(listing: Any) -> list[ListedDisk]:
    what = "diskutil list"
    if not isinstance(listing, dict):
        raise UnfamiliarInput(f"{what}: top level is not a dict")
    check_keys(listing, LIST_TOP_KEYS, what)
    names = _whole_names(listing)
    entries = dicts(get(listing, "AllDisksAndPartitions", list, what), what)
    if [e.get("DeviceIdentifier") for e in entries] != names:
        raise UnfamiliarInput(f"{what}: AllDisksAndPartitions does not follow WholeDisks")

    disks = []
    listed = list(names)
    for whole in entries:
        name = whole["DeviceIdentifier"]
        wwhat = f"{what} {name}"
        check_keys(whole, LIST_WHOLE_KEYS, wwhat)
        volumes = []
        for key in ("Partitions", "APFSVolumes"):
            for v in dicts(optional(whole, key, list, wwhat) or [], f"{wwhat} {key}"):
                vol = _parse_list_volume(v, wwhat)
                volumes.append(vol)
                listed.append(vol.bsd_name)
        stores = []
        for s in dicts(optional(whole, "APFSPhysicalStores", list, wwhat) or [], f"{wwhat} APFSPhysicalStores"):
            check_keys(s, LIST_STORE_KEYS, f"{wwhat} physical store")
            stores.append(slice_id(s, "DeviceIdentifier", f"{wwhat} physical store"))
        disks.append(
            ListedDisk(
                bsd_name=name,
                content=get(whole, "Content", str, wwhat),
                volumes=tuple(volumes),
                physical_stores=tuple(stores),
                mount_point=optional(whole, "MountPoint", str, wwhat),
            )
        )
    all_disks = _strings(get(listing, "AllDisks", list, what), what)
    if sorted(all_disks) != sorted(listed):
        raise UnfamiliarInput(f"{what}: AllDisks disagrees with the disks and volumes listed")
    return disks


def _parse_list_volume(v: dict[str, Any], wwhat: str) -> Volume:
    vwhat = f"{wwhat} volume {v.get('DeviceIdentifier')!r}"
    check_keys(v, LIST_VOLUME_KEYS, vwhat)
    mount = optional(v, "MountPoint", str, vwhat)
    snapshots = []
    for s in dicts(optional(v, "MountedSnapshots", list, vwhat) or [], f"{vwhat} MountedSnapshots"):
        check_keys(s, LIST_SNAPSHOT_KEYS, f"{vwhat} snapshot")
        slice_id(s, "SnapshotBSD", f"{vwhat} snapshot")
        snap_mount = optional(s, "SnapshotMountPoint", str, f"{vwhat} snapshot")
        if snap_mount is not None:
            snapshots.append(snap_mount)
    return Volume(
        bsd_name=slice_id(v, "DeviceIdentifier", vwhat),
        content=optional(v, "Content", str, vwhat),
        mount_points=tuple(([mount] if mount is not None else []) + snapshots),
        snapshot_mount_points=tuple(snapshots),
    )


def _parse_info(info: Any, name: str) -> DiskInfo:
    what = f"diskutil info {name}"
    if not isinstance(info, dict):
        raise UnfamiliarInput(f"{what}: top level is not a dict")
    check_keys(info, INFO_WHOLE_KEYS, what)
    if get(info, "DeviceIdentifier", str, what) != name or get(info, "WholeDisk", bool, what) is not True:
        raise UnfamiliarInput(f"{what}: not the whole disk {name!r}")
    vop = get(info, "VirtualOrPhysical", str, what)
    if vop not in VIRTUAL_OR_PHYSICAL:
        raise UnfamiliarInput(f"{what}: VirtualOrPhysical {vop!r} is unfamiliar")
    stores = []
    for s in dicts(optional(info, "APFSPhysicalStores", list, what) or [], f"{what} APFSPhysicalStores"):
        check_keys(s, INFO_STORE_KEYS, f"{what} physical store")
        stores.append(slice_id(s, "APFSPhysicalStore", f"{what} physical store"))
    return DiskInfo(
        bsd_name=name,
        internal=get(info, "Internal", bool, what),
        external=get(info, "RemovableMediaOrExternalDevice", bool, what),
        bus=get(info, "BusProtocol", str, what),
        virtual_or_physical=vop,
        physical_stores=tuple(stores),
        raid=get(info, "RAIDMaster", bool, what) or get(info, "RAIDSlice", bool, what),
    )


def _parse_boot(boot: Any) -> BootVolume:
    what = "diskutil info /"
    if not isinstance(boot, dict):
        raise UnfamiliarInput(f"{what}: top level is not a dict")
    check_keys(boot, INFO_BOOT_KEYS, what)
    if get(boot, "MountPoint", str, what) != "/":
        raise UnfamiliarInput(f"{what}: MountPoint is not '/'")
    parent = get(boot, "ParentWholeDisk", str, what)
    container = get(boot, "APFSContainerReference", str, what)
    if not _WHOLE.match(parent) or not _WHOLE.match(container):
        raise UnfamiliarInput(f"{what}: ParentWholeDisk / APFSContainerReference are not whole disks")
    stores = []
    for s in dicts(get(boot, "APFSPhysicalStores", list, what), f"{what} APFSPhysicalStores"):
        check_keys(s, INFO_STORE_KEYS, f"{what} physical store")
        stores.append(slice_id(s, "APFSPhysicalStore", f"{what} physical store"))
    if not stores:
        raise UnfamiliarInput(f"{what}: no APFS physical store")
    return BootVolume(
        bsd_name=slice_id(boot, "DeviceIdentifier", what),
        parent_whole_disk=parent,
        container=container,
        physical_stores=tuple(stores),
    )
