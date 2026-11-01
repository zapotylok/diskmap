"""Trial mount (T-026, D34): the plans `diskmap doctor --trial` runs on its own disk image.

One plan per format, so that one format failing tells nothing about the others: the
full repartition of the image into a single partition of that format, labelled TRIAL —
the very steps `diskmap plan repartition` would make for it — and then the trial's own
tail, which tries to mount whatever was made without presuming who takes it:

  diskutil unmountDisk <disk>        every volume down, as on a disk unplugged
  dd … of=/dev/r<disk> count=0       ext4 only: the kernel re-reads the map, macOS probes
                                     the slice again (D33); its postflight waits until
                                     macOS confirms some file system there, not a given one
  diskutil mountDisk <disk>          what mounts it, and how, is the result

The result is not a postflight: `doctor` reads it from `mount` after the plan (a step
that fails to mount is an answer, "no"). The D33 route for ext4 is left out on purpose —
it re-reads only when it predicts a driver; the trial re-reads always, to find out.

No step needs root: the image's nodes belong to the user who attached it (K7), and the
trial runs as that user. The last plan detaches the image, through the executor too.

Runs no commands (§S3).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.model.desired import parse_layout
from core.model.doctor import Ext4Mounting
from core.model.filesystems import Registry
from core.model.layout import Disk, Unpartitioned
from core.planner import formats as F
from core.planner.common import DISKUTIL, PlanError, Tools, extent, layout_of, reread_step, unmount_step
from core.planner.plan import Closed, DiskAvailable, ExecutionPlan, LayoutIs, Probed, Step
from core.planner.repartition import plan_repartition
from core.safety.guard import AvailableDisk

STRATEGY = "trial"
DETACH = "detach"
LABEL = "TRIAL"
HDIUTIL = "/usr/bin/hdiutil"


def trial_formats(registry: Registry, tools: Tools) -> list[tuple[str, str | None]]:
    """Every format a layout can name on this Mac, as (format, why not tried): diskutil's
    personalities that format, in its order, then ntfs and ext4. WHY is None for the ones
    the trial makes; else why diskmap plans none (an unmeasured content mask, a missing
    formatter)."""
    out: list[tuple[str, str | None]] = []
    for p in registry.personalities:
        if not p.formats:
            continue
        try:
            F.resolve_format(p.name, registry)
        except F.FormatError as e:
            out.append((p.name, f"diskmap does not plan it: {e}"))
            continue
        out.append((p.name, None))
    out.append((F.NTFS, None if tools.mkntfs else "mkntfs (ntfs-3g) is not found"))
    out.append((F.EXT4, None if tools.mke2fs else "mke2fs (e2fsprogs) is not found"))
    return out


@dataclass(frozen=True)
class TrialPlan:
    plan: ExecutionPlan
    tail: int  # how many of the last steps are the mount tail: a step that fails before them
    #            failed to create the format, one of them failed to mount it


def plan_trial(
    target: AvailableDisk, current: Disk | Unpartitioned, fmt: str, registry: Registry, tools: Tools, folder: str
) -> TrialPlan:
    """FMT on the whole of TARGET, then the mount tail. FOLDER: the map backup, as for any plan."""
    desired = parse_layout({"partitions": [{"format": fmt, "label": LABEL, "size": "rest"}]})
    # No D33 recognition inside: the tail below re-reads whatever the prediction says.
    base = plan_repartition(target, current, desired, registry, replace(tools, ext4=Ext4Mounting(None, None)), folder)
    disk = base.disk
    made = base.partitions[0]
    tail = [unmount_step(disk)]
    if F.resolve_format(fmt, registry).kind == F.EXT4:
        layout = LayoutIs(disk, base.scheme, tuple(extent(q) for q in base.after))
        tail.append(reread_step(disk, layout, (Probed(made.slice, None, LABEL),)))
    tail.append(
        Step(
            intent=f"try to mount every volume of {disk}: what mounts the new {fmt}, and how, is the result",
            command=(DISKUTIL, "mountDisk", disk),
            needs_root=False,
            writes_disk=False,
            stdin=Closed(),
            preflight=(DiskAvailable(disk),),
            postflight=(),
            rollback=None,
            destroys=(),
        )
    )
    steps = tuple(replace(s, needs_root=False) for s in (*base.steps, *tail))
    return TrialPlan(replace(base, strategy=STRATEGY, steps=steps, notes=(), mounting=()), len(tail))


def plan_detach(target: AvailableDisk, current: Disk | Unpartitioned) -> ExecutionPlan:
    """Detach the trial image: its device and every disk on it go away; nothing is written."""
    if not isinstance(target, AvailableDisk):
        raise TypeError("the planner accepts only a disk the guard issued (rules.md §S8)")
    disk = target.bsd_name
    if target.kind != "disk-image":
        raise PlanError(f"{disk} is not a disk image; only the trial's own image is detached")
    step = Step(
        intent=f"detach the trial image ({disk}): its volumes are unmounted and its devices go away",
        command=(HDIUTIL, "detach", disk),
        needs_root=False,
        writes_disk=False,
        stdin=Closed(),
        preflight=(DiskAvailable(disk),),
        postflight=(),
        rollback=None,
        destroys=(),
    )
    blocks = current.size // current.block_size
    return ExecutionPlan(DETACH, disk, blocks, current.block_size, layout_of(current), (), (), (step,), ())
