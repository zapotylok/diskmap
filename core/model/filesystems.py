"""File system personalities diskutil can format with, as it reports them (T-023).

Pure data, no probing. Built by core.probe.filesystems from `diskutil
listFilesystems -plist` — the list of formats is never hardcoded: without Paragon
there are no UFSD_* entries, with it there are, named as Paragon names them — plus
each personality's content mask from its bundle (T-042).

What the source does not say is not modelled: rules for volume names and case
sensitivity are not in the plist (the latter only as a word in some names).
"""

from __future__ import annotations

from dataclasses import dataclass

SYSTEM_BUNDLES = "/System/Library/Filesystems/"


@dataclass(frozen=True)
class Personality:
    name: str  # what diskutil verbs take, e.g. "Journaled HFS+", "UFSD_EXTFS4"
    user_visible_name: str  # e.g. "Mac OS Extended (Journaled)", "extFS 4"
    bundle: str
    # None: a partition type diskutil can cut, with no file system behind it
    # ("Free Space", "Linux Swap", "Linux Native") — sizes are then None too.
    bundle_path: str | None
    min_size: int | None  # bytes
    max_size: int | None  # bytes
    bootable: bool
    requires_booter: bool  # RequiresBooterToBoot
    # RequiresWholeDiskFormat as diskutil says it. Not interpreted: it is true for every
    # MS-DOS personality, and partitionDisk still cuts FAT32 partitions (rig fixtures).
    whole_disk_format: bool
    supports_resize: bool
    # FSFormatContentMask from the bundle's Info.plist: what diskutil turns into the
    # partition type (a MediaKit content name such as "Windows_NTFS", or a GUID). Not
    # in listFilesystems; filled by core.probe.filesystems.read_registry (T-042, D22).
    # None: not read, or the bundle names none.
    content_mask: str | None = None

    @property
    def formats(self) -> bool:
        """A file system is created, not only a partition type set."""
        return self.bundle_path is not None

    @property
    def third_party(self) -> bool:
        """Installed outside macOS (Paragon lives in /Library/Filesystems)."""
        return self.bundle_path is not None and not self.bundle_path.startswith(SYSTEM_BUNDLES)


@dataclass(frozen=True)
class Registry:
    personalities: tuple[Personality, ...]  # in diskutil's order

    def find(self, name: str) -> Personality | None:
        """By personality name; diskutil ignores case in them, so does this."""
        folded = name.casefold()
        return next((p for p in self.personalities if p.name.casefold() == folded), None)
