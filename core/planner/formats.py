"""What a `format` in the desired layout means to a strategy (T-042, D22).

Three kinds, each measured on the rig (macOS 26.6.2, arm64, 2026-09-27):

- a diskutil personality, by the name `diskutil listFilesystems` gives it (any case,
  never hardcoded, D15): partitionDisk cuts the slot and formats it in one go. The
  slot's type is the personality's content mask (FSFormatContentMask of its bundle),
  mapped below — only masks whose resulting type was seen on the rig;
- `ntfs`: NTFS by ntfsprogs (K4) — a blank slot of type Microsoft basic data, then
  `mkntfs` on it and `diskutil mount` so DiskArbitration re-reads it. The slot is no
  longer cut as ExFAT (D7): `%<GUID>% %noformat%` gives the type without formatting
  and mounting a volume only to destroy it;
- `efi`: an ESP on request only (K9) — a blank EFI slot, then `newfs_msdos -F 32 -v EFI`;
- `ext4`: ext4 by mke2fs (T-053, D33) — a blank slot of type Linux filesystem, then
  `mke2fs` on its rdisk node. Paragon's UFSD_EXTFS4 stays a personality of its own.

Whether formatting makes diskutil ask on stdin is measured per file system bundle (D27,
rig 2026-10-02: partitionDisk, addPartition and resizeVolume, stdin closed, piped `n` and a
terminal): only Paragon's NTFS bundle asks, and diskutil answers it itself. A bundle not
measured is refused — the preview must not miss a question (§S10).

There is no fallback through `diskutil eraseVolume`: it re-cuts the slot by the default
mode's rules (booters, MiB) and renumbers slices (D22). A format that cannot be
expressed directly is refused.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.model import gpt_types as T
from core.model.filesystems import Personality, Registry

PERSONALITY = "personality"
NTFS = "ntfs"
EFI = "efi"
EXT4 = "ext4"
RESERVED = (NTFS, EFI, EXT4)

# Content mask → the GPT type partitionDisk gives the slot. Every row seen on the rig:
# Apple_HFS (JHFS+), Apple_HFSX (Case-sensitive Journaled HFS+), Windows_NTFS (ExFAT,
# UFSD_NTFS), DOS_FAT_32 (MS-DOS), Linux (UFSD_EXTFS4), the APFS GUID (APFS).
# DOS_FAT_12 / DOS_FAT_16 were not measured.
MASK_TYPES = {
    "Apple_HFS": T.APPLE_HFS.guid,
    "Apple_HFSX": T.APPLE_HFS.guid,
    "Windows_NTFS": T.MS_BASIC_DATA.guid,
    "DOS_FAT_32": T.MS_BASIC_DATA.guid,
    "Linux": T.LINUX_FS.guid,
    T.APPLE_APFS.guid: T.APPLE_APFS.guid,
}

# File system bundle → the questions diskutil prints while formatting with any of its
# personalities, and answers itself. Keyed by bundle, not by personality name (D15): the
# question comes from the bundle's formatter (Paragon newfs_ufsd for NTFS). Every
# personality of these bundles the planner can emit was measured (D27).
PARAGON_NTFS_ASKS = (("Proceed with Format (y/n)?", "y"),)
PROMPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "ufsd_NTFS": PARAGON_NTFS_ASKS,
    "ufsd_ExtFS": (),
    "msdos": (),
    "hfs": (),
    "exfat": (),
    "apfs": (),
}

# The personality whose size limits an ESP obeys: newfs_msdos -F 32 is what formats it.
EFI_LIMITS = "MS-DOS FAT32"

# Below this mke2fs -t ext4 makes no journal (rig, 2026-10-17: 1 MiB without, 4 MiB with).
EXT4_MIN = 4 * 2**20
# The ext4 label field; mke2fs truncates a longer label with only a warning.
EXT4_LABEL_BYTES = 16


class FormatError(ValueError):
    """The format cannot be planned; the message says why."""


@dataclass(frozen=True)
class Format:
    kind: str  # PERSONALITY / NTFS / EFI / EXT4
    written: str  # as in the layout
    type: str  # GPT type GUID the slot gets
    personality: Personality | None  # PERSONALITY: the one partitionDisk formats with
    min_size: int | None  # bytes; None: no limit known
    max_size: int | None
    asks: tuple[tuple[str, str], ...] = ()  # what diskutil asks and answers itself (PROMPTS)


def resolve_format(written: str, registry: Registry) -> Format:
    folded = written.strip().casefold()
    clash = [p.name for p in registry.personalities if p.name.casefold() in RESERVED]
    if clash:
        # A personality now spells a word diskmap reserves: which one is meant is a guess.
        raise FormatError(f"diskutil now lists a personality named {clash[0]!r}, a word diskmap reserves")
    if folded == NTFS:
        return Format(NTFS, written, T.MS_BASIC_DATA.guid, None, None, None)
    if folded == EFI:
        fat = registry.find(EFI_LIMITS)
        if fat is None or not fat.formats:
            raise FormatError(f"efi: diskutil does not list {EFI_LIMITS!r}, whose limits an ESP obeys")
        return Format(EFI, written, T.EFI.guid, None, fat.min_size, fat.max_size)
    if folded == EXT4:
        return Format(EXT4, written, T.LINUX_FS.guid, None, EXT4_MIN, None)
    p = registry.find(written.strip())
    if p is None:
        names = ", ".join(q.name for q in registry.personalities if q.formats)
        raise FormatError(f"format {written!r} is unknown: expected ntfs, efi, ext4 or one of diskutil's: {names}")
    if not p.formats:
        raise FormatError(f"format {written!r} is a partition type without a file system; not supported")
    if p.content_mask is None:
        raise FormatError(f"format {written!r}: its bundle names no content mask, the partition type is unknown")
    guid = MASK_TYPES.get(p.content_mask)
    if guid is None:
        raise FormatError(
            f"format {written!r}: the partition type diskutil gives content {p.content_mask!r} was not measured"
        )
    if p.bundle not in PROMPTS:
        raise FormatError(
            f"format {written!r}: whether diskutil asks on stdin with bundle {p.bundle!r} was not measured"
        )
    return Format(PERSONALITY, written, guid, p, p.min_size, p.max_size, PROMPTS[p.bundle])
