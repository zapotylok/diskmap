"""Probe → core.model.layout.Disk: the partition map of one whole disk (T-020, K7).

ioreg gives offsets, sizes and raw types; `diskutil list -plist diskN` gives the type
and volume names. The two must describe the same disk — any disagreement stops.
The usable area is computed (see Disk); a partition outside it or overlapping another
means the assumptions do not hold for this disk (§S9). Block alignment is ioreg's check.
APFS containers come from `diskutil apfs list` (T-022), file systems and mount points
from `diskutil info` of every partition and APFS volume (T-021).

Shown by `diskmap show diskN` (cli/show.py, T-030).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from core.model import gpt_types
from core.model.apfs import ApfsContainer
from core.model.layout import GPT, MBR, Disk, Partition, Unpartitioned, gaps
from core.probe import ioreg
from core.probe.apfs import parse_apfs, read_apfs
from core.probe.diskutil import DuDisk, DuPartition, parse_list, read_list
from core.probe.errors import UnfamiliarInput
from core.probe.ioreg import IoDisk, parse_disk, read_iomedia
from core.probe.volume import APFS, DuSliceInfo, parse_info, read_info

GPT_ENTRY_TABLE_BYTES = 128 * 128


def read_layout(bsd_name: str) -> Disk | Unpartitioned:
    """Probe whole disk BSD_NAME now, APFS containers and file systems included. Read-only."""
    disk = attach_apfs(build_layout(read_iomedia(), read_list(bsd_name), bsd_name), parse_apfs(read_apfs()))
    return attach_volumes(disk, {s: read_info(s) for s in slices(disk)})


def slices(disk: Disk | Unpartitioned) -> list[str]:
    """Every partition and every volume of their APFS containers: what `diskutil info` is read for.
    A disk without a partition table: the disk itself."""
    if isinstance(disk, Unpartitioned):
        return [disk.bsd_name]
    out = []
    seen = set()
    for p in disk.partitions:
        out.append(p.bsd_name)
        if p.apfs is not None and p.apfs.reference not in seen:
            seen.add(p.apfs.reference)
            out.extend(v.bsd_name for v in p.apfs.volumes)
    return out


def attach_volumes(disk: Disk | Unpartitioned, infos: Mapping[str, Any]) -> Disk | Unpartitioned:
    """Hang `diskutil info` of each slice on its partition or APFS volume (T-021).

    INFOS (raw plists) must cover exactly slices(disk), and each must place its slice
    where the map has it: parent disk, offset, size, type name, label, container.
    Any disagreement means the sources describe different states of the disk — a stop.
    """
    if sorted(infos) != sorted(slices(disk)):
        raise UnfamiliarInput(f"{disk.bsd_name}: diskutil info was not read for exactly its partitions and volumes")
    parsed = {name: parse_info(raw, name) for name, raw in infos.items()}
    if isinstance(disk, Unpartitioned):
        i = parsed[disk.bsd_name]
        placed = (i.parent, i.partition_offset, i.size, i.content, i.volume.label)
        if placed != (disk.bsd_name, None, disk.size, ioreg.NO_SCHEME, disk.volume_name):
            raise UnfamiliarInput(f"{disk.bsd_name}: diskutil info {placed} disagrees with the map")
        return replace(disk, volume=i.volume)
    bs = disk.block_size
    containers: dict[str, ApfsContainer] = {}
    partitions = []
    for p in disk.partitions:
        i = parsed[p.bsd_name]
        placed = (i.parent, i.partition_offset, i.size, i.content, i.volume.label)
        if placed != (disk.bsd_name, p.start_lba * bs, p.blocks * bs, p.type_name, p.volume_name):
            raise UnfamiliarInput(f"{p.bsd_name}: diskutil info {placed} disagrees with the map")
        # info names the container only on Apple_APFS partitions, not on the ISC and
        # Recovery stores of an internal disk: apfs list stays the source (D11).
        if i.container is not None and i.container != (p.apfs.reference if p.apfs is not None else None):
            raise UnfamiliarInput(f"{p.bsd_name}: diskutil info names APFS container {i.container}")
        p = replace(p, volume=i.volume)
        if p.apfs is not None:
            ref = p.apfs.reference
            if ref not in containers:
                containers[ref] = _attach_container(p.apfs, parsed)
            p = replace(p, apfs=containers[ref])
        partitions.append(p)
    return replace(disk, partitions=tuple(partitions))


def _attach_container(c: ApfsContainer, parsed: Mapping[str, DuSliceInfo]) -> ApfsContainer:
    volumes = []
    for v in c.volumes:
        i = parsed[v.bsd_name]
        if (i.parent, i.container, i.volume.filesystem, i.volume.label) != (c.reference, c.reference, APFS, v.name):
            raise UnfamiliarInput(f"{v.bsd_name}: diskutil info disagrees with apfs list on the volume")
        volumes.append(replace(v, volume=i.volume))
    return replace(c, volumes=tuple(volumes))


def attach_apfs(disk: Disk | Unpartitioned, containers: tuple[ApfsContainer, ...]) -> Disk | Unpartitioned:
    """Hang every APFS container on the partition that is its physical store (T-022).

    A partition claimed by two containers, or a store whose size differs from the
    partition's, means the sources describe different disks — a stop (§S9). A
    container spanning several partitions (Fusion) hangs on each of them.
    """
    if isinstance(disk, Unpartitioned):
        return disk  # a physical store is always a partition
    by_store: dict[str, ApfsContainer] = {}
    for c in containers:
        for store in c.physical_stores:
            if store.bsd_name in by_store:
                raise UnfamiliarInput(f"{store.bsd_name}: physical store of two APFS containers")
            by_store[store.bsd_name] = c
    partitions = []
    for p in disk.partitions:
        c = by_store.get(p.bsd_name)
        if c is not None:
            [store] = [s for s in c.physical_stores if s.bsd_name == p.bsd_name]
            if store.size != p.blocks * disk.block_size:
                raise UnfamiliarInput(f"{p.bsd_name}: APFS physical store size {store.size} differs from the partition")
            p = replace(p, apfs=c)
        partitions.append(p)
    return replace(disk, partitions=tuple(partitions))


def build_layout(ioreg_roots: Any, diskutil_plist: Any, bsd_name: str) -> Disk | Unpartitioned:
    """Pure part of read_layout(): raw tool plists in, Disk (or Unpartitioned) out."""
    io = parse_disk(ioreg_roots, bsd_name)
    du = parse_list(diskutil_plist, bsd_name)
    du_parts = _cross_check(io, du)
    if io.scheme == ioreg.NO_SCHEME:
        return Unpartitioned(bsd_name, io.size, io.block_size, du.volume_name)
    if du.volume_name is not None:
        raise UnfamiliarInput(f"{bsd_name}: diskutil names a volume on a disk with a partition table")

    bs = io.block_size
    first, last = _usable_lbas(io)
    partitions = []
    prev_end = first - 1
    for p in io.partitions:
        part = Partition(
            bsd_name=p.bsd_name,
            partition_id=p.partition_id,
            start_lba=p.base // bs,
            blocks=p.size // bs,
            type=p.content,
            type_name=du_parts[p.bsd_name].content,
            volume_name=du_parts[p.bsd_name].volume_name,
            gpt_attributes=p.gpt_attributes,
            mbr_type=_mbr_type(io, p) if io.scheme == ioreg.MBR else None,
        )
        known = gpt_types.known(part.type) if io.scheme == ioreg.GPT else None
        if known is not None and known.name != part.type_name:
            # The table was checked against diskutil (T-025); a rename is a macOS change.
            raise UnfamiliarInput(
                f"{p.bsd_name}: diskutil calls type {part.type} {part.type_name!r}, not {known.name!r}"
            )
        if part.start_lba < first or part.end_lba > last:
            # The standard-table assumption does not hold for this disk: a derived
            # range that contradicts the real map must not be published (§S9).
            raise UnfamiliarInput(
                f"{p.bsd_name}: LBA {part.start_lba}..{part.end_lba} outside computed usable {first}..{last}"
            )
        if part.start_lba <= prev_end:
            raise UnfamiliarInput(f"{p.bsd_name}: overlaps the previous partition")
        prev_end = part.end_lba
        partitions.append(part)

    return Disk(
        bsd_name=bsd_name,
        scheme=GPT if io.scheme == ioreg.GPT else MBR,
        size=io.size,
        block_size=bs,
        first_usable_lba=first,
        last_usable_lba=last,
        partitions=tuple(partitions),
        free=gaps(first, last, [(p.start_lba, p.end_lba) for p in partitions]),
    )


def _cross_check(io: IoDisk, du: DuDisk) -> dict[str, DuPartition]:
    """ioreg and diskutil must describe the same disk; disagreement is a stop."""
    if du.size != io.size or du.content != io.scheme:
        raise UnfamiliarInput(f"{io.bsd_name}: diskutil and ioreg disagree on the whole disk")
    du_parts = {p.bsd_name: p for p in du.partitions}
    if sorted(du_parts) != sorted(p.bsd_name for p in io.partitions):
        raise UnfamiliarInput(f"{io.bsd_name}: diskutil and ioreg list different partitions")
    for p in io.partitions:
        d = du_parts[p.bsd_name]
        if d.size != p.size or d.disk_uuid != p.uuid:
            raise UnfamiliarInput(f"{p.bsd_name}: diskutil and ioreg disagree on size or partition GUID")
    return du_parts


def _usable_lbas(io: IoDisk) -> tuple[int, int]:
    lbas = io.size // io.block_size
    if io.scheme == ioreg.GPT:
        table = -(-GPT_ENTRY_TABLE_BYTES // io.block_size)  # ceil
        # LBA 0 protective MBR, LBA 1 header, then the table; mirrored at the end.
        return 2 + table, lbas - 2 - table
    return 1, min(lbas, 2**32) - 1


def _mbr_type(io: IoDisk, p: ioreg.IoPartition) -> int:
    assert io.mbr_type_names is not None
    if p.content not in io.mbr_type_names:
        raise UnfamiliarInput(f"{p.bsd_name}: MBR type {p.content!r} is not in the kernel's Content Table")
    return io.mbr_type_names[p.content]
