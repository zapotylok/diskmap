"""The file system on a partition or an APFS volume, as `diskutil info` reports it (T-021).

Pure data, no probing. Built by core.probe.volume, attached by core.probe.layout.

"Unknown" is None, never 0: an unmounted volume reports size 0 and free 0, and a
preview that took that for "empty" would understate what a step destroys (T-014).

`filesystem` is diskutil's claim, not proof (D13). On an unmounted slot diskutil
names a file system after the partition type even when the slot was wiped (EFI →
msdos, Apple_HFS → hfs), and a real NTFS without a label leaves no more trace than
that. `confirmed` says diskutil found the file system itself. Unconfirmed is neither
"there is one" nor "there is none": a preview must assume data may be there.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Space:
    """What a mounted file system offers. APFS has none per volume: its space is the
    container's (ApfsContainer.capacity_free, ApfsVolume.capacity_in_use)."""

    capacity: int  # bytes
    free: int  # bytes

    @property
    def used(self) -> int:
        return self.capacity - self.free


@dataclass(frozen=True)
class VolumeInfo:
    bsd_name: str
    filesystem: str | None  # FilesystemType ("exfat", "apfs", ...); None: diskutil names none
    filesystem_name: str | None  # FilesystemUserVisibleName, e.g. "Mac OS Extended (Journaled)"
    confirmed: bool  # mounted, or has a label or a volume UUID — see the module docstring
    label: str | None  # None: no label (diskutil itself says "NO NAME"/"Untitled" for some)
    mount_point: str | None  # None when not mounted
    writable: bool | None  # of the mount (an FSKit NTFS mount is not); None when not mounted
    space: Space | None  # None when not mounted, and always for APFS

    @property
    def mounted(self) -> bool:
        return self.mount_point is not None
