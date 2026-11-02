"""Partition layout of one whole disk: partitions and the free space between them (T-020).

Pure data, no probing. Positions are in logical blocks of the disk (`block_size`
bytes); byte values are derived. Built by core.probe.layout.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.model.apfs import ApfsContainer
from core.model.volume import VolumeInfo

GPT = "GPT"
MBR = "MBR"


@dataclass(frozen=True)
class Partition:
    bsd_name: str
    partition_id: int  # GPT entry index / MBR slot, 1-based
    start_lba: int
    blocks: int
    type: str  # GPT: raw type GUID; MBR: the kernel's name for the type byte
    type_name: str  # diskutil's name for the type
    volume_name: str | None
    gpt_attributes: int | None  # GPT only
    mbr_type: int | None  # MBR only; computed from the kernel's table (D8)
    apfs: ApfsContainer | None = None  # the container this partition is a physical store of
    volume: VolumeInfo | None = None  # its file system (T-021); None until attached

    @property
    def end_lba(self) -> int:
        """Last block, inclusive."""
        return self.start_lba + self.blocks - 1


@dataclass(frozen=True)
class FreeSpace:
    """Blocks inside the usable area that no partition covers. Every gap counts,
    alignment slack included: whether a gap is worth showing is the UI's call."""

    start_lba: int
    blocks: int

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.blocks - 1


@dataclass(frozen=True)
class Disk:
    bsd_name: str
    scheme: str  # GPT or MBR
    size: int  # bytes
    block_size: int
    # Computed, not read (K7): GPT assumes the standard 128 x 128-byte entry table at
    # both ends; MBR is bounded by LBA 0 and 32-bit LBA fields.
    first_usable_lba: int
    last_usable_lba: int
    partitions: tuple[Partition, ...]  # in LBA order, non-overlapping, inside usable
    free: tuple[FreeSpace, ...]  # in LBA order

    @property
    def blocks(self) -> int:
        return self.size // self.block_size

    def extents(self) -> tuple[Partition | FreeSpace, ...]:
        """Partitions and free space in LBA order: the usable area, tiled exactly."""
        return tuple(sorted((*self.partitions, *self.free), key=lambda e: e.start_lba))


def gaps(first: int, last: int, taken: list[tuple[int, int]]) -> tuple[FreeSpace, ...]:
    """Free extents of FIRST..LAST (inclusive) around sorted, disjoint (start, end) ranges."""
    out = []
    cursor = first
    for start, end in taken:
        if start > cursor:
            out.append(FreeSpace(cursor, start - cursor))
        cursor = end + 1
    if cursor <= last:
        out.append(FreeSpace(cursor, last - cursor + 1))
    return tuple(out)


@dataclass(frozen=True)
class Unpartitioned:
    """A whole disk without a partition table: blank, or one file system over all of
    it (a "superfloppy"). Not a Disk with no partitions: there is no usable area to
    compute, and a file system here is occupied space, not free (T-030, D17)."""

    bsd_name: str
    size: int  # bytes
    block_size: int
    volume_name: str | None  # diskutil list's, as on Partition
    volume: VolumeInfo | None = None  # the whole disk's `diskutil info` (T-021); None until attached
    apfs: ApfsContainer | None = None  # a container whose physical store is the whole disk (K20)

    @property
    def blocks(self) -> int:
        return self.size // self.block_size
