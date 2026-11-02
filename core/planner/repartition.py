"""Strategy "full repartition": the whole disk becomes the desired layout (T-042, K9, D22).

One `diskutil partitionDisk -noEFI <disk> GPT …` with exact geometry: a leading
`Free Space 1Mi` puts the first partition at LBA 2048, every slot gets its size in
bytes (a multiple of 4 KiB), and what is left at the end is an explicit `Free Space`.
With -noEFI diskutil adds nothing and aligns nothing further (D21), so the plan's
geometry is the result by construction — and the prediction is asked anyway: the
plan is refused if predict() disagrees with what was computed here.

Capacity for percentages and `rest` is LBA 2048 up to the end of the GPT usable area,
rounded down to 4 KiB. Fixed sizes are rounded up to 4 KiB, as diskutil would;
percentages and `rest` down. The preview shows the resulting bytes (T-045).

Formats diskutil cannot make in partitionDisk (ntfs, efi — formats.py) get a blank
slot of the right type in the same command and a step of their own afterwards. Their
slice is known because -noEFI numbers the new partitions 1..N in order; the step
still checks it by place and type before it writes (SliceIs).

Before it: the map backup (core.planner.backup, K11) and `diskutil unmountDisk` (T-011) —
partitionDisk unmounts by itself, but with a file held open it fails without saying who
holds it; the explicit step names the process and stops before anything is written.

Runs no commands (§S3): the current layout, the file system registry, the tool paths
and the backup folder are inputs.
"""

from __future__ import annotations

from core.model.apfs import ApfsContainer
from core.model.desired import DesiredLayout, human
from core.model.filesystems import Registry
from core.model.layout import GPT, Disk, Unpartitioned
from core.model.volume import VolumeInfo
from core.planner import formats as F
from core.planner.backup import backup_notes, backup_steps
from core.planner.common import (
    DISKUTIL,
    PlanError,
    Tools,
    byte_sizes,
    check_size_limits,
    closed_for,
    extent,
    format_all,
    formats_of,
    fs_name,
    layout_of,
    makes,
    mounting_lines,
    size_arg,
    triple,
    unmount_step,
)
from core.planner.plan import (
    DiskAvailable,
    DiskUnmounted,
    ExecutionPlan,
    LayoutIs,
    Loss,
    PlannedPartition,
    Step,
)
from core.planner.predict import BLOCK, KIB4, MIB, Predicted, Request, Slot, Unpredictable, predict
from core.safety.guard import AvailableDisk

STRATEGY = "repartition"
FREE = "Free Space"  # partitionDisk's word for a gap (K9); listed as a personality, formats nothing
LEAD_BLOCKS = MIB  # first partition at LBA 2048
GPT_ENTRY_BLOCKS = 34  # protective MBR + header + 128 x 128-byte entries (as layout.Disk)


def plan_repartition(
    target: AvailableDisk,
    current: Disk | Unpartitioned,
    desired: DesiredLayout,
    registry: Registry,
    tools: Tools,
    folder: str,
) -> ExecutionPlan:
    """FOLDER: where the plan's first steps back up the map (core.planner.backup)."""
    if not isinstance(target, AvailableDisk):
        raise TypeError("the planner accepts only a disk the guard issued (rules.md §S8)")
    disk = target.bsd_name
    if current.bsd_name != disk:
        raise PlanError(f"layout of {current.bsd_name} given for {disk}")
    if current.block_size != BLOCK:
        raise PlanError(f"{disk}: {current.block_size}-byte blocks; only {BLOCK}-byte disks were measured")

    fmts = formats_of(desired, registry, tools)

    blocks = current.size // BLOCK
    area_end = (blocks - GPT_ENTRY_BLOCKS + 1) // KIB4 * KIB4  # exclusive, 4 KiB aligned
    if area_end <= LEAD_BLOCKS:
        raise PlanError(f"{disk} is too small to lay out: {human(current.size)}")
    capacity = (area_end - LEAD_BLOCKS) * BLOCK
    sizes = byte_sizes(desired, capacity)
    for n, (b, f) in enumerate(zip(sizes, fmts), 1):
        check_size_limits(n, f, b)

    tail = capacity - sum(sizes)
    slots = [Slot(None, LEAD_BLOCKS * BLOCK), *(Slot(f.type, b) for f, b in zip(fmts, sizes))]
    if tail:
        slots.append(Slot(None, None))
    try:
        after = predict(Request(GPT, tuple(slots), no_efi=True), blocks, BLOCK)
    except Unpredictable as e:
        raise PlanError(f"diskutil's result is not predictable: {e}") from None
    _check_agrees(after, fmts, sizes)

    argv: list[str] = [DISKUTIL, "partitionDisk", "-noEFI", disk, GPT, FREE, "gap", size_arg(LEAD_BLOCKS * BLOCK)]
    for p, f, b in zip(desired.partitions, fmts, sizes):
        argv += triple(f, p.label, b)
    if tail:
        argv += [FREE, "free", "0b"]

    before = layout_of(current)
    planned = tuple(
        PlannedPartition(p.format, p.label, p.size.text, makes(f), f"{disk}s{i}", q)
        for i, (p, f, q) in enumerate(zip(desired.partitions, fmts, after), 1)
    )
    steps, backed = backup_steps(current, folder)
    steps += [
        unmount_step(disk),
        Step(
            intent=f"repartition {disk} as GPT with {len(planned)} partition(s)",
            command=tuple(argv),
            needs_root=False,
            writes_disk=True,
            stdin=closed_for(fmts),  # diskutil answers a Paragon formatter's y/n itself (D22, D27)
            preflight=(DiskAvailable(disk), before, DiskUnmounted(disk), *backed),
            postflight=(LayoutIs(disk, GPT, tuple(extent(q) for q in after)),),
            rollback=None,
            destroys=_destroys(current),
        ),
    ]
    made, format_notes = format_all(
        planned, fmts, disk, tools, backed, LayoutIs(disk, GPT, tuple(extent(q) for q in after))
    )
    steps += made

    notes = format_notes + backup_notes(current, folder)
    if not any(f.kind == F.EFI for f in fmts):
        notes.append('No EFI partition is created. To add one, put {"format": "efi", "size": "200Mi"} first.')
    if tail:
        notes.append(f"{human(tail)} at the end stays free.")
    return ExecutionPlan(
        STRATEGY,
        disk,
        blocks,
        BLOCK,
        before,
        after,
        planned,
        tuple(steps),
        tuple(notes),
        mounting=tuple(mounting_lines(planned, fmts, tools.trial)),
    )


def _check_agrees(after: tuple[Predicted, ...], fmts: list[F.Format], sizes: list[int]) -> None:
    """The geometry computed here and predict()'s must be the same, partition for partition."""
    cursor = LEAD_BLOCKS
    ok = len(after) == len(fmts)
    for q, f, b in zip(after, fmts, sizes):
        ok = ok and q.added is None and q.type == f.type and q.start_lba == cursor and q.blocks == b // BLOCK
        cursor += b // BLOCK
    if not ok:
        raise PlanError("internal: the plan's geometry and diskutil's predicted result disagree")


def _destroys(current: Disk | Unpartitioned) -> tuple[Loss, ...]:
    if isinstance(current, Unpartitioned) and current.apfs is not None:
        return _apfs_losses(current.apfs, current.bsd_name)
    if isinstance(current, Unpartitioned):
        v = current.volume
        if v is not None and v.filesystem is not None:
            return (Loss(current.bsd_name, fs_name(v) + " over the whole disk", _used(v), v.confirmed, v.label),)
        return (Loss(current.bsd_name, "no partition table, no file system recognized", None, False),)
    out: list[Loss] = []
    for p in current.partitions:
        if p.apfs is not None:
            out.extend(_apfs_losses(p.apfs, p.bsd_name))
        elif p.volume is not None and p.volume.filesystem is not None:
            out.append(Loss(p.bsd_name, fs_name(p.volume), _used(p.volume), p.volume.confirmed, p.volume.label))
        else:
            out.append(Loss(p.bsd_name, f"{p.type_name}, no file system recognized", None, False))
    return tuple(out)


def _apfs_losses(c: ApfsContainer, store: str) -> tuple[Loss, ...]:
    where = f"container {c.reference} on {store}"
    if not c.volumes:
        return (Loss(store, f"empty APFS {where}", 0, True),)
    out = []
    for av in c.volumes:
        roles = f" [{', '.join(av.roles)}]" if av.roles else ""
        what = f'APFS volume "{av.name}"{roles} ({where})'
        out.append(Loss(av.bsd_name, what, av.capacity_in_use, True, av.name or None))
    return tuple(out)


def _used(v: VolumeInfo) -> int | None:
    return v.space.used if v.space is not None else None
