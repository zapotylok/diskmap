"""What the strategies share (T-042, T-043, T-044): formats and labels, sizes, the extra
steps of formats diskutil cannot make, and placing new partitions in a free tail.

ext4 by mke2fs (T-053, D33): macOS does not notice a file system written by a tool of
its own — not after mke2fs, newfs_msdos or a write to either node, not on `diskutil
mount` or `mountDisk` (rig, 2026-10-17). It probes the slice again only when the kernel
re-reads the map: when /dev/rdiskN of the whole disk, opened for writing, is closed —
`dd` that copies nothing does it. That open is refused while any volume of the disk is
mounted, so the volumes are unmounted first and mounted again after; mountDisk mounts
every volume it can, a formatted ESP too, so this comes before efi and ntfs are made.

Runs no commands (§S3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from core.model import gpt_types as T
from core.model.apfs import ApfsContainer
from core.model.desired import FIXED, PERCENT, REST, DesiredLayout, LayoutError, human, resolve
from core.model.doctor import NO, RO, Ext4Mounting, TrialNow
from core.model.filesystems import Registry
from core.model.layout import Disk, Partition, Unpartitioned
from core.model.volume import VolumeInfo
from core.planner import formats as F
from core.planner.plan import (
    Assertion,
    Closed,
    DiskAvailable,
    DiskUnmounted,
    Extent,
    LayoutIs,
    MountedBy,
    PlannedPartition,
    Probed,
    SliceIs,
    Step,
    Unmounted,
    VolumeIs,
)
from core.planner.predict import BLOCK, KIB4, MIB, Add, Predicted, Unpredictable, predict_add

DISKUTIL = "/usr/sbin/diskutil"
NEWFS_MSDOS = "/sbin/newfs_msdos"
DD = "/bin/dd"
GIB_BYTES = 2**30


class PlanError(ValueError):
    """No plan: the message says why. LayoutError (desired.py) is raised as is."""


@dataclass(frozen=True)
class Tools:
    """Formatters outside diskutil, as found and working (diskmap doctor). None: absent."""

    mkntfs: str | None
    mke2fs: str | None = None
    ext4: Ext4Mounting | None = None  # who would mount ext4, observed now; None: not observed
    trial: TrialNow | None = None  # the last trial mount against this Mac (T-026); None: not read


def formats_of(desired: DesiredLayout, registry: Registry, tools: Tools) -> list[F.Format]:
    """The format of each desired partition, its label checked against it."""
    fmts = [format_of(p.format, n, registry) for n, p in enumerate(desired.partitions, 1)]
    for n, (p, f) in enumerate(zip(desired.partitions, fmts), 1):
        check_label(p.label, f, n)
        if f.kind == F.NTFS and tools.mkntfs is None:
            raise PlanError(f"partition {n}: ntfs needs mkntfs (ntfs-3g), which is not found — see diskmap doctor")
        if f.kind == F.EXT4 and tools.mke2fs is None:
            raise PlanError(f"partition {n}: ext4 needs mke2fs (e2fsprogs), which is not found — see diskmap doctor")
        if f.kind == F.EXT4 and tools.ext4 is None:
            # The preview must say who mounts it before anything is formatted (T-026).
            raise PlanError(f"partition {n}: internal: who would mount ext4 on this Mac was not observed")
    return fmts


def format_of(written: str, n: int, registry: Registry) -> F.Format:
    try:
        return F.resolve_format(written, registry)
    except F.FormatError as e:
        raise PlanError(f"partition {n}: {e}") from None


LABEL_REQUIRED = "required"
LABEL_OPTIONAL = "optional"
LABEL_NONE = "none"


def label_rule(f: F.Format) -> str:
    """Whether a partition of format F needs a label, may have one, or must have none —
    the rule check_label enforces, for whoever asks first (the web UI, T-061)."""
    if f.kind == F.PERSONALITY:
        # diskutil refuses an empty name before touching the map (rig, 2026-09-27);
        # diskmap does not invent "Untitled".
        return LABEL_REQUIRED
    if f.kind == F.EFI:
        return LABEL_NONE  # always EFI
    return LABEL_OPTIONAL


def check_label(label: str | None, f: F.Format, n: int) -> None:
    where = f"partition {n}"
    rule = label_rule(f)
    if label is not None and label.startswith("-"):
        # It would stand where a tool may read an option.
        raise PlanError(f"{where}: label {label!r} starts with '-'")
    if label is not None and len(label) > 1 and label.startswith("%") and label.endswith("%"):
        raise PlanError(f"{where}: label {label!r} reads as one of diskutil's %…% keywords")
    if rule == LABEL_REQUIRED and label is None:
        raise PlanError(f"{where}: {f.written} needs a label — diskutil formats no volume without a name")
    if rule == LABEL_NONE and label is not None:
        raise PlanError(f"{where}: an EFI partition is always labelled EFI; leave the label out")
    if f.kind == F.EXT4 and label is not None and len(label.encode()) > F.EXT4_LABEL_BYTES:
        # mke2fs would cut it with only a warning.
        raise PlanError(f"{where}: an ext4 label holds at most {F.EXT4_LABEL_BYTES} bytes, {label!r} has more")


def check_size_limits(n: int, f: F.Format, b: int) -> None:
    if f.min_size is not None and b < f.min_size:
        raise PlanError(f"partition {n}: {f.written} needs at least {human(f.min_size)}, got {human(b)}")
    if f.max_size is not None and b > f.max_size:
        raise PlanError(f"partition {n}: {f.written} takes at most {human(f.max_size)}, got {human(b)}")


def makes(f: F.Format) -> str:
    if f.kind == F.NTFS:
        return "NTFS (mkntfs)"
    if f.kind == F.EFI:
        return "EFI system partition, FAT32 (newfs_msdos)"
    if f.kind == F.EXT4:
        return "ext4 (mke2fs)"
    assert f.personality is not None
    return f.personality.user_visible_name


def byte_sizes(desired: DesiredLayout, capacity: int) -> list[int]:
    """Bytes per partition, each a multiple of 4 KiB, summing to at most CAPACITY."""
    raw = resolve(desired, capacity)  # every rejection with the user's numbers first
    k = KIB4 * BLOCK
    rest = None
    out: list[int] = []
    for n, (p, b) in enumerate(zip(desired.partitions, raw), 1):
        if p.size.kind == FIXED:
            out.append(-(-b // k) * k)
        elif p.size.kind == PERCENT:
            out.append(b // k * k)
            if out[-1] == 0:
                raise LayoutError(f"partition {n}: {p.size} of {human(capacity)} is less than 4 KiB")
        else:
            rest = n - 1
            out.append(0)
    taken = sum(out)
    if rest is not None:
        left = capacity - taken  # a multiple of 4 KiB: so are capacity and every other size
        if left <= 0:
            raise LayoutError(f"partition {rest + 1}: rest has no room once sizes are rounded to 4 KiB")
        out[rest] = left
    elif taken > capacity:
        raise LayoutError(
            f"partitions take {human(taken)} once rounded up to 4 KiB, more than the {human(capacity)} available"
        )
    return out


def size_arg(nbytes: int) -> str:
    """Exact size in a unit diskutil was measured with: Gi, Mi, G, M — the shortest that
    is exact — else plain bytes."""
    out = str(nbytes)
    for unit, mult in (("Gi", 2**30), ("G", 10**9), ("Mi", 2**20), ("M", 10**6)):
        if nbytes % mult == 0 and len(f"{nbytes // mult}{unit}") < len(out):
            out = f"{nbytes // mult}{unit}"
    return out


def closed_for(fmts: list[F.Format]) -> Closed:
    """stdin of a diskutil command formatting with FMTS: closed, with the questions diskutil
    is known to ask and answer itself for them (D27), each once."""
    asks: list[tuple[str, str]] = []
    for f in fmts:
        asks += [qa for qa in f.asks if qa not in asks]
    return Closed(tuple(asks))


def triple(f: F.Format, label: str | None, nbytes: int) -> list[str]:
    """`<format> <name> <size>` as partitionDisk, addPartition and resizeVolume take it."""
    if f.kind == F.PERSONALITY:
        assert f.personality is not None and label is not None
        return [f.personality.name, label, size_arg(nbytes)]
    return [f"%{f.type}%", "%noformat%", size_arg(nbytes)]


# --- new partitions in a free tail (addPartition, resizeVolume: D25, D26) ---


@dataclass(frozen=True)
class Tail:
    sizes: list[int]  # bytes asked of diskutil, per desired partition
    new: tuple[Predicted, ...]  # where they land
    rest: int | None  # index of the `rest` partition
    last: int  # the usable end diskutil fills to, or the last free block of the gap
    gap: bool = False  # between partitions (T-075), not the tail


def place_in_tail(
    disk: str,
    cursor: int,
    after: str,
    blocks: int,
    desired: DesiredLayout,
    fmts: list[F.Format],
    place: Callable[[tuple[Add, ...]], tuple[Predicted, ...]] | None = None,
    before_lba: int | None = None,
) -> Tail:
    """New partitions from CURSOR (just past AFTER, as messages name it) to the end of the
    usable area — or to BEFORE_LBA, where the next partition starts (a gap, T-075) — by
    predict_add's rules, or by PLACE when the command has more of its own (resizeContainer,
    D26). Percentages are of the free space; `rest` is the largest size that fits, not a
    subtraction."""
    if place is None:

        def place(adds: tuple[Add, ...]) -> tuple[Predicted, ...]:
            return predict_add(cursor, adds, blocks, BLOCK, before_lba)

    last = blocks - 34 if before_lba is None else before_lba - 1  # the usable end diskutil fills to (rig)
    start0 = -(-cursor // KIB4) * KIB4
    area_end = (last + 1) // KIB4 * KIB4
    if area_end - start0 < MIB:
        raise PlanError(f"{disk}: no free space after {after}")
    sizes = byte_sizes(desired, (area_end - start0) * BLOCK)

    def adds(sz: list[int]) -> tuple[Add, ...]:
        return tuple(Add(f.type, b, f.kind == F.PERSONALITY) for f, b in zip(fmts, sz))

    for n, (f, b) in enumerate(zip(fmts, sizes), 1):
        if f.kind == F.EXT4 and b >= GIB_BYTES:
            # Else `rest` would quietly come out just under 1 GiB.
            raise PlanError(
                f"partition {n}: ext4 of {human(b)} after {after}: diskutil puts a 128 MiB Apple_Boot after a blank "
                "Linux slot of 1 GiB or more (rig) — give it a size under 1 GiB, or use the full repartition"
            )
    rest = next((i for i, p in enumerate(desired.partitions) if p.size.kind == REST), None)
    if rest is not None:
        sizes[rest] = largest_rest(sizes, rest, lambda sz: place(adds(sz)))
    try:
        new = place(adds(sizes))
    except Unpredictable as e:
        raise PlanError(f"diskutil's result is not predictable: {e}") from None
    for n, (q, f) in enumerate(zip(new, fmts), 1):
        check_size_limits(n, f, q.blocks * BLOCK)
    return Tail(sizes, new, rest, last, before_lba is not None)


def tail_notes(tail: Tail, fmts: list[F.Format]) -> list[str]:
    """What diskutil takes off the sizes asked."""
    notes = []
    for n, (q, f, b) in enumerate(zip(tail.new, fmts, tail.sizes), 1):
        if q.blocks * BLOCK < b and f.type == T.MS_BASIC_DATA.guid and n - 1 != tail.rest:
            notes.append(
                f"Partition {n}: Microsoft basic data starts and ends on a MiB boundary, "
                f"{human(b - q.blocks * BLOCK)} less than asked."
            )
        if q.blocks * BLOCK < b and f.type == T.APPLE_HFS.guid:
            notes.append(
                f"Partition {n}: diskutil keeps {human(b - q.blocks * BLOCK)} of it as booter space (HFS+ over 1 GiB)."
            )
    return notes


def free_at_end(tail: Tail) -> list[str]:
    end = tail.new[-1].end_lba + 1
    if (tail.last + 1 - end) * BLOCK >= 2**20:
        where = "of the gap" if tail.gap else "at the end"
        return [f"About {human((tail.last + 1 - end) * BLOCK)} {where} stays free."]
    return []


def largest_rest(sizes: list[int], i: int, fits: Callable[[list[int]], object]) -> int:
    """The largest multiple of 4 KiB for partition I, up to what byte_sizes gave it, with
    which the prediction still fits."""
    k = KIB4 * BLOCK
    lo, hi = 0, sizes[i] // k  # in 4 KiB units; lo fits (as 0: nothing), hi may not

    def ok(units: int) -> bool:
        trial = list(sizes)
        trial[i] = units * k
        try:
            fits(trial)
        except Unpredictable:
            return False
        return True

    if ok(hi):
        return hi * k
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    if lo == 0:
        raise LayoutError(f"partition {i + 1}: rest has no room once alignment and booter space are counted")
    return lo * k


def check_slice_names(disk: str, current: Disk) -> None:
    """New slices are named s(N+1)… only if the existing ones are diskNs1..sN in table order."""
    ids = [p.partition_id for p in current.partitions]
    names = [p.bsd_name for p in current.partitions]
    if sorted(ids) != list(range(1, len(ids) + 1)) or names != [f"{disk}s{i}" for i in ids] or ids != sorted(ids):
        raise PlanError(
            f"{disk}: its partitions are not {disk}s1..s{len(ids)} in table order, so the names "
            "of new slices cannot be known in advance"
        )


def unmount_step(disk: str) -> Step:
    """Unmount every volume of DISK before a write that needs none mounted (T-011).
    diskutil names the process that holds a file open when it cannot (rig, 2026-10-11);
    it may unmount some volumes and not others, so the postflight checks them all."""
    return Step(
        intent=f"unmount every volume of {disk}; fails, naming the process that holds a file open, if one cannot be",
        command=(DISKUTIL, "unmountDisk", disk),
        needs_root=False,
        writes_disk=False,
        stdin=Closed(),
        preflight=(DiskAvailable(disk),),
        postflight=(DiskUnmounted(disk),),
        rollback=Step(
            intent=f"mount the volumes of {disk} again",
            command=(DISKUTIL, "mountDisk", disk),
            needs_root=False,
            writes_disk=False,
            stdin=Closed(),
            preflight=(),
            postflight=(),
            rollback=None,
            destroys=(),
        ),
        destroys=(),
    )


def format_steps(
    pp: PlannedPartition, f: F.Format, disk: str, tools: Tools, backed: tuple[Assertion, ...]
) -> list[Step]:
    """BACKED: the plan's MapBackedUp, carried by each step that writes (empty: no table, D17)."""
    here = SliceIs(pp.slice, extent(pp.predicted))
    if f.kind == F.NTFS:
        assert tools.mkntfs is not None
        label = ["-L", pp.label] if pp.label is not None else []
        # ntfsprogs want the block node, not rdisk (rig trap); a blank slot is not mounted.
        return [
            Step(
                intent=f"make NTFS on {pp.slice}" + (f' "{pp.label}"' if pp.label else ""),
                command=(tools.mkntfs, "-Q", *label, f"/dev/{pp.slice}"),
                needs_root=True,  # a real disk's node is root:operator 640 (K7)
                writes_disk=True,
                stdin=Closed(),
                preflight=(DiskAvailable(disk), here, Unmounted(pp.slice), *backed),
                postflight=(here,),
                rollback=None,
                destroys=(),  # the slot was cut blank by the step before
            ),
            Step(
                intent=f"mount {pp.slice} so that macOS re-reads it as NTFS",
                command=(DISKUTIL, "mount", pp.slice),
                needs_root=False,
                writes_disk=False,
                stdin=Closed(),
                preflight=(here,),
                postflight=(VolumeIs(pp.slice, "ntfs", pp.label),),
                rollback=Step(
                    intent=f"unmount {pp.slice}",
                    command=(DISKUTIL, "unmount", pp.slice),
                    needs_root=False,
                    writes_disk=False,
                    stdin=Closed(),
                    preflight=(),
                    postflight=(Unmounted(pp.slice),),
                    rollback=None,
                    destroys=(),
                ),
                destroys=(),
            ),
        ]
    if f.kind == F.EXT4:
        assert tools.mke2fs is not None
        label = ["-L", pp.label] if pp.label is not None else []
        return [
            Step(
                intent=f"make ext4 on {pp.slice}" + (f' "{pp.label}"' if pp.label else ""),
                # -F: in a terminal mke2fs asks "Proceed anyway? (y,N)" when the slot still holds
                # an old file system, without one it does not (rig) — the same either way (§S6).
                command=(tools.mke2fs, "-F", "-t", "ext4", *label, f"/dev/r{pp.slice}"),
                needs_root=True,  # a real disk's node is root:operator 640 (K7)
                writes_disk=True,
                stdin=Closed(),
                preflight=(DiskAvailable(disk), here, Unmounted(pp.slice), *backed),
                postflight=(here,),
                rollback=None,
                destroys=(),  # the slot was cut blank by the step before
            )
        ]
    if f.kind == F.EFI:
        return [
            Step(
                intent=f"make the EFI system partition on {pp.slice} (FAT32, EFI)",
                command=(NEWFS_MSDOS, "-F", "32", "-v", "EFI", f"/dev/r{pp.slice}"),
                needs_root=True,
                writes_disk=True,
                stdin=Closed(),
                preflight=(DiskAvailable(disk), here, Unmounted(pp.slice), *backed),
                postflight=(here,),
                rollback=None,
                destroys=(),
            )
        ]
    return []


def format_all(
    planned: tuple[PlannedPartition, ...],
    fmts: list[F.Format],
    disk: str,
    tools: Tools,
    backed: tuple[Assertion, ...],
    layout: LayoutIs,
) -> tuple[list[Step], list[str]]:
    """The steps that make the file systems diskutil does not, and what the preview says
    about them. LAYOUT: the disk's map once the plan has cut every slot. ext4 first, then
    macOS made to see it (module docstring), then efi and ntfs."""
    pairs = list(zip(planned, fmts))
    ext4 = [pp for pp, f in pairs if f.kind == F.EXT4]
    steps = [s for pp, f in pairs if f.kind == F.EXT4 for s in format_steps(pp, f, disk, tools, backed)]
    notes: list[str] = []
    if ext4:
        m = tools.ext4
        assert m is not None  # formats_of refuses ext4 without it
        which = ", ".join(pp.slice for pp in ext4)
        if m.bundle is not None and m.driver is not None:
            steps += _recognize(disk, ext4, m.bundle, layout)
            notes.append(
                f"ext4 ({which}): {m.bundle} mounts it ({m.driver}) — observed now, checked after mounting. "
                f"For macOS to see a file system mke2fs made, every volume of {disk} is unmounted and mounted "
                "again; mountDisk mounts every volume it can, an EFI partition already formatted included."
            )
        elif m.bundle is not None:
            notes.append(
                f"ext4 ({which}): nothing on this Mac mounts it now — {m.bundle} takes ext, but the driver it "
                "mounts with is not loaded (see diskmap doctor). The volume is made and left unmounted."
            )
        else:
            notes.append(
                f"ext4 ({which}): nothing on this Mac mounts it — diskutil lists no file system bundle that "
                "takes ext (see diskmap doctor). macOS shows the partition as Linux Filesystem with no file "
                "system; the volume is made all the same."
            )
    steps += [s for pp, f in pairs if f.kind != F.EXT4 for s in format_steps(pp, f, disk, tools, backed)]
    return steps, notes


def reread_step(disk: str, layout: LayoutIs, probed: tuple[Probed, ...]) -> Step:
    """Make macOS see a file system a tool of its own wrote (module docstring): the kernel
    re-reads DISK's map, DiskArbitration probes its slices again. Every volume of DISK must
    be unmounted before it."""
    return Step(
        intent=f"open /dev/r{disk} for writing and close it, writing nothing: the kernel re-reads the map "
        "and macOS probes the new ext4 again",
        command=(DD, "if=/dev/null", f"of=/dev/r{disk}", "count=0", "conv=notrunc"),
        needs_root=True,  # opening a real disk's node for writing (K7)
        writes_disk=False,
        stdin=Closed(),
        preflight=(DiskAvailable(disk), layout, DiskUnmounted(disk)),  # busy while a volume is mounted (rig)
        postflight=(layout, *probed),
        rollback=None,
        destroys=(),
    )


def renumber_step(disk: str, layout: LayoutIs) -> Step:
    """Read DISK's map again after a partition was put between two others (T-075, D50): until
    then ioreg marks the ones after it out of date and no probe reads the disk (D24). The same
    zero-byte write as reread_step; the slices then take the numbers of their table entries."""
    return Step(
        intent=f"open /dev/r{disk} for writing and close it, writing nothing: the kernel re-reads the map "
        "and the partitions take the names of their table entries",
        command=(DD, "if=/dev/null", f"of=/dev/r{disk}", "count=0", "conv=notrunc"),
        needs_root=True,  # opening a real disk's node for writing (K7)
        writes_disk=False,
        stdin=Closed(),
        # No LayoutIs before: the map cannot be read yet — that is what this step is for.
        preflight=(DiskAvailable(disk), DiskUnmounted(disk)),  # busy while a volume is mounted (rig)
        postflight=(layout,),
        rollback=None,
        destroys=(),
    )


def mount_step(disk: str) -> Step:
    """Mount every volume of DISK that can be: after unmount_step and a re-read of the map."""
    return Step(
        intent=f"mount every volume of {disk} again",
        command=(DISKUTIL, "mountDisk", disk),
        needs_root=False,
        writes_disk=False,
        stdin=Closed(),
        preflight=(DiskAvailable(disk),),
        postflight=(),
        rollback=Step(
            intent=f"unmount every volume of {disk}",
            command=(DISKUTIL, "unmountDisk", disk),
            needs_root=False,
            writes_disk=False,
            stdin=Closed(),
            preflight=(),
            postflight=(DiskUnmounted(disk),),
            rollback=None,
            destroys=(),
        ),
        destroys=(),
    )


def _recognize(disk: str, ext4: list[PlannedPartition], bundle: str, layout: LayoutIs) -> list[Step]:
    probed = tuple(Probed(pp.slice, bundle, pp.label) for pp in ext4)
    return [
        unmount_step(disk),
        reread_step(disk, layout, probed),
        Step(
            intent=f"mount every volume of {disk} again, the new ext4 included",
            command=(DISKUTIL, "mountDisk", disk),
            needs_root=False,
            writes_disk=False,
            stdin=Closed(),
            preflight=(DiskAvailable(disk), *probed),
            # The mount type `mount` shows for a kext-backed bundle is the bundle's name (rig).
            postflight=tuple(MountedBy(pp.slice, bundle) for pp in ext4),
            rollback=Step(
                intent=f"unmount every volume of {disk}",
                command=(DISKUTIL, "unmountDisk", disk),
                needs_root=False,
                writes_disk=False,
                stdin=Closed(),
                preflight=(),
                postflight=(DiskUnmounted(disk),),
                rollback=None,
                destroys=(),
            ),
            destroys=(),
        ),
    ]


def mounting_lines(planned: tuple[PlannedPartition, ...], fmts: list[F.Format], trial: TrialNow | None) -> list[str]:
    """What the preview says, before anything is formatted, about how this Mac mounts each
    new volume (T-026, D34): from the last trial mount while it holds for this Mac. A volume
    this Mac mounts read-only or not at all is a warning, never a refusal; no other format is
    offered. TRIAL None: not read — nothing is said."""
    if trial is None:
        return []
    made = [(n, pp, f) for n, (pp, f) in enumerate(zip(planned, fmts), 1) if f.kind != F.EFI]
    if not made:
        return []
    if trial.trial is None:
        why = f"the kept trial cannot be read ({trial.problem})" if trial.problem else "no trial mount yet"
        return [
            (
                f"Not checked: {why}. diskmap doctor --trial makes and mounts each format on a disk image of its "
                "own and says how this Mac mounts it."
            )
        ]
    t = trial.trial
    if trial.changed:
        return [
            (
                f"Not known: the trial mount of {t.at} was made on another setup — {'; '.join(trial.changed)}. "
                "Run diskmap doctor --trial again."
            )
        ]
    out = []
    for n, pp, f in made:
        key = f.personality.name if f.kind == F.PERSONALITY and f.personality is not None else f.kind
        who = f"partition {n} ({pp.slice}, {key})"
        c = t.find(key)
        if c is None:
            out.append(f"{who}: not checked — the trial mount of {t.at} did not try {key}.")
        elif c.created is not True:
            out.append(f"WARNING {who}: the trial mount of {t.at} could not make it — {c.detail}. Tried all the same.")
        elif c.mount is None:
            out.append(f"{who}: not known — the trial mount of {t.at} made it and stopped before mounting.")
        elif c.mount.mode == NO:
            out.append(
                f"WARNING {who}: nothing on this Mac mounts it — {c.mount.detail} (trial mount {t.at}). "
                "It is made all the same."
            )
        elif c.mount.mode == RO:
            out.append(
                f"WARNING {who}: this Mac mounts it read-only ({c.mount.by}; trial mount {t.at}) — nothing here "
                "writes to it. It is made all the same."
            )
        else:
            out.append(f"{who}: mounts read-write ({c.mount.by}; trial mount {t.at}).")
    return out


def extent(q: Predicted) -> Extent:
    return Extent(q.start_lba, q.blocks, q.type)


def layout_of(current: Disk | Unpartitioned) -> LayoutIs:
    if isinstance(current, Unpartitioned):
        return LayoutIs(current.bsd_name, None, ())
    return LayoutIs(current.bsd_name, current.scheme, tuple(extent(p) for p in current.partitions))


def fs_name(v: VolumeInfo) -> str:
    name = v.filesystem_name or v.filesystem or "?"
    return name if v.label is None else f'{name} "{v.label}"'


def what(p: Partition) -> str:
    """A partition as the preview names it: 'APFS container disk5: "A"', 'ExFAT "B"'."""
    if p.apfs is not None:
        return container(p.apfs)
    if p.volume is not None and p.volume.filesystem is not None:
        return fs_name(p.volume)
    return p.type_name


def container(c: ApfsContainer) -> str:
    vols = ", ".join(f'"{v.name}"' for v in c.volumes)
    return f"APFS container {c.reference}" + (f": {vols}" if vols else "")
