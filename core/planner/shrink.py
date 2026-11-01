"""Strategy "shrink a partition and add partitions after it" (T-044, D26): the data of the
shrunk partition stays, nothing else is touched.

One command: `diskutil resizeVolume <slice> <size> [<format> <name> <size>]…` for HFS+, or
`diskutil apfs resizeContainer <slice> <size> […]` for an APFS physical store — the second
is what diskutil itself points to for APFS. Only these two can be shrunk in place: for
every other file system diskutil refuses resize limits (ExFAT, FAT32, NTFS, ext4 — rig
2026-10-02), and the plan says so and names the full repartition instead (AC of T-044).

Limits come first: the strategy is handed `diskutil … limits -plist` as read by probe
(core.probe.resize) and refuses a size below its minimum; the step asks them again
(ShrinkAllowed). HFS+ answers limits only while mounted.

New partitions only after the last partition: put between two partitions, a new one
renumbers the ones after it (D24, D25; rig: Live=false even with nothing mounted). A
partition in the middle may still be shrunk alone, with no new partitions — that does not
renumber anything. The geometry is predict_resize()'s, measured rules only: the partition
gets exactly the size asked, an HFS+ partition of 1 GiB or more keeps 128 MiB of booter
space after it, the new ones are placed as addPartition places them. apfs resizeContainer
refuses blank slots, so ntfs and efi cannot follow an APFS store in the same command.

Runs no commands (§S3).
"""

from __future__ import annotations

from core.model import gpt_types as T
from core.model.desired import FIXED, DesiredLayout, LayoutError, Size, human
from core.model.filesystems import Registry
from core.model.layout import GPT, Disk, Partition, Unpartitioned
from core.model.resize import UNMOUNTED, UNSUPPORTED, NotResizable, ResizeLimits
from core.planner import formats as F
from core.planner.backup import backup_notes, backup_steps
from core.planner.common import (
    DISKUTIL,
    PlanError,
    Tools,
    check_slice_names,
    closed_for,
    extent,
    format_all,
    formats_of,
    free_at_end,
    layout_of,
    makes,
    mounting_lines,
    place_in_tail,
    size_arg,
    tail_notes,
    triple,
    what,
)
from core.planner.plan import (
    Assertion,
    DiskAvailable,
    ExecutionPlan,
    KeptPartition,
    LayoutIs,
    PlannedPartition,
    ResizedPartition,
    ShrinkAllowed,
    Step,
    VolumeIs,
)
from core.planner.predict import (
    BLOCK,
    GIB,
    KIB4,
    RESIZE_APFS,
    RESIZE_HFS,
    Predicted,
    Unpredictable,
    predict_resize,
    resize_cursor,
)
from core.safety.guard import AvailableDisk

STRATEGY = "shrink"


def plan_shrink(
    target: AvailableDisk,
    current: Disk | Unpartitioned,
    slice_name: str,
    new_size: Size,
    desired: DesiredLayout | None,
    limits: ResizeLimits | NotResizable,
    registry: Registry,
    tools: Tools,
    folder: str,
) -> ExecutionPlan:
    """FOLDER: where the plan's first steps back up the map (core.planner.backup)."""
    current, part, limits, kind = resizable(target, current, slice_name, limits)
    disk = target.bsd_name
    size = _new_size(new_size, part, limits)
    following = [p for p in current.partitions if p.start_lba > part.start_lba]
    fmts = formats_of(desired, registry, tools) if desired is not None else []
    if fmts and following:
        raise PlanError(
            f"{slice_name} is not the last partition: a new partition after it would land between "
            f"{slice_name} and {following[0].bsd_name} and renumber the partitions after it. "
            "Shrink it alone (leave out the layout), or use the full repartition."
        )
    if kind == RESIZE_APFS and any(f.kind != F.PERSONALITY for f in fmts):
        raise PlanError(
            "apfs resizeContainer cannot cut a blank slot, which ntfs and efi need: shrink the "
            f"container first (with the other partitions, or alone), then add those with diskmap plan add {disk}"
        )

    blocks = current.blocks
    now = part.blocks * BLOCK
    try:
        shrunk, _ = predict_resize(part.start_lba, now, kind, size, (), blocks, BLOCK)
    except Unpredictable as e:
        raise PlanError(f"diskutil's result is not predictable: {e}") from None
    new: tuple[Predicted, ...] = ()
    sizes: list[int] = []
    notes_tail: list[str] = []
    if desired is not None:
        cursor = resize_cursor(part.start_lba, kind, shrunk.blocks)
        tail = place_in_tail(
            disk,
            cursor,
            f"{slice_name} once shrunk",
            blocks,
            desired,
            fmts,
            lambda adds: predict_resize(part.start_lba, now, kind, size, adds, blocks, BLOCK)[1],
        )
        new = tail.new
        sizes = tail.sizes
        notes_tail = tail_notes(tail, fmts) + free_at_end(tail)

    others = [p for p in current.partitions if p is not part]
    kept = tuple(KeptPartition(p.bsd_name, _place(p), what(p)) for p in others)
    resized = ResizedPartition(slice_name, _place(part), shrunk, what(part))
    after = tuple(sorted((*(k.predicted for k in kept), shrunk, *new), key=lambda q: q.start_lba))
    count = len(current.partitions)
    planned = tuple(
        PlannedPartition(p.format, p.label, p.size.text, makes(f), f"{disk}s{count + i}", q)
        for i, (p, f, q) in enumerate(zip(desired.partitions if desired else (), fmts, new), 1)
    )

    argv: list[str] = (
        [DISKUTIL, "apfs", "resizeContainer", slice_name, size_arg(size)]
        if kind == RESIZE_APFS
        else [DISKUTIL, "resizeVolume", slice_name, size_arg(size)]
    )
    for pp, f, b in zip(planned, fmts, sizes):
        argv += triple(f, pp.label, b)
    pre, backed = backup_steps(current, folder)
    preflight: list[Assertion] = [DiskAvailable(disk), layout_of(current), *backed]
    if kind == RESIZE_HFS:
        assert part.volume is not None
        preflight.append(VolumeIs(slice_name, "hfs", part.volume.label))
    preflight.append(ShrinkAllowed(slice_name, kind == RESIZE_APFS, size))
    adding = f" and add {', '.join(pp.slice for pp in planned)} after it" if planned else ""
    steps = [
        *pre,
        Step(
            intent=f"shrink {slice_name} ({what(part)}) to {human(size)}{adding}",
            command=tuple(argv),
            needs_root=False,
            writes_disk=True,
            stdin=closed_for(fmts),  # diskutil answers a Paragon formatter's y/n itself (D22, D27)
            preflight=tuple(preflight),
            postflight=(LayoutIs(disk, GPT, tuple(extent(q) for q in after)),),
            rollback=None,  # growing back is not measured
            destroys=(),  # the file system is shrunk with its data; new slots take free space only
        ),
    ]
    made, format_notes = format_all(
        planned, fmts, disk, tools, backed, LayoutIs(disk, GPT, tuple(extent(q) for q in after))
    )
    steps += made

    notes = [
        (
            f"{slice_name} ({what(part)}) is shrunk in place from {human(part.blocks * BLOCK)} to {human(size)}; "
            "its data stays. A resize that fails part-way can damage the file system: back up what matters on it first."
        )
    ]
    if others:
        names = [p.bsd_name for p in others]
        which = names[0] if len(names) == 1 else ", ".join(names)
        notes.append(f"{which}: entries, places and data are not touched.")
    if kind == RESIZE_HFS and shrunk.blocks >= GIB and planned:
        notes.append(
            f"diskutil leaves 128 MiB free after {slice_name} as booter space (HFS+ of 1 GiB or more); "
            "the new partitions start after it."
        )
    notes += notes_tail
    if not planned:
        freed = human((part.blocks - shrunk.blocks) * BLOCK)
        if following:
            notes.append(
                f"The {freed} freed lies between {slice_name} and {following[0].bsd_name}; diskmap cannot add "
                "a partition there (one added there renumbers the partitions after it)."
            )
        else:
            notes.append(f"The {freed} freed stays free; diskmap plan add {disk} can use it.")
    notes += format_notes
    notes += backup_notes(current, folder)
    return ExecutionPlan(
        STRATEGY,
        disk,
        blocks,
        BLOCK,
        layout_of(current),
        after,
        planned,
        tuple(steps),
        tuple(notes),
        kept,
        (resized,),
        mounting=tuple(mounting_lines(planned, fmts, tools.trial)),
    )


def resizable(
    target: AvailableDisk, current: Disk | Unpartitioned, slice_name: str, limits: ResizeLimits | NotResizable
) -> tuple[Disk, Partition, ResizeLimits, str]:
    """What plan_shrink and plan_grow (T-074) check before a size is named: SLICE_NAME on this
    disk can be resized in place, and LIMITS are its own. Also what the web UI asks before it
    offers a size (T-069), so the page refuses in the planner's words. Raises PlanError; the last
    item is RESIZE_HFS or RESIZE_APFS."""
    if not isinstance(target, AvailableDisk):
        raise TypeError("the planner accepts only a disk the guard issued (rules.md §S8)")
    disk = target.bsd_name
    if current.bsd_name != disk:
        raise PlanError(f"layout of {current.bsd_name} given for {disk}")
    if not isinstance(current, Disk) or current.scheme != GPT:
        what_ = "no partition table" if isinstance(current, Unpartitioned) else f"an {current.scheme} partition map"
        raise PlanError(f"{disk} has {what_}: only GPT partitions were measured with resizeVolume")
    if current.block_size != BLOCK:
        raise PlanError(f"{disk}: {current.block_size}-byte blocks; only {BLOCK}-byte disks were measured")
    part = next((p for p in current.partitions if p.bsd_name == slice_name), None)
    if part is None:
        raise PlanError(f"{slice_name} is not a partition of {disk}")
    if limits.slice != slice_name:
        raise PlanError(f"internal: resize limits of {limits.slice} given for {slice_name}")
    check_slice_names(disk, current)
    kind = _kind(part, limits, disk)
    assert isinstance(limits, ResizeLimits)
    if limits.apfs != (kind == RESIZE_APFS) or limits.current != part.blocks * BLOCK:
        raise PlanError(f"{slice_name}: resize limits and the partition map disagree — run the plan again")
    return current, part, limits, kind


def _kind(part: Partition, limits: ResizeLimits | NotResizable, disk: str) -> str:
    """RESIZE_HFS or RESIZE_APFS — or the refusal the AC of T-044 asks for."""
    if isinstance(limits, NotResizable):
        if limits.why == UNMOUNTED:
            raise PlanError(
                f"{part.bsd_name} ({what(part)}): diskutil gives resize limits only while it is mounted. "
                f"Mount it (diskutil mount {part.bsd_name}) and plan again."
            )
        assert limits.why == UNSUPPORTED
        raise PlanError(
            f"{part.bsd_name} ({what(part)}): this file system does not support non-destructive resizing "
            f'in your configuration — diskutil: "{limits.said}". To lay {disk} out anew, use the full '
            f"repartition: diskmap plan repartition {disk} <layout> — it destroys everything on {disk}."
        )
    if part.type == T.APPLE_APFS.guid:
        c = part.apfs
        if c is None:
            raise PlanError(f"{part.bsd_name}: an APFS partition whose container diskutil does not list")
        if len(c.physical_stores) != 1:
            raise PlanError(f"{part.bsd_name}: its APFS container spans {len(c.physical_stores)} partitions")
        locked = [v.name for v in c.volumes if v.locked]
        if locked:
            raise PlanError(f"{part.bsd_name}: APFS volume(s) {', '.join(locked)} locked; unlock them first")
        return RESIZE_APFS
    if part.type == T.APPLE_HFS.guid and part.volume is not None and part.volume.filesystem == "hfs":
        return RESIZE_HFS
    raise PlanError(f"{part.bsd_name} ({what(part)}): diskutil gives resize limits, but resizing it was not measured")


def _new_size(new_size: Size, part: Partition, limits: ResizeLimits) -> int:
    if new_size.kind != FIXED:
        raise LayoutError(f"new size {new_size}: give a size such as 900Mi or 20G, not a percentage or rest")
    k = KIB4 * BLOCK
    size = -(-int(new_size.value) // k) * k
    now = part.blocks * BLOCK
    if size >= now:
        raise PlanError(
            f"{part.bsd_name} is {human(now)}; {new_size} rounded up to 4 KiB is {human(size)} — "
            "this strategy only shrinks"
        )
    if size < limits.minimum:
        raise PlanError(
            f"{part.bsd_name} can be shrunk to no less than {human(limits.minimum)} "
            f"({limits.minimum} bytes, diskutil resize limits: the space its files take); asked {human(size)}"
        )
    return size


def _place(p: Partition) -> Predicted:
    return Predicted(p.start_lba, p.blocks, p.type, None)
