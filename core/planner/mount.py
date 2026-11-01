"""Mount or unmount one partition (T-073, K16): one `diskutil mount|unmount <diskNsM>` step.

The partition map does not change and nothing is destroyed, so there is no map backup
(a Step that mounts or unmounts does not write the disk, §S5) and the step undoes itself
with the opposite one. HFS+ gives resize limits only while mounted (D26): this is how the
web UI mounts it before a shrink, as Finder would on a click.

Only a partition with a file system diskutil recognizes. An APFS physical store is not
one: its volumes live on the container's disk, and managing them is a non-goal.

Runs no commands (§S3): the layout is an input.
"""

from __future__ import annotations

from core.model.layout import Disk, Partition, Unpartitioned
from core.planner.common import DISKUTIL, PlanError, layout_of, what
from core.planner.plan import (
    Closed,
    DiskAvailable,
    ExecutionPlan,
    Extent,
    KeptPartition,
    Mounted,
    SliceIs,
    Step,
    Unmounted,
)
from core.planner.predict import Predicted
from core.safety.guard import AvailableDisk

MOUNT = "mount"
UNMOUNT = "unmount"


def plan_mount(target: AvailableDisk, current: Disk | Unpartitioned, slice_name: str, mount: bool) -> ExecutionPlan:
    """MOUNT (else unmount) SLICE_NAME of the disk the guard issued. Raises PlanError."""
    if not isinstance(target, AvailableDisk):
        raise TypeError("the planner accepts only a disk the guard issued (rules.md §S8)")
    disk = target.bsd_name
    if current.bsd_name != disk:
        raise PlanError(f"layout of {current.bsd_name} given for {disk}")
    if isinstance(current, Unpartitioned):
        raise PlanError(f"{disk} has no partition table: diskmap mounts partitions")
    part = next((p for p in current.partitions if p.bsd_name == slice_name), None)
    if part is None:
        raise PlanError(f"{slice_name} is not a partition of {disk}")
    if part.apfs is not None:
        raise PlanError(
            f"{slice_name} ({what(part)}) is an APFS physical store: its volumes are on "
            f"{part.apfs.reference}, and diskmap does not manage APFS volumes"
        )
    v = part.volume
    if v is None or v.filesystem is None:
        raise PlanError(f"{slice_name} ({what(part)}): no file system diskutil recognizes, nothing to mount")
    if mount and v.mounted:
        raise PlanError(f"{slice_name} ({what(part)}) is already mounted at {v.mount_point}")
    if not mount and not v.mounted:
        raise PlanError(f"{slice_name} ({what(part)}) is not mounted")

    kept = tuple(
        KeptPartition(p.bsd_name, Predicted(p.start_lba, p.blocks, p.type, None), what(p)) for p in current.partitions
    )
    return ExecutionPlan(
        MOUNT if mount else UNMOUNT,
        disk,
        current.blocks,
        current.block_size,
        layout_of(current),
        tuple(k.predicted for k in kept),
        (),
        (_step(disk, part, mount, _step(disk, part, not mount, None)),),
        (f"The partition map of {disk} does not change.",),
        kept=kept,
        scheme=current.scheme,
    )


def _step(disk: str, part: Partition, mount: bool, rollback: Step | None) -> Step:
    """ROLLBACK: the opposite step, which undoes this one."""
    s = part.bsd_name
    here = SliceIs(s, Extent(part.start_lba, part.blocks, part.type))
    return Step(
        intent=f"{'mount' if mount else 'unmount'} {s} ({what(part)})",
        command=(DISKUTIL, MOUNT if mount else UNMOUNT, s),
        needs_root=False,
        writes_disk=False,
        stdin=Closed(),
        preflight=(DiskAvailable(disk), here, Unmounted(s) if mount else Mounted(s)),
        postflight=(Mounted(s),) if mount else (Unmounted(s),),
        rollback=rollback,
        destroys=(),
    )
