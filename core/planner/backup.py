"""Back up a partition map, and put one back (T-012, K11): mkdir and dd steps, nothing else.

backup_steps() opens every plan that writes to a disk with a partition table: two mkdir
and two dd that copy the blocks the table lives in (core.model.mapdump) into a folder of
its own. Every writing step after them carries MapBackedUp of that folder (§S5). The
folder is new: mkdir without -p fails if it exists, so a plan pasted twice stops instead
of overwriting the backup of the first state with the second. A disk without a partition
table has no map to back up (D17): no steps.

plan_backup() is the backup alone (`diskmap backup-map`). plan_restore() puts one back
(`diskmap restore-map`): back up what is there now, unmount every volume (T-011), write
the tail, then the head. The kernel re-reads the table when dd closes the disk and goes
by the primary GPT (rig, 2026-10-11): after the tail the old map still stands, after the
head the restored one — the switch is one write. Each write can be undone from the
backup the same plan made.

Root: reading or writing /dev/rdiskN of a real disk takes it (root:operator 640, K7);
the steps say so by that model, not by trying — a rig node is the user's.

Runs no commands (§S3): the layout, the kernel's map and the backup are inputs.
"""

from __future__ import annotations

import os
from datetime import datetime

from core.model import gpt_types
from core.model.layout import GPT, MBR, Disk, Partition, Unpartitioned
from core.model.mapdump import BLOCK_SIZES, HEAD_FILE, TAIL_FILE, Entry, MapDump, head_blocks, tail_blocks
from core.planner.common import DISKUTIL, PlanError, layout_of, unmount_step, what
from core.planner.plan import (
    Assertion,
    Closed,
    DiskAvailable,
    DiskUnmounted,
    ExecutionPlan,
    KeptPartition,
    MapBackedUp,
    MapIs,
    RestoredPartition,
    Step,
)
from core.planner.predict import Predicted
from core.probe.ioreg import IoDisk
from core.probe.mapbackup import differences, fits
from core.safety.guard import AvailableDisk

BACKUP = "backup"
RESTORE = "restore"
DD = "/bin/dd"
MKDIR = "/bin/mkdir"


def backup_folder(home: str, now: datetime, disk: str) -> str:
    """Where a backup made NOW of DISK goes. The name is the disk's at that moment only."""
    return os.path.join(home, ".diskmap", "backups", f"{now:%Y%m%d-%H%M%S}-{disk}")


def backup_steps(current: Disk | Unpartitioned, folder: str) -> tuple[list[Step], tuple[MapBackedUp, ...]]:
    """Steps that back up CURRENT's map into FOLDER, and the preflight each writing step
    after them carries. A disk without a partition table: none of either (D17)."""
    if isinstance(current, Unpartitioned):
        return [], ()
    disk, bs, blocks = current.bsd_name, current.block_size, current.blocks
    if bs not in BLOCK_SIZES:
        raise PlanError(f"{disk}: {bs}-byte blocks; a map backup knows {' and '.join(map(str, BLOCK_SIZES))}")
    if current.scheme == MBR and any(p.partition_id > 4 for p in current.partitions):
        raise PlanError(f"{disk}: logical partitions in an extended one; a map backup does not cover the EBR chain")
    h, t = head_blocks(bs), tail_blocks(bs)
    raw = f"/dev/r{disk}"
    backed = MapBackedUp(disk, folder)
    if current.scheme == GPT:
        head_what, tail_what = "the protective MBR, the GPT header and entries", "the backup GPT's entries and header"
    else:
        head_what, tail_what = "the MBR and the blocks a GPT would take", "where a GPT would keep its backup"
    steps = [
        _local(f"create {os.path.dirname(folder)} if missing", (MKDIR, "-p", os.path.dirname(folder))),
        _local(
            "create the folder for this backup — fails if it exists, so no earlier backup is overwritten",
            (MKDIR, folder),
        ),
        Step(
            intent=f"back up blocks 0..{h - 1} of {disk}: {head_what}",
            command=(DD, f"if={raw}", f"of={os.path.join(folder, HEAD_FILE)}", f"bs={bs}", f"count={h}"),
            needs_root=True,
            writes_disk=False,
            stdin=Closed(),
            preflight=(DiskAvailable(disk),),
            postflight=(),
            rollback=None,
            destroys=(),
        ),
        Step(
            intent=f"back up blocks {blocks - t}..{blocks - 1} of {disk}: {tail_what}",
            command=(
                DD,
                f"if={raw}",
                f"of={os.path.join(folder, TAIL_FILE)}",
                f"bs={bs}",
                f"skip={blocks - t}",
                f"count={t}",
            ),
            needs_root=True,
            writes_disk=False,
            stdin=Closed(),
            preflight=(DiskAvailable(disk),),
            postflight=(backed, MapIs(disk, folder)),  # the copy is whole and is the map the kernel shows
            rollback=None,
            destroys=(),
        ),
    ]
    return steps, (backed,)


def backup_notes(current: Disk | Unpartitioned, folder: str) -> list[str]:
    """What the preview says about the backup steps of a plan."""
    disk = current.bsd_name
    if isinstance(current, Unpartitioned):
        return [f"{disk} has no partition table: there is no map to back up (D17)."]
    bs = current.block_size
    return [
        (
            f"Steps 1-4 back up the partition map of {disk} to {folder}; dd reports {head_blocks(bs)}+0 and "
            f"{tail_blocks(bs)}+0 records in and out — anything less is not a whole backup. To put the map back: "
            f"diskmap restore-map {folder} {disk}"
        ),
    ]


def plan_backup(target: AvailableDisk, current: Disk | Unpartitioned, folder: str) -> ExecutionPlan:
    disk = _target(target, current)
    if isinstance(current, Unpartitioned):
        raise PlanError(f"{disk} has no partition table: there is no map to back up (D17)")
    steps, _ = backup_steps(current, folder)
    kept = tuple(
        KeptPartition(p.bsd_name, Predicted(p.start_lba, p.blocks, p.type, None), what(p)) for p in current.partitions
    )
    notes = [f"Nothing on {disk} changes: its blocks are only read.", *backup_notes(current, folder)]
    return ExecutionPlan(
        BACKUP,
        disk,
        current.blocks,
        current.block_size,
        layout_of(current),
        tuple(k.predicted for k in kept),
        (),
        tuple(steps),
        tuple(notes),
        kept=kept,
        scheme=current.scheme,
    )


def plan_restore(
    target: AvailableDisk,
    current: Disk | Unpartitioned,
    io: IoDisk,
    source: MapDump,
    source_folder: str,
    folder: str,
) -> ExecutionPlan:
    """Put the map in SOURCE (read from SOURCE_FOLDER) back on the disk, backing up the
    current one to FOLDER first. IO is the kernel's map of the disk now."""
    disk = _target(target, current)
    if io.bsd_name != disk:
        raise PlanError(f"kernel's map of {io.bsd_name} given for {disk}")
    if isinstance(current, Unpartitioned):
        raise PlanError(
            f"{disk} has no partition table now: a restore would write over a file system that may span the "
            "whole disk — not supported"
        )
    unfit = fits(source, io)
    if unfit:
        raise PlanError(f"{source_folder} is not a backup of a disk like {disk}: {unfit[0]}")
    if not differences(source, io):
        raise PlanError(f"the partition map of {disk} already is the one in {source_folder}: nothing to restore")
    if current.scheme == MBR and source.scheme == GPT:
        raise PlanError(
            f"{disk} has an MBR: a GPT restored over it would write blocks at both ends that may belong to "
            "its partitions — not supported"
        )
    if os.path.normpath(source_folder) == os.path.normpath(folder):
        raise PlanError(f"{folder}: the new backup would go where the one being restored is")

    steps, backed = backup_steps(current, folder)
    unmount = unmount_step(disk)
    steps.append(unmount)
    bs, blocks = current.block_size, current.blocks
    h, t = head_blocks(bs), tail_blocks(bs)
    writes = (DiskAvailable(disk), DiskUnmounted(disk), *backed, MapBackedUp(disk, source_folder))
    first = len(steps) + 1
    if current.scheme == GPT:
        steps.append(
            _write(
                disk,
                f"write the backup's last {t} blocks to {disk} — the kernel goes by the primary GPT, "
                "so the map now still stands",
                source_folder,
                folder,
                TAIL_FILE,
                bs,
                t,
                blocks - t,
                (*writes, MapIs(disk, folder)),
                MapIs(disk, folder),
            )
        )
        count = h
    else:
        count = 1  # MBR over MBR: block 0 is the whole table; the blocks after it are a partition's
    steps.append(
        _write(
            disk,
            f"write the backup's first {count} block{'s' if count > 1 else ''} to {disk}: the map becomes the backup's",
            source_folder,
            folder,
            HEAD_FILE,
            bs,
            count,
            0,
            (*writes, MapIs(disk, folder)),
            MapIs(disk, source_folder),
        )
    )

    restored = tuple(
        RestoredPartition(e.index, Predicted(e.start_lba, e.blocks, e.type, None), _type_of(source.scheme, e.type))
        for e in source.entries
    )
    leaving = [p.bsd_name for p in current.partitions if not any(_same_place(p, e) for e in source.entries)]
    notes = backup_notes(current, folder)
    if leaving:
        notes.append(
            f"{', '.join(leaving)} {'leaves' if len(leaving) == 1 else 'leave'} the map; the data on "
            f"{'it' if len(leaving) == 1 else 'them'} is not erased, and the backup of steps 1-4 brings "
            f"{'it' if len(leaving) == 1 else 'them'} back."
        )
    notes += [
        (
            "A restore brings back partitions, not data: a partition whose blocks were rewritten since the backup "
            "was made comes back with what is there now."
        ),
        (
            f"Steps {first}-{len(steps)} can be undone in reverse order from the backup of steps 1-4. "
            f"The volumes stay unmounted: {DISKUTIL} mountDisk {disk} mounts the ones that are intact."
        ),
    ]
    return ExecutionPlan(
        RESTORE,
        disk,
        blocks,
        bs,
        layout_of(current),
        tuple(r.predicted for r in restored),
        (),
        tuple(steps),
        tuple(notes),
        restored=restored,
        scheme=source.scheme,
    )


def _target(target: AvailableDisk, current: Disk | Unpartitioned) -> str:
    if not isinstance(target, AvailableDisk):
        raise TypeError("the planner accepts only a disk the guard issued (rules.md §S8)")
    if current.bsd_name != target.bsd_name:
        raise PlanError(f"layout of {current.bsd_name} given for {target.bsd_name}")
    return target.bsd_name


def _local(intent: str, command: tuple[str, ...]) -> Step:
    """A step on diskmap's own files: no root, nothing on the disk."""
    return Step(
        intent=intent,
        command=command,
        needs_root=False,
        writes_disk=False,
        stdin=Closed(),
        preflight=(),
        postflight=(),
        rollback=None,
        destroys=(),
    )


def _write(
    disk: str,
    intent: str,
    source: str,
    undo: str,
    name: str,
    bs: int,
    count: int,
    seek: int,
    preflight: tuple[Assertion, ...],
    after: MapIs,
) -> Step:
    """dd COUNT blocks of SOURCE/NAME to the disk at block SEEK; undone from UNDO/NAME."""

    def dd(folder: str) -> tuple[str, ...]:
        where = (f"seek={seek}",) if seek else ()
        return (DD, f"if={os.path.join(folder, name)}", f"of=/dev/r{disk}", f"bs={bs}", *where, f"count={count}")

    return Step(
        intent=intent,
        command=dd(source),
        needs_root=True,
        writes_disk=True,
        stdin=Closed(),
        preflight=preflight,
        postflight=(after,),
        rollback=Step(
            intent=f"put back the same blocks from {undo}",
            command=dd(undo),
            needs_root=True,
            writes_disk=True,
            stdin=Closed(),
            preflight=(DiskAvailable(disk), DiskUnmounted(disk), MapBackedUp(disk, undo)),
            postflight=(),
            rollback=None,
            destroys=(),
        ),
        destroys=(),  # the blocks at both ends hold the map, not a volume's data (K11)
    )


def _type_of(scheme: str, kind: str) -> str:
    if scheme == MBR:
        return f"MBR type {kind}"
    t = gpt_types.known(kind)
    return t.name if t else "type not known to diskmap"


def _same_place(p: Partition, e: Entry) -> bool:
    return (p.start_lba, p.blocks) == (e.start_lba, e.blocks)
