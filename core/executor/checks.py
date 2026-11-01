"""A step's assertions, checked against the system now (T-050; core.planner.plan).

Every assertion type has its check here; one without a check stops the plan, it never
passes. A check that does not hold raises NotMet with what it found — not AssertionError,
which `python -O` strips. Reads only, through core.probe and the guard.

Right after a write the IORegistry may still be rebuilding the disk's children: reads of
the map go through settled().
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TypeVar

from core.model.layout import Disk, Unpartitioned
from core.model.mapdump import BadBackup
from core.model.resize import NotResizable
from core.planner.plan import (
    Assertion,
    DiskAvailable,
    DiskUnmounted,
    Extent,
    GrowAllowed,
    LayoutIs,
    MapBackedUp,
    MapIs,
    Mounted,
    MountedBy,
    Probed,
    ShrinkAllowed,
    SliceIs,
    Unmounted,
    VolumeIs,
)
from core.probe import system
from core.probe.errors import CommandFailed, ProbeError
from core.probe.holders import read_holders
from core.probe.ioreg import IoDisk, parse_disk, read_iomedia
from core.probe.layout import read_layout
from core.probe.mapbackup import differences, fits, read_backup
from core.probe.mounts import read_mounts
from core.probe.resize import read_limits
from core.probe.system import SystemView
from core.probe.volume import parse_info, read_info
from core.safety.guard import NotAvailable, require_available

T = TypeVar("T")

# How long Probed waits for DiskArbitration's re-probe (rig, 2026-10-17: 0.2-0.7 s).
PROBE_WAIT = 10.0

_SLICE = re.compile(r"^(disk[0-9]+)s([0-9]+)$")


class NotMet(Exception):
    """An assertion does not hold now; the message says what was found."""


def settled(read: Callable[[], T]) -> T:
    """READ right after the map changed. The IORegistry may still be rebuilding the
    disk's children: `ioreg` then exits 1 with "can't obtain child" (seen once in ~170
    runs, 2026-09-27). Only that error is retried, for up to 4 s; anything else stops at once."""
    for attempt in range(5):
        try:
            return read()
        except CommandFailed as e:
            if "can't obtain child" not in str(e) or attempt == 4:
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


def io_disk(disk: str) -> IoDisk:
    return settled(lambda: parse_disk(read_iomedia(), disk))


def extents(disk: str) -> LayoutIs:
    """DISK's map now, as a LayoutIs compares it."""
    d = settled(lambda: read_layout(disk))
    if isinstance(d, Unpartitioned):
        return LayoutIs(disk, None, ())
    assert isinstance(d, Disk)
    return LayoutIs(disk, d.scheme, tuple(Extent(p.start_lba, p.blocks, p.type) for p in d.partitions))


def mounted(view: SystemView, disk: str) -> dict[str, tuple[str, ...]]:
    """Device → mount points of everything of DISK that is mounted: a file system over the
    whole disk, its partitions, and the volumes of every APFS container with a store on it."""
    found: dict[str, tuple[str, ...]] = {}
    for d in view.disks:
        if d.bsd_name != disk and not any(_whole(s) == disk for s in d.physical_stores):
            continue
        if d.mount_point is not None:
            found[d.bsd_name] = (d.mount_point,)
        for v in d.volumes:
            if v.mount_points:
                found[v.bsd_name] = v.mount_points
    return found


def check(a: Assertion) -> None:
    """Raise NotMet unless A holds now. A probe that fails is NotMet too: what cannot be
    read is not known to hold."""
    try:
        _check(a)
    except ProbeError as e:
        raise NotMet(f"cannot check {type(a).__name__}: {e}") from None


def _check(a: Assertion) -> None:
    if isinstance(a, DiskAvailable):
        try:
            require_available(a.disk)
        except NotAvailable as e:
            raise NotMet(str(e)) from None
    elif isinstance(a, MapBackedUp | MapIs):
        try:
            dump = read_backup(a.folder)
        except BadBackup as e:
            raise NotMet(f"{a.folder}: not an intact map backup — {e}") from None
        io = io_disk(a.disk)
        problems = fits(dump, io) if isinstance(a, MapBackedUp) else differences(dump, io)
        if problems:
            what = "does not fit" if isinstance(a, MapBackedUp) else "is not the map of"
            raise NotMet(f"the backup in {a.folder} {what} {a.disk} now: " + "; ".join(problems))
    elif isinstance(a, DiskUnmounted):
        held = mounted(system.probe(), a.disk)
        if held:
            raise NotMet(f"{a.disk} still has mounted volumes:" + _held(held))
    elif isinstance(a, LayoutIs):
        now = extents(a.disk)
        if now != a:
            raise NotMet(f"the map of {a.disk} is not the one the plan says:\n  plan: {_map(a)}\n  now:  {_map(now)}")
    elif isinstance(a, SliceIs):
        m = _SLICE.match(a.slice)
        if m is None:
            raise NotMet(f"{a.slice}: not a partition name")
        now = extents(m[1])
        n = int(m[2])
        found = now.partitions[n - 1] if n <= len(now.partitions) else None
        if found != a.extent:
            raise NotMet(f"{a.slice} is not where the plan says: plan {_ext(a.extent)}, now {_ext(found)}")
    elif isinstance(a, Unmounted):
        v = parse_info(read_info(a.slice), a.slice).volume
        if v.mounted:
            raise NotMet(f"{a.slice} is mounted")
    elif isinstance(a, Mounted):
        v = parse_info(read_info(a.slice), a.slice).volume
        if not v.mounted:
            raise NotMet(f"{a.slice} is not mounted")
    elif isinstance(a, VolumeIs):
        v = parse_info(read_info(a.slice), a.slice).volume
        if (v.filesystem, v.label) != (a.bundle, a.name):
            raise NotMet(f"{a.slice} is {v.filesystem} {v.label!r}, the plan says {a.bundle} {a.name!r}")
    elif isinstance(a, Probed):
        deadline = time.monotonic() + PROBE_WAIT
        while True:
            v = parse_info(read_info(a.slice), a.slice).volume
            if a.bundle is None and v.confirmed and v.label == a.name:
                break
            if a.bundle is not None and (v.filesystem, v.label) == (a.bundle, a.name):
                break
            if time.monotonic() >= deadline:
                says = a.bundle or "a file system"
                raise NotMet(
                    f"{a.slice} is {v.filesystem} {v.label!r} {PROBE_WAIT:g} s on, the plan says {says} {a.name!r}: "
                    "macOS has not recognized the new file system"
                )
            time.sleep(0.25)
    elif isinstance(a, MountedBy):
        found = [m for m in read_mounts() if m.device == a.slice]
        if not found:
            raise NotMet(f"{a.slice} is not mounted; the plan says {a.by} mounts it")
        m = found[0]
        if m.by != a.by:
            how = "read-write" if m.writable else "read-only"
            raise NotMet(f"{a.slice} is mounted by {m.by} ({how}, at {m.point}); the plan says {a.by}")
    elif isinstance(a, ShrinkAllowed):
        lim = read_limits(a.slice, a.apfs)
        if isinstance(lim, NotResizable):
            raise NotMet(f"{a.slice}: diskutil gives no resize limits now ({lim.why}): {lim.said}")
        if a.size < lim.minimum:
            raise NotMet(
                f"{a.slice} can be shrunk to no less than {lim.minimum} bytes now (files written since the plan?); "
                f"the plan asks {a.size}"
            )
        if a.size >= lim.current:
            raise NotMet(f"{a.slice} is {lim.current} bytes now; the plan shrinks it to {a.size}")
    elif isinstance(a, GrowAllowed):
        lim = read_limits(a.slice, a.apfs)
        if isinstance(lim, NotResizable):
            raise NotMet(f"{a.slice}: diskutil gives no resize limits now ({lim.why}): {lim.said}")
        if a.size <= lim.current:
            raise NotMet(f"{a.slice} is {lim.current} bytes now; the plan grows it to {a.size}")
    else:
        raise NotMet(f"no check for {type(a).__name__}: the plan cannot run")


def _held(held: dict[str, tuple[str, ...]]) -> str:
    """The mounted volumes, and who holds files open on each (lsof, best-effort)."""
    parts = []
    for dev, points in sorted(held.items()):
        line = f"{dev} on {', '.join(points)}"
        for p in points:
            who = read_holders(p)
            if who:
                line += f"\n    open on {p}:\n      " + "\n      ".join(who.splitlines())
        parts.append(line)
    return "\n  " + "\n  ".join(parts)


def _whole(slice_name: str) -> str | None:
    m = _SLICE.match(slice_name)
    return m[1] if m else None


def _ext(e: Extent | None) -> str:
    return "nothing" if e is None else f"{e.start_lba}+{e.blocks} {e.type}"


def _map(m: LayoutIs) -> str:
    return f"{m.scheme or 'no table'}: " + (", ".join(_ext(e) for e in m.partitions) or "no partitions")
