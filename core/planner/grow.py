"""Strategy "grow a partition into the free space right after it" (T-074, D48): the data of
the grown partition stays, nothing else is touched.

One command, as for shrinking (D26): `diskutil resizeVolume <slice> <size>` for HFS+, or
`diskutil apfs resizeContainer <slice> <size>` for an APFS physical store. The same two only:
for every other file system diskutil gives no resize limits (core/planner/shrink.py).

The limit is the free space right after the partition, by the map: to the next partition or the
end of the usable area, less 128 MiB of booter space for HFS+, in whole 4 KiB (grow_edge; rig
2026-10-30, testrig/resize_cases.txt). That is where diskutil's own R and 0 go, and an explicit
size that large passes. The MaximumSize of its resize limits says up to a MiB less — the end
rounded down to a MiB — so a partition that diskutil laid out on 4 KiB could not grow back to the
size it had (Vitali, four-parts s5 shrunk and grown). MaximumSize above that edge is diskutil
knowing something not measured: refused (§S9). diskmap names the exact size, so the preview says
the byte count diskutil is asked for. The map is the plan's at the step (LayoutIs), so the free
space is too; the step asks the limits again all the same (GrowAllowed).

Growing into a gap between two partitions renumbers nothing (rig: same indices, no Live=false),
so the partition need not be the last. No new partitions in the same command: diskutil refuses
them with a grow (-69604). The geometry is predict_resize()'s: the partition keeps its start
and gets exactly the size asked.

Runs no commands (§S3).
"""

from __future__ import annotations

from core.model.desired import FIXED, LayoutError, Size, human
from core.model.layout import GPT, Disk, Partition, Unpartitioned
from core.model.mapdump import tail_blocks
from core.model.resize import NotResizable, ResizeLimits
from core.planner.backup import backup_notes, backup_steps
from core.planner.common import DISKUTIL, PlanError, extent, layout_of, size_arg, what
from core.planner.plan import (
    Assertion,
    Closed,
    DiskAvailable,
    ExecutionPlan,
    GrowAllowed,
    KeptPartition,
    LayoutIs,
    ResizedPartition,
    ShrinkAllowed,
    Step,
    VolumeIs,
)
from core.planner.predict import (
    BLOCK,
    KIB4,
    RESIZE_APFS,
    RESIZE_HFS,
    Predicted,
    Unpredictable,
    grow_edge,
    predict_resize,
)
from core.planner.shrink import resizable
from core.safety.guard import AvailableDisk

STRATEGY = "grow"


def plan_grow(
    target: AvailableDisk,
    current: Disk | Unpartitioned,
    slice_name: str,
    new_size: Size,
    limits: ResizeLimits | NotResizable,
    folder: str,
) -> ExecutionPlan:
    """FOLDER: where the plan's first steps back up the map (core.planner.backup)."""
    current, part, limits, kind = resizable(target, current, slice_name, limits)
    disk = target.bsd_name
    now = part.blocks * BLOCK
    top = grow_limit(current, part, kind, limits)
    size = _new_size(new_size, slice_name, now, top, current)
    try:
        grown, _ = predict_resize(part.start_lba, now, kind, size, (), current.blocks, BLOCK)
    except Unpredictable as e:
        raise PlanError(f"diskutil's result is not predictable: {e}") from None

    others = [p for p in current.partitions if p is not part]
    kept = tuple(KeptPartition(p.bsd_name, _place(p), what(p)) for p in others)
    resized = ResizedPartition(slice_name, _place(part), grown, what(part))
    after = tuple(sorted((*(k.predicted for k in kept), grown), key=lambda q: q.start_lba))
    apfs = kind == RESIZE_APFS

    def resize_to(nbytes: int) -> tuple[str, ...]:
        if apfs:
            return (DISKUTIL, "apfs", "resizeContainer", slice_name, size_arg(nbytes))
        return (DISKUTIL, "resizeVolume", slice_name, size_arg(nbytes))

    pre, backed = backup_steps(current, folder)
    volume: list[Assertion] = []
    if kind == RESIZE_HFS:
        assert part.volume is not None
        volume.append(VolumeIs(slice_name, "hfs", part.volume.label))
    after_is = LayoutIs(disk, GPT, tuple(extent(q) for q in after))
    # Back to the size it has now: shrinking is measured (D26), as long as its files still fit.
    back = Step(
        intent=f"shrink {slice_name} back to {human(now)}",
        command=resize_to(now),
        needs_root=False,
        writes_disk=True,
        stdin=Closed(),
        preflight=(DiskAvailable(disk), after_is, *backed, *volume, ShrinkAllowed(slice_name, apfs, now)),
        postflight=(layout_of(current),),
        rollback=None,
        destroys=(),
    )
    steps = [
        *pre,
        Step(
            intent=f"grow {slice_name} ({what(part)}) to {human(size)}",
            command=resize_to(size),
            needs_root=False,
            writes_disk=True,
            stdin=Closed(),  # no formatter: nothing asked (D27)
            preflight=(DiskAvailable(disk), layout_of(current), *backed, *volume, GrowAllowed(slice_name, apfs, size)),
            postflight=(after_is,),
            rollback=back,
            destroys=(),  # the file system grows with its data; it takes free space only
        ),
    ]

    notes = [
        (
            f"{slice_name} ({what(part)}) is grown in place from {human(now)} to {human(size)}; its data stays. "
            "A resize that fails part-way can damage the file system: back up what matters on it first."
        )
    ]
    if others:
        names = [p.bsd_name for p in others]
        notes.append(f"{', '.join(names)}: entries, places and data are not touched.")
    left = top - size
    if left >= 2**20:
        notes.append(f"{human(left)} more could still be added to {slice_name} (up to {human(top)}).")
    if kind == RESIZE_HFS:
        notes.append(
            f"diskutil keeps 128 MiB free after an HFS+ partition as booter space: {slice_name} cannot grow into it."
        )
    notes += backup_notes(current, folder)
    return ExecutionPlan(
        STRATEGY,
        disk,
        current.blocks,
        BLOCK,
        layout_of(current),
        after,
        (),
        tuple(steps),
        tuple(notes),
        kept,
        (resized,),
    )


def grow_limit(current: Disk, part: Partition, kind: str, limits: ResizeLimits) -> int:
    """The most bytes PART can be grown to: grow_edge of the free space right after it, or its size
    when there is none. Also what the web UI offers as the end of the handle (cli/plan.resize_limits)."""
    following = [p.start_lba for p in current.partitions if p.start_lba > part.start_lba]
    end = min(following, default=current.blocks - tail_blocks(BLOCK))
    edge = grow_edge(part.start_lba, kind, end)
    if limits.maximum > max(edge, limits.current):
        raise PlanError(
            f"{part.bsd_name}: diskutil's resize limits allow {human(limits.maximum)}, more than the free space "
            f"right after it by the map ({human(edge)}) — not measured, not planned"
        )
    return max(edge, part.blocks * BLOCK)


def _new_size(new_size: Size, slice_name: str, now: int, top: int, current: Disk) -> int:
    """NEW_SIZE in bytes, rounded up to 4 KiB as for shrinking, between the size NOW and TOP (grow_limit)."""
    if new_size.kind != FIXED:
        raise LayoutError(f"new size {new_size}: give a size such as 3Gi or 20G, not a percentage or rest")
    k = KIB4 * BLOCK
    size = -(-int(new_size.value) // k) * k
    if size <= now:
        raise PlanError(
            f"{slice_name} is {human(now)}; {new_size} rounded up to 4 KiB is {human(size)} — this strategy only grows"
        )
    if top <= now:
        part = next(p for p in current.partitions if p.bsd_name == slice_name)
        following = [p for p in current.partitions if p.start_lba > part.start_lba]
        where = f"{following[0].bsd_name} follows it" if following else "it reaches the end of the map"
        raise PlanError(f"{slice_name} cannot grow: there is no free space right after it ({where})")
    if size > top:
        raise PlanError(
            f"{slice_name} can be grown to no more than {human(top)} (the free space right after it); "
            f"asked {human(size)}"
        )
    return size


def _place(p: Partition) -> Predicted:
    return Predicted(p.start_lba, p.blocks, p.type, None)
