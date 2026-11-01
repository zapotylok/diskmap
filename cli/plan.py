"""`diskmap plan repartition|add <diskN> <layout.json>` (T-045, T-043; add `--after <diskNsM>`, T-075),
`diskmap plan shrink <diskNsM> --to <size> [<layout.json>]` (T-044),
`diskmap plan grow <diskNsM> --to <size>` (T-074), `diskmap backup-map <diskN>`
and `diskmap restore-map <backup> <diskN>` (T-012): build a plan and show it. Read-only.
Also the one-step plans `diskmap mount|unmount <diskNsM>` runs at once (T-073, K16).

The strategy is always named: the full repartition destroys the disk, and must never be
what a forgotten word defaults to.

One SystemView serves both the guard's verdict and the map shown as NOW, so the two
cannot disagree about which disk this is. The AvailableDisk goes from the guard
straight to the planner and nowhere else (D19). Nothing is applied here. Every plan
that writes starts by backing up the map into a new folder under ~/.diskmap/backups,
named by the time the plan is made (K11).

Each build returns the plan with its preview: `diskmap plan` prints the preview,
`diskmap apply` (cli/apply.py) prints the same and runs that very plan.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cli.preview import render_plan
from cli.show import _access
from core.model.desired import DesiredLayout, LayoutError, parse_layout, parse_size
from core.model.layout import Disk, Unpartitioned
from core.model.mapdump import BadBackup
from core.planner.addpart import plan_add
from core.planner.backup import backup_folder, plan_backup, plan_restore
from core.planner.common import PlanError, Tools
from core.planner.formats import EXT4, FormatError
from core.planner.grow import grow_limit, plan_grow
from core.planner.mount import plan_mount
from core.planner.plan import ExecutionPlan
from core.planner.predict import RESIZE_APFS
from core.planner.repartition import plan_repartition
from core.planner.shrink import plan_shrink, resizable
from core.probe import system
from core.probe.doctor import mke2fs_path, read_ext4_mounting, working_tool
from core.probe.environment import read_untested_macos
from core.probe.filesystems import read_registry
from core.probe.ioreg import parse_disk, read_iomedia
from core.probe.layout import read_layout
from core.probe.mapbackup import read_backup
from core.probe.resize import read_limits
from core.probe.show import DiskMap, Failed, access_of, build_map
from core.probe.trial import read_setup, read_trial
from core.safety.guard import AvailableDisk, assess

STRATEGIES = {"repartition": plan_repartition, "add": plan_add}
SHRINK = "shrink"  # takes a partition and a new size: build_shrink
GROW = "grow"  # takes a partition and a new size, no layout: build_grow


class Refused(Exception):
    """No plan; the message says why, for stderr."""


@dataclass(frozen=True)
class Built:
    plan: ExecutionPlan
    preview: str
    untested: str | None = None  # this macOS is not one diskmap was tested on: said first (K19)


def _built(plan: ExecutionPlan, now: DiskMap, width: int) -> Built:
    untested = read_untested_macos()
    return Built(plan, render_plan(plan, now, width, untested), untested)


def load_layout(text: str, source: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise Refused(f"{source}: not JSON — {e}") from None


def build(
    strategy: str,
    disk_arg: str,
    doc: Any,
    search_path: str,
    home: str,
    width: int,
    at: datetime,
    after: str | None = None,
) -> Built:
    """AFTER: add only — the partition whose following free space to fill (T-075)."""
    name = disk_arg.removeprefix("/dev/")
    try:
        desired = parse_layout(doc)
    except LayoutError as e:
        raise Refused(f"layout: {e}") from None
    verdict, now, current = _mapped(name, width)
    tools = _tools(desired, search_path, home)
    try:
        folder = backup_folder(home, at, name)
        if strategy == "add":
            plan = plan_add(
                verdict, current, desired, read_registry(), tools, folder, after and after.removeprefix("/dev/")
            )
        else:
            plan = STRATEGIES[strategy](verdict, current, desired, read_registry(), tools, folder)
    except (LayoutError, PlanError, FormatError) as e:
        raise Refused(str(e)) from None
    return _built(plan, now, width)


def _tools(desired: DesiredLayout | None, search_path: str, home: str) -> Tools:
    """The formatters found, and — only when the layout asks for ext4 — who would mount it,
    observed now (T-053): the preview says so before anything is formatted."""
    ext4 = desired is not None and any(p.format.strip().casefold() == EXT4 for p in desired.partitions)
    return Tools(
        working_tool("mkntfs", search_path, home),
        mke2fs_path(search_path, home) if ext4 else None,
        read_ext4_mounting() if ext4 else None,
        # How this Mac mounts what the plan makes, said before it is made (T-026).
        read_trial(home, read_setup(search_path, home)) if desired is not None else None,
    )


def build_shrink(
    slice_arg: str, size_text: str, doc: Any | None, search_path: str, home: str, width: int, at: datetime
) -> Built:
    """Shrink SLICE to SIZE_TEXT, adding the partitions of DOC (None: none) after it."""
    name = slice_arg.removeprefix("/dev/")
    m = re.fullmatch(r"(disk[0-9]+)s[0-9]+", name)
    if m is None:
        raise Refused(f"{slice_arg}: expected a partition, diskNsM")
    try:
        new_size = parse_size(size_text)
        desired = parse_layout(doc) if doc is not None else None
    except LayoutError as e:
        raise Refused(f"layout: {e}") from None
    verdict, now, current = _mapped(m[1], width)
    part = next((p for p in current.partitions if p.bsd_name == name), None) if isinstance(current, Disk) else None
    if part is None:
        raise Refused(f"{name} is not a partition of {m[1]}")
    limits = read_limits(name, apfs=part.apfs is not None)
    tools = _tools(desired, search_path, home)
    try:
        folder = backup_folder(home, at, m[1])
        plan = plan_shrink(verdict, current, name, new_size, desired, limits, read_registry(), tools, folder)
    except (LayoutError, PlanError, FormatError) as e:
        raise Refused(str(e)) from None
    return _built(plan, now, width)


def build_grow(slice_arg: str, size_text: str, home: str, width: int, at: datetime) -> Built:
    """Grow SLICE to SIZE_TEXT into the free space right after it (T-074)."""
    name = slice_arg.removeprefix("/dev/")
    m = re.fullmatch(r"(disk[0-9]+)s[0-9]+", name)
    if m is None:
        raise Refused(f"{slice_arg}: expected a partition, diskNsM")
    try:
        new_size = parse_size(size_text)
    except LayoutError as e:
        raise Refused(f"new size: {e}") from None
    verdict, now, current = _mapped(m[1], width)
    part = next((p for p in current.partitions if p.bsd_name == name), None) if isinstance(current, Disk) else None
    if part is None:
        raise Refused(f"{name} is not a partition of {m[1]}")
    limits = read_limits(name, apfs=part.apfs is not None)
    try:
        plan = plan_grow(verdict, current, name, new_size, limits, backup_folder(home, at, m[1]))
    except (LayoutError, PlanError) as e:
        raise Refused(str(e)) from None
    return _built(plan, now, width)


@dataclass(frozen=True)
class Resizable:
    """A partition build_shrink and build_grow would take, before a size is named (T-069, T-074)."""

    slice: str
    disk: str
    apfs: bool  # an APFS physical store: apfs resizeContainer; else HFS+, resizeVolume
    current: int  # bytes, as the map has it
    minimum: int  # bytes, diskutil's resize limits: the least build_shrink accepts
    maximum: int  # bytes, the most build_grow accepts: the free space right after it (grow_limit), or the size now


def resize_limits(slice_arg: str, width: int) -> Resizable:
    """What build_shrink and build_grow check before the size, with the same probes and the same refusals:
    the web UI asks it before it offers a size. Raises Refused, ProbeError or OSError."""
    name = slice_arg.removeprefix("/dev/")
    m = re.fullmatch(r"(disk[0-9]+)s[0-9]+", name)
    if m is None:
        raise Refused(f"{slice_arg}: expected a partition, diskNsM")
    verdict, _, current = _mapped(m[1], width)
    part = next((p for p in current.partitions if p.bsd_name == name), None) if isinstance(current, Disk) else None
    if part is None:
        raise Refused(f"{name} is not a partition of {m[1]}")
    try:
        disk, part, limits, kind = resizable(verdict, current, name, read_limits(name, apfs=part.apfs is not None))
        top = grow_limit(disk, part, kind, limits)
    except PlanError as e:
        raise Refused(str(e)) from None
    return Resizable(name, m[1], kind == RESIZE_APFS, limits.current, limits.minimum, top)


def build_mount(slice_arg: str, mount: bool, width: int) -> Built:
    """Mount (else unmount) partition SLICE_ARG (T-073, K16)."""
    name = slice_arg.removeprefix("/dev/")
    m = re.fullmatch(r"(disk[0-9]+)s[0-9]+", name)
    if m is None:
        raise Refused(f"{slice_arg}: expected a partition, diskNsM")
    verdict, now, current = _mapped(m[1], width)
    try:
        plan = plan_mount(verdict, current, name, mount)
    except PlanError as e:
        raise Refused(str(e)) from None
    return _built(plan, now, width)


def build_backup(disk_arg: str, home: str, width: int, at: datetime) -> Built:
    name = disk_arg.removeprefix("/dev/")
    verdict, now, current = _mapped(name, width)
    try:
        plan = plan_backup(verdict, current, backup_folder(home, at, name))
    except PlanError as e:
        raise Refused(str(e)) from None
    return _built(plan, now, width)


def build_restore(source_arg: str, disk_arg: str, home: str, width: int, at: datetime) -> Built:
    """Put the map backed up in folder SOURCE_ARG back on DISK_ARG."""
    name = disk_arg.removeprefix("/dev/")
    source = os.path.abspath(source_arg)  # the preview's commands work from any directory
    try:
        dump = read_backup(source)
    except BadBackup as e:
        raise Refused(f"{source}: not a map backup — {e}") from None
    verdict, now, current = _mapped(name, width)
    io = parse_disk(read_iomedia(), name)  # a ProbeError goes to the caller, as from _mapped
    try:
        plan = plan_restore(verdict, current, io, dump, source, backup_folder(home, at, name))
    except PlanError as e:
        raise Refused(str(e)) from None
    return _built(plan, now, width)


def _mapped(name: str, width: int) -> tuple[AvailableDisk, DiskMap, Disk | Unpartitioned]:
    """The guard's verdict on whole disk NAME and its map, from one SystemView (D19)."""
    view = system.probe()
    verdict = assess(view).get(name)
    if verdict is None:
        raise Refused(f"{name}: not a whole disk in the system now")
    if not isinstance(verdict, AvailableDisk):
        lines = "\n".join(_access(access_of(verdict), width))
        raise Refused(f"{name} is protected; diskmap plans nothing for it:\n{lines}")
    now = build_map(view, [name], False, read_layout)
    current = now.disks[0]
    if isinstance(current, Failed):
        raise Refused(f"{name}: cannot map it — {current.reason}")
    return verdict, now, current
