"""`diskmap doctor --trial` (T-026, D34): make and mount every format on an image of its own.

For can_create and can_mount the only honest answer is trying (D16). The trial:
1. reads the setup that decides mounting (core.probe.trial) — again at the end; if it
   changed in between, nothing is kept;
2. makes a sparse image in a folder of its own and attaches it unmounted
   (core.executor.image — the two commands outside a plan, K13);
3. per format, a plan (core.planner.trial) run by the executor, journaled like any run,
   with the trial's own guard before every step (core.safety.image): this image only;
4. after each plan reads `mount`: what mounted the volume, read-write or read-only — or,
   when nothing did, why;
5. detaches the image by a plan as well, removes its folder, keeps the result in
   ~/.diskmap/trial.json.

Runs as the user, never under sudo: the image's nodes are then the user's (K7) and no step
needs root. Ctrl-C stops between steps as in `apply`; the image is detached all the same.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from datetime import datetime

from core.executor.image import create_and_attach
from core.executor.journal import Journal, JournalError, journal_path
from core.executor.run import Refused, StepResult, apply
from core.model.doctor import NO, RO, RW, Capability, Mounting, Trial
from core.model.filesystems import Registry
from core.planner.common import PlanError, Tools
from core.planner.plan import ExecutionPlan, Step
from core.planner.trial import plan_detach, plan_trial, trial_formats
from core.probe.doctor import mke2fs_path, working_tool
from core.probe.environment import read_environment
from core.probe.errors import ProbeError
from core.probe.filesystems import read_registry
from core.probe.images import read_image_devices
from core.probe.layout import read_layout
from core.probe.mounts import read_mounts
from core.probe.trial import changes, encode, read_setup, trial_path
from core.probe.volume import parse_info, read_info
from core.safety.guard import NotAvailable, require_available
from core.safety.image import NotTrialImage, require_trial_image

IMAGE = "trial.sparseimage"


class TrialError(Exception):
    """The trial did not run, or its result is not kept; the message says why."""


def trial_folder(home: str, at: datetime) -> str:
    return os.path.join(home, ".diskmap", "trial", f"{at:%Y%m%d-%H%M%S}")


def run_trial(
    home: str,
    search_path: str,
    folder: str,
    at: datetime,
    *,
    only: tuple[str, ...] = (),
    gate: Callable[[Step], None] = lambda step: None,
    finished: Callable[[StepResult], None] = lambda result: None,
    out: Callable[[str], None] = print,
) -> Trial:
    """Try every format (ONLY: just these) on an image made in FOLDER, a folder that must
    not exist yet; keep and return the result. GATE: core.executor.run's, after the trial's
    own guard — the rig guard in tests. FINISHED is told about every step."""
    if os.geteuid() == 0:
        raise TrialError("run it as yourself, not through sudo: the image is then yours and no step needs root")
    setup = read_setup(search_path, home)
    registry = read_registry()
    tools = Tools(working_tool("mkntfs", search_path, home), mke2fs_path(search_path, home))
    formats = trial_formats(registry, tools)
    if only:
        folded = {o.casefold() for o in only}
        unknown = folded - {f.casefold() for f, _ in formats}
        if unknown:
            raise TrialError(f"not a format a layout can name here: {', '.join(sorted(unknown))}")
        formats = [(f, why) for f, why in formats if f.casefold() in folded]
    os.makedirs(folder)
    image = os.path.join(folder, IMAGE)
    disk = create_and_attach(image)
    out(f"trial image {image} attached as {disk}")
    journal = Journal(journal_path(home))
    env = read_environment(search_path, home)

    def guarded(step: Step) -> None:
        require_trial_image(image, disk, step)
        gate(step)

    def run(plan: ExecutionPlan) -> list[StepResult]:
        return apply(plan, journal, env, have_root=False, gate=guarded, finished=finished)

    caps: list[Capability] = []
    left: list[str] = []  # why the image is still attached
    try:
        for n, (fmt, why) in enumerate(formats, 1):
            if why is not None:
                caps.append(Capability(fmt, None, why, None))
            else:
                caps.append(_one(disk, image, fmt, registry, tools, os.path.join(folder, f"{n:02d}"), run))
            out(f"[{n}/{len(formats)}] {describe(caps[-1])}")
    finally:
        try:
            results = run(plan_detach(require_available(disk), read_layout(disk)))
            if results[-1].error is not None:
                left.append(results[-1].error)
        except (ProbeError, NotAvailable, NotTrialImage, PlanError, JournalError, Refused, OSError) as e:
            left.append(f"{type(e).__name__}: {e}")
        if left:
            out(f"could not detach the trial image: {left[0]} — hdiutil detach {disk}, then remove {folder}")
    if left:
        raise TrialError(f"the trial image is still attached as {disk}; nothing is kept")
    shutil.rmtree(folder)
    now = read_setup(search_path, home)
    if now != setup:
        raise TrialError("the setup changed during the trial, nothing is kept: " + "; ".join(changes(setup, now)))
    trial = Trial(at.isoformat(timespec="seconds"), setup, tuple(caps))
    _keep(trial, home)
    return trial


def _one(
    disk: str,
    image: str,
    fmt: str,
    registry: Registry,
    tools: Tools,
    folder: str,
    run: Callable[[ExecutionPlan], list[StepResult]],
) -> Capability:
    tp = plan_trial(require_available(disk), read_layout(disk), fmt, registry, tools, folder)
    results = run(tp.plan)
    last = results[-1]
    first_tail = len(tp.plan.steps) - tp.tail + 1
    if last.error is not None and last.n < first_tail:
        return Capability(fmt, False, _failed(last), None)
    made = tp.plan.partitions[0].slice
    v = parse_info(read_info(made), made).volume
    named = (
        f"diskutil names it {v.filesystem_name or v.filesystem}" if v.filesystem else "diskutil names no file system"
    )
    return Capability(fmt, True, named, mounting(image, last))


def mounting(image: str, last: StepResult) -> Mounting:
    """How the trial volume is mounted now, from `mount`; LAST: the plan's last step that ran."""
    wholes = {d for d in read_image_devices(image) if re.fullmatch(r"disk[0-9]+", d)}
    found = [m for m in read_mounts() if re.sub(r"s[0-9]+$", "", m.device) in wholes]
    if not found:
        return Mounting(NO, None, _failed(last) if last.error is not None else "mountDisk mounted nothing")
    m = found[0]
    return Mounting(RW if m.writable else RO, m.by, f"{m.device} at {m.point}")


def describe(c: Capability) -> str:
    if c.created is None:
        return f"{c.format}: not tried — {c.detail}"
    if not c.created:
        return f"{c.format}: not created — {c.detail}"
    if c.mount is None:
        return f"{c.format}: created; mount not tried"
    if c.mount.mode == NO:
        return f"{c.format}: created; mounts: no — {c.mount.detail}"
    return f"{c.format}: created; mounts {c.mount.mode} by {c.mount.by}"


def _failed(r: StepResult) -> str:
    said = next((ln.strip() for ln in reversed((r.stderr or r.stdout).splitlines()) if ln.strip()), "")
    return f"step {r.n} ({r.step.command[0].rsplit('/', 1)[-1]} {r.step.command[1]}) {r.error}" + (
        f": {said}" if said else ""
    )


def _keep(trial: Trial, home: str) -> None:
    path = trial_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(encode(trial), f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
