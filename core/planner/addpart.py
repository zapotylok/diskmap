"""Strategy "add to the free space at the end" (T-043, D25): new partitions after the last
one, nothing that exists is touched — not its entry, not its place, not its data. Or, with
AFTER, into the free space between that partition and the next (T-075, D50, see _into_gap).

One `diskutil addPartition <disk> <format> <name> <size>` per new partition, the whole
disk as target, so that each lands last. Aimed at a slice, diskutil puts a partition that
does not fit the gap after it somewhere else; put into a gap between partitions, it
renumbers the ones after it (rig, D25, D24). So the tail is filled aiming at the disk, and
a gap in the middle is left as it is, with a note, unless it is asked for by AFTER. Formats are the same as for the full repartition
(formats.py): ntfs and efi get a blank slot and a step of their own.

The geometry is predict_add()'s, measured rules only; there is no -noEFI for addPartition,
so HFS+ over 1 GiB loses 128 MiB to booter space and the preview says so. `rest` is the
largest size the prediction lets fit, not a subtraction: alignment and booter space are
then accounted for exactly. Percentages are of the tail.

Slice names of the new partitions follow the existing ones only when those are numbered
diskNs1..sN; otherwise the plan is refused rather than guessed.
"""

from __future__ import annotations

from core.model.desired import DesiredLayout, human
from core.model.filesystems import Registry
from core.model.layout import GPT, Disk, Partition, Unpartitioned
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
    mount_step,
    mounting_lines,
    place_in_tail,
    renumber_step,
    tail_notes,
    triple,
    unmount_step,
    what,
)
from core.planner.plan import (
    DiskAvailable,
    ExecutionPlan,
    KeptPartition,
    LayoutIs,
    PlannedPartition,
    SliceIs,
    Step,
)
from core.planner.predict import BLOCK, Predicted
from core.safety.guard import AvailableDisk

STRATEGY = "add"


def plan_add(
    target: AvailableDisk,
    current: Disk | Unpartitioned,
    desired: DesiredLayout,
    registry: Registry,
    tools: Tools,
    folder: str,
    after: str | None = None,
) -> ExecutionPlan:
    """FOLDER: where the plan's first steps back up the map (core.planner.backup). AFTER: the
    partition (diskNsM) whose following free space to fill; None or the last one: the tail."""
    if not isinstance(target, AvailableDisk):
        raise TypeError("the planner accepts only a disk the guard issued (rules.md §S8)")
    disk = target.bsd_name
    if current.bsd_name != disk:
        raise PlanError(f"layout of {current.bsd_name} given for {disk}")
    if not isinstance(current, Disk) or current.scheme != GPT:
        what_ = "no partition table" if isinstance(current, Unpartitioned) else f"an {current.scheme} partition map"
        raise PlanError(f"{disk} has {what_}: addPartition needs GPT — use the full repartition")
    if current.block_size != BLOCK:
        raise PlanError(f"{disk}: {current.block_size}-byte blocks; only {BLOCK}-byte disks were measured")
    if not current.partitions:
        raise PlanError(f"{disk} has no partitions: addPartition needs one to follow — use the full repartition")
    check_slice_names(disk, current)
    names = [p.bsd_name for p in current.partitions]

    fmts = formats_of(desired, registry, tools)
    blocks = current.blocks
    if after is not None and after not in names:
        raise PlanError(f"{after} is not a partition of {disk}")
    if after is not None and after != names[-1]:
        return _into_gap(current, names.index(after), desired, fmts, tools, folder)
    cursor = max(p.end_lba for p in current.partitions) + 1
    tail = place_in_tail(disk, cursor, f"its last partition ({names[-1]})", blocks, desired, fmts)
    new = tail.new

    kept = tuple(
        KeptPartition(p.bsd_name, Predicted(p.start_lba, p.blocks, p.type, None), what(p)) for p in current.partitions
    )
    old = tuple(extent(k.predicted) for k in kept)
    before = layout_of(current)
    planned = tuple(
        PlannedPartition(p.format, p.label, p.size.text, makes(f), f"{disk}s{len(kept) + i}", q)
        for i, (p, f, q) in enumerate(zip(desired.partitions, fmts, new), 1)
    )
    steps, backed = backup_steps(current, folder)
    for i, (pp, f, b) in enumerate(zip(planned, fmts, tail.sizes)):
        steps.append(
            Step(
                intent=f"add {pp.slice} after the last partition of {disk}",
                command=(DISKUTIL, "addPartition", disk, *triple(f, pp.label, b)),
                needs_root=False,
                writes_disk=True,
                stdin=closed_for([f]),
                preflight=(
                    DiskAvailable(disk),
                    LayoutIs(disk, GPT, old + tuple(extent(q) for q in new[:i])),
                    *backed,
                ),
                postflight=(LayoutIs(disk, GPT, old + tuple(extent(q) for q in new[: i + 1])),),
                rollback=None,
                destroys=(),  # free space only (a blank slot is zeroed at its start: man diskutil)
            )
        )
    made, format_notes = format_all(
        planned, fmts, disk, tools, backed, LayoutIs(disk, GPT, old + tuple(extent(q) for q in new))
    )
    steps += made

    which = names[0] if len(names) == 1 else f"{names[0]}..{names[-1]}"
    notes = [f"{which}: entries, places and data are not touched."]
    notes += tail_notes(tail, fmts)
    middle = [g for g in current.free if g.end_lba < cursor and g.blocks * BLOCK >= 2**20]
    if middle:
        notes.append(
            f"Free space between partitions ({human(sum(g.blocks for g in middle) * BLOCK)}) is not used: "
            "--after <the partition before it> fills it, renumbering the partitions after it."
        )
    notes += free_at_end(tail)
    notes += format_notes
    notes += backup_notes(current, folder)
    return ExecutionPlan(
        STRATEGY,
        disk,
        blocks,
        BLOCK,
        before,
        tuple(k.predicted for k in kept) + new,
        planned,
        tuple(steps),
        tuple(notes),
        kept,
        mounting=tuple(mounting_lines(planned, fmts, tools.trial)),
    )


def _into_gap(
    current: Disk, k: int, desired: DesiredLayout, fmts: list[F.Format], tools: Tools, folder: str
) -> ExecutionPlan:
    """New partitions into the free space after partition K (0-based) and before K+1 (rig,
    2026-10-30, D50). `addPartition <slice before the gap>` puts one there exactly as into the
    tail (predict_add) and rewrites the table in LBA order: the partitions after it take the
    next numbers, and ioreg marks them out of date (Live=false, D24) — mounted or not. No probe
    reads such a disk; the map is read again when the kernel re-reads it, the zero-byte `dd`
    of D33, every volume unmounted. Then the names follow the table, so each next partition is
    aimed at the one the step before made. A size that does not fit the gap is refused:
    diskutil puts it in the tail, rc=0 (D25)."""
    disk, parts, blocks = current.bsd_name, current.partitions, current.blocks
    if [p.start_lba for p in parts] != sorted(p.start_lba for p in parts):
        raise PlanError(f"{disk}: its partition table is not in disk order, so where new entries go is not known")
    before, nxt = parts[k], parts[k + 1]
    tail = place_in_tail(
        disk,
        before.end_lba + 1,
        f"{before.bsd_name} (before {nxt.bsd_name})",
        blocks,
        desired,
        fmts,
        before_lba=nxt.start_lba,
    )
    new, m = tail.new, len(tail.new)

    def place(p: Partition) -> Predicted:
        return Predicted(p.start_lba, p.blocks, p.type, None)

    kept = tuple(
        KeptPartition(f"{disk}s{i if i <= k + 1 else i + m}", place(p), what(p), None if i <= k + 1 else p.bsd_name)
        for i, p in enumerate(parts, 1)
    )
    head, rest_ = [extent(q.predicted) for q in kept[: k + 1]], [extent(q.predicted) for q in kept[k + 1 :]]

    def layout(i: int) -> LayoutIs:
        """The map once I of the new partitions are in."""
        return LayoutIs(disk, GPT, (*head, *(extent(q) for q in new[:i]), *rest_))

    planned = tuple(
        PlannedPartition(p.format, p.label, p.size.text, makes(f), f"{disk}s{k + 1 + i}", q)
        for i, (p, f, q) in enumerate(zip(desired.partitions, fmts, new), 1)
    )
    steps, backed = backup_steps(current, folder)
    for i, (pp, f, b) in enumerate(zip(planned, fmts, tail.sizes)):
        aim = f"{disk}s{k + 1 + i}"  # the partition before the gap, then the one the add before made
        steps += [
            Step(
                intent=f"add {pp.slice} right after {aim}; the partitions after it take the next numbers",
                command=(DISKUTIL, "addPartition", aim, *triple(f, pp.label, b)),
                needs_root=False,
                writes_disk=True,
                stdin=closed_for([f]),
                preflight=(DiskAvailable(disk), layout(i), SliceIs(aim, layout(i).partitions[k + i]), *backed),
                postflight=(),  # out of date until the map is read again (D24): the reread checks it
                rollback=None,
                destroys=(),  # free space only (a blank slot is zeroed at its start: man diskutil)
            ),
            unmount_step(disk),  # the new volume is mounted at once; the reread needs none
            renumber_step(disk, layout(i + 1)),
        ]
    steps.append(mount_step(disk))
    made, format_notes = format_all(planned, fmts, disk, tools, backed, layout(m))
    steps += made

    moved = [q for q in kept if q.was is not None]
    renames = ", ".join(f"{q.was} → {q.slice}" for q in moved)
    if len(moved) == 1:
        kept_note = f"{moved[0].was} keeps its entry, place and data but takes a new name: {renames}."
    else:
        kept_note = f"{', '.join(q.was or '' for q in moved)} keep their entries, places and data but take new names: {renames}."
    notes = [kept_note + " Every volume of the disk is unmounted while the map is read again, then mounted."]
    notes += tail_notes(tail, fmts)
    notes += free_at_end(tail)
    notes += format_notes
    notes += backup_notes(current, folder)
    return ExecutionPlan(
        STRATEGY,
        disk,
        blocks,
        BLOCK,
        layout_of(current),
        tuple(q.predicted for q in kept[: k + 1]) + new + tuple(q.predicted for q in kept[k + 1 :]),
        planned,
        tuple(steps),
        tuple(notes),
        kept,
        mounting=tuple(mounting_lines(planned, fmts, tools.trial)),
    )
