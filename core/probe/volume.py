"""`diskutil info -plist <slice>` → core.model.volume (T-021). Read-only, no root.

One call per partition and per APFS volume, or for a whole disk without a partition
table (its file system sits on the disk itself, T-030). What it adds to the map: file system,
label, mount point, whether the mount is writable, capacity and free space. What it
does not have: the raw partition type GUID (only diskutil's name for it — the GUID
comes from ioreg, K7). Whole-disk flags (Internal, VirtualOrPhysical, …) belong to
the whole disk and are read by core.probe.system (D6).

FilesystemType is a claim: on an unmounted slot diskutil may infer it from the
partition type alone (D13, VolumeInfo.confirmed).

The size fields mean something only on a mounted non-APFS file system; everywhere
else diskutil reports 0 and 0, which is not "empty" (see core.model.volume). A value
outside that pattern is unfamiliar and stops (§S9). The file system type is passed
through as is, not checked against a list: it is shown, not acted upon.

Key allowlist collected on 2026-09-26 (macOS 26.6.2, arm64) from every slice of every
rig fixture, mounted and unmounted, plus this Mac's internal disk, its synthesized
containers (a sealed snapshot included) and the EFI partition of a USB SSD.
"""

from __future__ import annotations

import plistlib
import re
from dataclasses import dataclass
from typing import Any

from core.model.volume import Space, VolumeInfo
from core.probe.command import run_read_only
from core.probe.errors import UnfamiliarInput
from core.probe.strict import FUSION, check_keys, get, not_fusion, optional

INFO_SLICE_KEYS = {
    FUSION,  # macOS 15 only (core.probe.strict.not_fusion)
    "AESHardware",
    "APFSContainerFree",
    "APFSContainerReference",
    "APFSContainerSize",
    "APFSPhysicalStores",
    "APFSSnapshot",
    "APFSSnapshotName",
    "APFSSnapshotUUID",
    "APFSVolumeGroupID",
    "Bootable",
    "BooterDeviceIdentifier",
    "BusProtocol",
    "CanBeMadeBootable",
    "CanBeMadeBootableRequiresDestroy",
    "CapacityInUse",
    "Content",
    "DeviceBlockSize",
    "DeviceIdentifier",
    "DeviceNode",
    "DeviceTreePath",
    "DiskUUID",
    "Ejectable",
    "EjectableMediaAutomaticUnderSoftwareControl",
    "EjectableOnly",
    "Encryption",
    "EncryptionThisVolumeProper",
    "FileVault",
    "FilesystemName",
    "FilesystemType",
    "FilesystemUserVisibleName",
    "FreeSpace",
    "GlobalPermissionsEnabled",
    "IOKitSize",
    "IORegistryEntryName",
    "Internal",
    "JournalOffset",
    "JournalSize",
    "Locked",
    "MacOSSystemAPFSEFIDriverVersion",
    "MediaName",
    "MediaType",
    "MountPoint",
    "OSInternalMedia",
    "ParentWholeDisk",
    "PartitionMapPartition",
    "PartitionMapPartitionOffset",
    "RAIDMaster",
    "RAIDSlice",
    "RecoveryDeviceIdentifier",
    "Removable",
    "RemovableMedia",
    "RemovableMediaOrExternalDevice",
    "SMARTDeviceSpecificKeysMayVaryNotGuaranteed",
    "SMARTStatus",
    "Sealed",
    "Size",
    "SolidState",
    "SupportsGlobalPermissionsDisable",
    "SystemImage",
    "TotalSize",
    "VolumeAllocationBlockSize",
    "VolumeName",
    "VolumeSize",
    "VolumeUUID",
    "WholeDisk",
    "Writable",
    "WritableMedia",
    "WritableVolume",
}
# A whole disk without a partition table carries its file system itself (T-030);
# its `info` has the whole-disk keys as well (rig fixtures no-table, whole-exfat).
INFO_WHOLE_KEYS = INFO_SLICE_KEYS | {"LowLevelFormatSupported", "OS9DriversInstalled", "VirtualOrPhysical"}
FS_KEYS = ("FilesystemType", "FilesystemName", "FilesystemUserVisibleName")
APFS = "apfs"

_WHOLE = re.compile(r"^disk[0-9]+$")
_SLICE = re.compile(r"^disk[0-9]+s[0-9]+$")


@dataclass(frozen=True)
class DuSliceInfo:
    """One slice's `diskutil info`: the VolumeInfo, plus what places the slice — for
    the cross-check against ioreg, `diskutil list` and `apfs list` (core.probe.layout)."""

    volume: VolumeInfo
    parent: str  # ParentWholeDisk
    content: str  # partition type name; for an APFS volume, its type GUID
    size: int  # bytes
    partition_offset: int | None  # bytes; present exactly on partition-map partitions
    container: str | None  # APFSContainerReference: a physical store's or a volume's container


def read_info(bsd_name: str) -> Any:
    """Run `diskutil info -plist BSD_NAME` for a partition, an APFS volume or a whole
    disk without a partition table."""
    if not (_SLICE.match(bsd_name) or _WHOLE.match(bsd_name)):
        raise UnfamiliarInput(f"not a disk, partition or volume identifier: {bsd_name!r}")
    return plistlib.loads(run_read_only(["/usr/sbin/diskutil", "info", "-plist", bsd_name]))


def parse_info(info: Any, bsd_name: str) -> DuSliceInfo:
    what = f"diskutil info {bsd_name}"
    if not isinstance(info, dict):
        raise UnfamiliarInput(f"{what}: top level is not a dict")
    whole = bool(_WHOLE.match(bsd_name))
    check_keys(info, INFO_WHOLE_KEYS if whole else INFO_SLICE_KEYS, what)
    not_fusion(info, what)
    if get(info, "DeviceIdentifier", str, what) != bsd_name or get(info, "WholeDisk", bool, what) is not whole:
        raise UnfamiliarInput(f"{what}: not the {'whole disk' if whole else 'slice'} {bsd_name!r}")
    parent = get(info, "ParentWholeDisk", str, what)
    container = optional(info, "APFSContainerReference", str, what)
    if not _WHOLE.match(parent) or (container is not None and not _WHOLE.match(container)):
        raise UnfamiliarInput(f"{what}: ParentWholeDisk / APFSContainerReference are not whole disks")
    if whole and (parent != bsd_name or container is not None):
        # A synthesized container is a whole disk too, but it is read through its volumes.
        raise UnfamiliarInput(f"{what}: a whole disk that is its own parent and no APFS container was expected")

    in_map = get(info, "PartitionMapPartition", bool, what)
    offset = optional(info, "PartitionMapPartitionOffset", int, what)
    if in_map != (offset is not None) or (whole and in_map):
        raise UnfamiliarInput(f"{what}: PartitionMapPartitionOffset without PartitionMapPartition, or back")

    fs = [optional(info, k, str, what) for k in FS_KEYS]
    if any(v is None for v in fs) and any(v is not None for v in fs):
        raise UnfamiliarInput(f"{what}: only some of {list(FS_KEYS)} present")
    fs_type, _, fs_name = fs
    if fs_type == APFS and (in_map or container is None):
        raise UnfamiliarInput(f"{what}: an APFS volume must be a slice of a container, not a partition")

    label = get(info, "VolumeName", str, what) or None
    mount = get(info, "MountPoint", str, what) or None
    volume_uuid = optional(info, "VolumeUUID", str, what)
    writable = get(info, "WritableVolume", bool, what)
    if fs_type is None and (label or mount or volume_uuid):
        raise UnfamiliarInput(f"{what}: a label, a mount point or a volume UUID without a file system")
    if mount is None and writable:
        raise UnfamiliarInput(f"{what}: WritableVolume on a volume that is not mounted")

    return DuSliceInfo(
        volume=VolumeInfo(
            bsd_name=bsd_name,
            filesystem=fs_type,
            filesystem_name=fs_name,
            # Traces of the file system itself, not of the partition type (D13).
            confirmed=fs_type is not None and (mount is not None or label is not None or volume_uuid is not None),
            label=label,
            mount_point=mount,
            writable=writable if mount is not None else None,
            space=_space(info, fs_type, mount, what),
        ),
        parent=parent,
        content=get(info, "Content", str, what),
        size=get(info, "Size", int, what),
        partition_offset=offset,
        container=container,
    )


def _space(info: dict[str, Any], fs_type: str | None, mount: str | None, what: str) -> Space | None:
    capacity = get(info, "VolumeSize", int, what)
    free = get(info, "FreeSpace", int, what)
    if fs_type is None or fs_type == APFS or mount is None:
        # Not "empty" — unknown, or kept by the APFS container.
        if capacity != 0 or free != 0:
            raise UnfamiliarInput(f"{what}: VolumeSize/FreeSpace {capacity}/{free} where 0/0 was expected")
        return None
    if not 0 <= free <= capacity or capacity == 0:
        raise UnfamiliarInput(f"{what}: VolumeSize/FreeSpace {capacity}/{free} of a mounted file system")
    return Space(capacity=capacity, free=free)
