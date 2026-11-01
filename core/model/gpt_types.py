"""GPT partition types diskmap knows by name (T-025).

A type GUID is fixed by a specification, not by this Mac, so the table lives in code.
`name` is what `diskutil list` calls the type: every entry was cut on the rig as a
`%<GUID>% %noformat%` slot (fixture gpt-types) and read back on macOS 26.6.2. The
layout stops if diskutil ever names a known GUID differently (§S9), and the planner
can predict diskutil's output with these names (T-041).

A GUID that is not here is shown as is — diskutil does the same — never as "Unknown".
testrig/fixture.sh (GPT_TYPES) cuts exactly these types plus one unknown GUID.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartitionType:
    guid: str
    name: str  # as `diskutil list` prints it


EFI = PartitionType("C12A7328-F81F-11D2-BA4B-00A0C93EC93B", "EFI")
BIOS_BOOT = PartitionType("21686148-6449-6E6F-744E-656564454649", "Bios Boot Partition")
MS_BASIC_DATA = PartitionType("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7", "Microsoft Basic Data")
MS_RESERVED = PartitionType("E3C9E316-0B5C-4DB8-817D-F92DF00215AE", "Microsoft Reserved")
WINDOWS_RECOVERY = PartitionType("DE94BBA4-06D1-4D40-A16A-BFD50179D6AC", "Windows Recovery")
LINUX_FS = PartitionType("0FC63DAF-8483-4772-8E79-3D69D8477DE4", "Linux Filesystem")
LINUX_SWAP = PartitionType("0657FD6D-A4AB-43C4-84E5-0933C84B4F4F", "Linux Swap")
LINUX_LVM = PartitionType("E6D6D379-F507-44C2-A23C-238F2A3DF928", "Linux LVM")
LINUX_RAID = PartitionType("A19D880F-05FC-4D3B-A006-743F0F84911E", "Linux_RAID")
APPLE_HFS = PartitionType("48465300-0000-11AA-AA11-00306543ECAC", "Apple_HFS")
APPLE_APFS = PartitionType("7C3457EF-0000-11AA-AA11-00306543ECAC", "Apple_APFS")
APPLE_BOOT = PartitionType("426F6F74-0000-11AA-AA11-00306543ECAC", "Apple_Boot")
APPLE_APFS_ISC = PartitionType("69646961-6700-11AA-AA11-00306543ECAC", "Apple_APFS_ISC")
APPLE_APFS_RECOVERY = PartitionType("52637672-7900-11AA-AA11-00306543ECAC", "Apple_APFS_Recovery")
APPLE_CORE_STORAGE = PartitionType("53746F72-6167-11AA-AA11-00306543ECAC", "Apple_CoreStorage")

TYPES: dict[str, PartitionType] = {
    t.guid: t
    for t in (
        EFI,
        BIOS_BOOT,
        MS_BASIC_DATA,
        MS_RESERVED,
        WINDOWS_RECOVERY,
        LINUX_FS,
        LINUX_SWAP,
        LINUX_LVM,
        LINUX_RAID,
        APPLE_HFS,
        APPLE_APFS,
        APPLE_BOOT,
        APPLE_APFS_ISC,
        APPLE_APFS_RECOVERY,
        APPLE_CORE_STORAGE,
    )
}


def known(guid: str) -> PartitionType | None:
    return TYPES.get(guid)
