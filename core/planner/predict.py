"""Predict the partition map `diskutil partitionDisk` will produce (T-041, D21).

Pure arithmetic, no probing. The input is what a plan hands diskutil — per slot the
partition type it will get and an exact size in bytes — plus the disk's geometry. The
output is every partition that will exist afterwards, in order, with exact LBA bounds,
including the ones diskutil adds on its own (EFI, Apple_Boot).

Every rule below was measured on the rig (macOS 26.6.2, arm64, 512-byte images,
2026-09-27; corpus in tests/data/partition/). A request outside what was measured is
refused with Unpredictable rather than guessed (rules.md §S9): a preview that guesses
is worse than a planner that says "not supported".

GPT, default mode (no -noEFI):
- EFI (200 MiB at LBA 40) is added when the usable area is at least 2 GiB.
- A slot starts at the next 4 KiB boundary; Microsoft basic data at the next MiB, and
  its size is rounded down to whole MiB. Other sizes are rounded up to 4 KiB.
- A slot of 1 GiB or more acquires a 128 MiB booter unless its type is APFS or MS basic
  data. Above 1 GiB the booter is carved from the slot; at exactly 1 GiB it is added
  after it. For HFS+ the booter is only space: carved, it is simply lost to the slot;
  added, it stays a gap. For other types it is a real Apple_Boot partition.
GPT with -noEFI: no EFI, no booters, 4 KiB alignment for every type.
Both: positions count from LBA 0 (a leading Free Space of 1 MiB puts the first slot at
LBA 2048); the last slot runs to the end of the usable area, rounded down to its
alignment, whatever size it asked for.
MBR: a disk of at most 1 GiB starts at LBA 1 and aligns later slots to 2 sectors;
a larger one aligns every slot to 1 MiB. Sizes round up to 4 KiB; the last slot runs
to the last sector. APFS and untyped (%noformat%) slots are refused by diskutil.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.model import gpt_types as T
from core.model.layout import GPT, MBR

BLOCK = 512  # the only block size the rig can make (no 4Kn images, see testrig/fixture.sh)
KIB4 = 4096 // BLOCK
MIB = 2**20 // BLOCK
GIB = 2**30 // BLOCK
EFI_BLOCKS = 200 * MIB
BOOTER_BLOCKS = 128 * MIB
EFI_MIN_USABLE = 2 * GIB

# %noformat% without a type: diskutil writes this in place of a type GUID.
UNTYPED = "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF"

# MBR types, as the kernel names them (layout.Partition.type), that were cut on the rig.
MBR_TYPES = {"Windows_NTFS", "DOS_FAT_32", "Apple_HFS"}

# Booter behaviour of a slot of 1 GiB or more, per type, as measured in default mode.
NO_BOOTER = "none"
BOOTER_SPACE = "space"  # HFS+
BOOTER_PARTITION = "partition"
_BOOTER = {
    T.APPLE_APFS.guid: NO_BOOTER,
    T.MS_BASIC_DATA.guid: NO_BOOTER,
    T.APPLE_HFS.guid: BOOTER_SPACE,
    T.LINUX_FS.guid: BOOTER_PARTITION,
    UNTYPED: BOOTER_PARTITION,
}
_UNKNOWN_BOOTER = BOOTER_PARTITION  # a GUID diskmap has no name for (measured with one)

# Types measured with -noEFI.
# EFI: an explicit ESP slot when the user asks for one (K9); 4 KiB like the rest.
_NOEFI_TYPES = {T.EFI.guid, T.APPLE_APFS.guid, T.MS_BASIC_DATA.guid, T.APPLE_HFS.guid, T.LINUX_FS.guid, UNTYPED}

# diskutil silently dropped a BIOS boot slot after Linux RAID and after Linux filesystem;
# it was created right after EFI and after MS basic data.
_BIOS_BOOT_AFTER = {T.EFI.guid, T.MS_BASIC_DATA.guid}

ADDED_EFI = "EFI"
ADDED_BOOTER = "booter"


class Unpredictable(Exception):
    """diskutil's result for this request was not measured; the planner must not emit it."""


@dataclass(frozen=True)
class Slot:
    type: str | None  # GPT: type GUID; MBR: kernel type name; None: Free Space
    size: int | None  # bytes; None: 0b, all that is left — the last slot only


@dataclass(frozen=True)
class Request:
    scheme: str  # GPT or MBR
    slots: tuple[Slot, ...]
    no_efi: bool = False  # -noEFI; GPT only


@dataclass(frozen=True)
class Predicted:
    start_lba: int
    blocks: int
    type: str
    slot: int | None  # index into Request.slots; None when diskutil adds it
    added: str | None = None  # ADDED_EFI / ADDED_BOOTER

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.blocks - 1


def predict(req: Request, blocks: int, block_size: int) -> tuple[Predicted, ...]:
    if block_size != BLOCK:
        raise Unpredictable(f"block size {block_size}: only {BLOCK}-byte disks were measured")
    if not req.slots:
        raise Unpredictable("no slots")
    for i, s in enumerate(req.slots):
        if s.size is None and i != len(req.slots) - 1:
            raise Unpredictable(f"slot {i + 1}: a remainder is measured only as the last slot")
        if s.size is not None and s.size <= 0:
            raise Unpredictable(f"slot {i + 1}: size {s.size}")
    if req.scheme == GPT:
        return _gpt(req, blocks)
    if req.scheme == MBR:
        return _mbr(req, blocks)
    raise Unpredictable(f"scheme {req.scheme!r}")


def _up(n: int, a: int) -> int:
    return -(-n // a) * a


def _down(n: int, a: int) -> int:
    return n // a * a


def _blocks(nbytes: int) -> int:
    return -(-nbytes // BLOCK)


def _gpt(req: Request, blocks: int) -> tuple[Predicted, ...]:
    first, last = 34, blocks - 34  # 128 x 128-byte entries at both ends, as layout.Disk
    out: list[Predicted] = []
    cursor = 0
    if not req.no_efi and last - first + 1 >= EFI_MIN_USABLE:
        efi = Predicted(_up(first, KIB4), EFI_BLOCKS, T.EFI.guid, None, ADDED_EFI)
        out.append(efi)
        cursor = efi.start_lba + efi.blocks
    n = len(req.slots)
    for i, s in enumerate(req.slots):
        where = f"slot {i + 1}"
        is_last = i == n - 1
        if s.type is None:
            if is_last and s.size is None:
                break
            if is_last or not req.no_efi:
                raise Unpredictable(f"{where}: Free Space is measured only as the tail, or between slots with -noEFI")
            if s.size % 4096:
                raise Unpredictable(f"{where}: Free Space of {s.size} bytes is not a multiple of 4 KiB")
            cursor += s.size // BLOCK
            continue
        _check_gpt_type(req, i, s.type, out)
        align = MIB if (s.type == T.MS_BASIC_DATA.guid and not req.no_efi) else KIB4
        start = _up(max(cursor, first), align)
        if is_last:
            size = _down(last + 1, align) - start
        elif align == MIB:
            size = _down(_blocks(s.size), MIB)
        else:
            size = _up(_blocks(s.size), KIB4)
        booter = NO_BOOTER if req.no_efi else _BOOTER.get(s.type, _UNKNOWN_BOOTER)
        gap = 0
        if booter != NO_BOOTER and size >= GIB:
            if size == GIB:
                if is_last:
                    raise Unpredictable(f"{where}: a last slot of exactly 1 GiB was not measured")
                gap = BOOTER_BLOCKS
            else:
                size -= BOOTER_BLOCKS
                gap = BOOTER_BLOCKS if booter == BOOTER_PARTITION else 0
        if size <= 0 or start + size - 1 > last:
            raise Unpredictable(f"{where}: does not fit ({size} blocks at LBA {start}, usable up to {last})")
        out.append(Predicted(start, size, s.type, i))
        cursor = start + size
        if gap and booter == BOOTER_PARTITION:
            out.append(Predicted(cursor, BOOTER_BLOCKS, T.APPLE_BOOT.guid, None, ADDED_BOOTER))
        cursor += gap
        if cursor - 1 > last:
            raise Unpredictable(f"{where}: its booter does not fit")
    if len(out) > 128:
        raise Unpredictable(f"{len(out)} partitions, a GPT holds 128")
    return tuple(out)


def _check_gpt_type(req: Request, i: int, guid: str, before: list[Predicted]) -> None:
    where = f"slot {i + 1}"
    if req.no_efi:
        if guid not in _NOEFI_TYPES:
            raise Unpredictable(f"{where}: type {guid} was not measured with -noEFI")
        return
    if guid == T.EFI.guid and before and before[0].added == ADDED_EFI:
        raise Unpredictable(f"{where}: an EFI slot on a disk that gets diskutil's own EFI was not measured")
    if guid == T.BIOS_BOOT.guid and (not before or before[-1].type not in _BIOS_BOOT_AFTER):
        raise Unpredictable(
            f"{where}: diskutil silently drops a BIOS boot slot that does not follow EFI or MS basic data"
        )
    size = req.slots[i].size
    big = size is None or _blocks(size) >= GIB
    if big and guid in T.TYPES and guid not in _BOOTER:
        raise Unpredictable(f"{where}: a {T.TYPES[guid].name} slot of 1 GiB or more was not measured (booter rule)")


def _mbr(req: Request, blocks: int) -> tuple[Predicted, ...]:
    if req.no_efi:
        raise Unpredictable("-noEFI with MBR: diskmap does not emit it")
    if blocks * BLOCK > 2**41:
        raise Unpredictable("MBR on a disk over 2 TiB was not measured")
    parts = [s for s in req.slots if s.type is not None]
    if len(parts) > 4:
        raise Unpredictable(f"{len(parts)} partitions: only primary MBR partitions were measured")
    small = blocks <= GIB
    last = blocks - 1
    out: list[Predicted] = []
    cursor = 0
    n = len(req.slots)
    for i, s in enumerate(req.slots):
        where = f"slot {i + 1}"
        is_last = i == n - 1
        if s.type is None:
            if is_last and s.size is None:
                break
            raise Unpredictable(f"{where}: Free Space on MBR is measured only as the tail")
        if s.type not in MBR_TYPES:
            raise Unpredictable(f"{where}: MBR type {s.type!r} was not measured")
        if small:
            start = 1 if not out else _up(cursor, 2)
        else:
            start = _up(max(cursor, 1), MIB)
        size = last + 1 - start if is_last else _up(_blocks(s.size), KIB4)
        if size <= 0 or start + size - 1 > last:
            raise Unpredictable(f"{where}: does not fit ({size} blocks at LBA {start}, disk ends at {last})")
        out.append(Predicted(start, size, s.type, i))
        cursor = start + size
    return tuple(out)


# --- addPartition (T-043, D25) -------------------------------------------------------
#
# `diskutil addPartition <disk> <format> <name> <size>`, the whole disk as target: the new
# partition goes after the last one, into the free tail. Measured on the rig 2026-09-27
# (corpus tests/data/addpartition/). There is no -noEFI here, and it shows:
# - a slot starts at the next 4 KiB boundary; Microsoft basic data at the next MiB, and it
#   ends at the MiB boundary at or below (previous end + size) — the end, not the size, is
#   rounded, counting from where the previous partition ends;
# - an HFS+ personality (any of the family) asked for more than 1 GiB gets 128 MiB less:
#   booter space. If what is left is still at least 1 GiB, that space stays reserved and the
#   next partition starts after it; otherwise the next starts right after the HFS+ slot.
#   Exactly 1 GiB is not cut. ExFAT, FAT32, APFS, UFSD NTFS and UFSD ext4 are never cut;
# - a size that does not fit the tail is refused (-5344), never put elsewhere. Aimed at a
#   slice instead of the disk, diskutil does put it elsewhere, and a slot in a gap between
#   partitions renumbers the ones after it (D24): the planner aims at the disk only.
# A blank %GUID% %noformat% slot is measured only for MS basic data (no booter), EFI up
# to 1 GiB and Linux filesystem under 1 GiB (2026-10-17, T-053: 4 KiB start, exact size).
# A blank Linux slot of exactly 1 GiB is followed by a real 128 MiB Apple_Boot; at 1 GiB +
# 4 KiB it also loses 128 MiB to it — not emitted, refused.


@dataclass(frozen=True)
class Add:
    type: str  # GPT type GUID the slot gets
    size: int  # bytes, a multiple of 4 KiB
    formatted: bool  # a personality formats it; False: a %GUID% %noformat% slot


_ADD_FORMATTED = {T.APPLE_HFS.guid, T.MS_BASIC_DATA.guid, T.LINUX_FS.guid, T.APPLE_APFS.guid}
_ADD_BLANK = {T.MS_BASIC_DATA.guid, T.EFI.guid, T.LINUX_FS.guid}


def predict_add(
    after_lba: int, adds: tuple[Add, ...], blocks: int, block_size: int, before_lba: int | None = None
) -> tuple[Predicted, ...]:
    """The partitions ADDS create, in order, on a GPT disk of BLOCKS, in the free space from
    AFTER_LBA (where the partition before it ends) to BEFORE_LBA (where the next one starts;
    None: the tail, to the end of the usable area). Partitions already there are not
    returned: they keep their places (in a gap, not their numbers — D24, D50)."""
    if block_size != BLOCK:
        raise Unpredictable(f"block size {block_size}: only {BLOCK}-byte disks were measured")
    last = blocks - 34 if before_lba is None else before_lba - 1
    cursor = after_lba
    out: list[Predicted] = []
    for i, a in enumerate(adds):
        where = f"partition {i + 1}"
        if a.size <= 0 or a.size % 4096:
            raise Unpredictable(f"{where}: size {a.size} is not a positive multiple of 4 KiB")
        n = a.size // BLOCK
        if a.formatted and a.type not in _ADD_FORMATTED:
            raise Unpredictable(f"{where}: type {a.type} was not measured with addPartition")
        if not a.formatted and (
            a.type not in _ADD_BLANK or (a.type == T.EFI.guid and n > GIB) or (a.type == T.LINUX_FS.guid and n >= GIB)
        ):
            raise Unpredictable(f"{where}: a blank {a.type} slot of this size was not measured with addPartition")
        if a.type == T.MS_BASIC_DATA.guid:
            start = _up(cursor, MIB)
            size = _down(cursor + n, MIB) - start
            reach, nxt = start + n, start + size
        else:
            start = _up(cursor, KIB4)
            size, reach, nxt = n, start + n, start + n
            if a.formatted and a.type == T.APPLE_HFS.guid and n > GIB:
                size = n - BOOTER_BLOCKS
                nxt = start + (n if size >= GIB else size)
        # What diskutil checks against the tail was seen only with room to spare or none:
        # the whole request, from the aligned start, must fit.
        if size <= 0 or reach - 1 > last:
            if before_lba is not None:
                # diskutil puts it in the tail, rc=0, or trims an MS basic data slot (rig, D50).
                raise Unpredictable(
                    f"{where}: does not fit the gap ({n} blocks at LBA {start}, the gap ends at {last})"
                )
            raise Unpredictable(f"{where}: does not fit ({n} blocks at LBA {start}, usable up to {last})")
        out.append(Predicted(start, size, a.type, i))
        cursor = nxt
    return tuple(out)


# --- resizeVolume / apfs resizeContainer (T-044, D26) ----------------------------------
#
# `diskutil resizeVolume <slice> <size> [<format> <name> <size>]…` for HFS+ and
# `diskutil apfs resizeContainer <slice> <size> […]` for an APFS physical store, the
# slice being the last partition. Measured on the rig 2026-10-02 (corpus tests/data/resize/):
# - the partition keeps its start and gets exactly the size asked (a multiple of 4 KiB);
# - after an HFS+ partition of 1 GiB or more, 128 MiB of booter space stays free and the
#   new partitions start after it (1 GiB - 4 KiB: none); after APFS, none;
# - the new partitions are then placed exactly as addPartition places them (predict_add);
# - apfs resizeContainer refuses %noformat%: no blank slots after an APFS store;
# - apfs resizeContainer refuses new partitions whose sizes add up to more than the store
#   is now (-69771 "target disk is too small"), however much free space follows it: equal
#   passes, 4 KiB more fails. resizeVolume has no such limit;
# - with a partition after the shrunk one, a new partition lands between them and the
#   ones after it are renumbered (Live=false, D24): not predicted, refused.

RESIZE_HFS = "hfs"
RESIZE_APFS = "apfs"


def predict_resize(
    start_lba: int, current: int, kind: str, new_size: int, adds: tuple[Add, ...], blocks: int, block_size: int
) -> tuple[Predicted, tuple[Predicted, ...]]:
    """The partition at START_LBA, CURRENT bytes and of KIND (RESIZE_HFS or RESIZE_APFS),
    resized to NEW_SIZE bytes, and the partitions ADDS create after it (a shrink of the last one).
    Whether a grow fits is diskutil's MaximumSize (resize limits), not decided here (T-074)."""
    if block_size != BLOCK:
        raise Unpredictable(f"block size {block_size}: only {BLOCK}-byte disks were measured")
    if kind not in (RESIZE_HFS, RESIZE_APFS):
        raise Unpredictable(f"resizing {kind!r} was not measured")
    if new_size <= 0 or new_size % 4096:
        raise Unpredictable(f"new size {new_size} is not a positive multiple of 4 KiB")
    if adds and new_size > current:
        raise Unpredictable("diskutil creates no partitions with a grow (-69604)")
    if kind == RESIZE_APFS and any(not a.formatted for a in adds):
        raise Unpredictable("apfs resizeContainer refuses %noformat%: no blank slot after an APFS store")
    if kind == RESIZE_APFS and sum(a.size for a in adds) > current:
        raise Unpredictable(
            f"apfs resizeContainer refuses new partitions of more than {current} bytes in all, the store's size now"
        )
    n = new_size // BLOCK
    shrunk = Predicted(start_lba, n, T.APPLE_HFS.guid if kind == RESIZE_HFS else T.APPLE_APFS.guid, None)
    return shrunk, predict_add(resize_cursor(start_lba, kind, n), adds, blocks, block_size) if adds else ()


def grow_edge(start_lba: int, kind: str, end_lba: int) -> int:
    """The most bytes a partition at START_LBA of KIND can be grown to when the free space after it
    ends at END_LBA (the next partition, or past the last usable block), as measured (T-074, D48):
    to END_LBA, less 128 MiB of booter space for HFS+, in whole 4 KiB from its start. diskutil's
    R and 0 grow to exactly this, and an explicit size this large passes (HFS+ took 4 KiB more too:
    the booter space is not a hard limit; diskmap does not go into it). The
    MaximumSize of its resize limits is up to a MiB less (the end rounded down to a MiB)."""
    if kind not in (RESIZE_HFS, RESIZE_APFS):
        raise Unpredictable(f"resizing {kind!r} was not measured")
    end = end_lba - (BOOTER_BLOCKS if kind == RESIZE_HFS else 0)
    return max(0, (end - start_lba) // KIB4 * KIB4) * BLOCK


def resize_cursor(start_lba: int, kind: str, n: int) -> int:
    """Where the new partitions are placed from, after a partition of N blocks shrunk in place."""
    return start_lba + n + (BOOTER_BLOCKS if kind == RESIZE_HFS and n >= GIB else 0)
