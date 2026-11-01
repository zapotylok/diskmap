"""Applying an ExecutionPlan (T-050; rules.md §S3): its steps in order, and nothing else.

For each step:
1. the guard, asked now, still issues the plan's disk (§S8) — before every step, whether
   the step declares DiskAvailable or not; the AvailableDisk it returns is the one the
   command runs on;
2. the step's preflight, every assertion (core.executor.checks);
3. GATE(step): the caller's last word before the command — the rig tests put the rig
   guard of every device the argv names here (§S1); the product puts nothing;
4. the command (core.executor.command: argv, the stdin contract, no terminal);
5. the postflight — checked even when the command failed once it had started:
   partitionDisk may exit 1 having changed the map (D21). What it finds goes into the
   step's error; the step still fails.

The first step that fails stops the plan. Nothing is rolled back: the map backup the
plan made and the journal say where the disk stands (T-052). Every step is journaled
(T-013, K12); a run is refused while another holds the journal.

Ctrl-C (SIGINT), SIGTERM and SIGHUP stop the plan between steps, never inside one (T-052,
T-066): a step is not killed half-way — partitionDisk cut short leaves a map nobody
predicted. SIGHUP is the terminal gone, its window closed: the caller can no longer print,
but the plan stops the same way. While the plan runs they only set a flag (and tell the
caller); the step in flight finishes, its postflight is checked and journaled, and the
plan stops before the next one with Stopped, a KeyboardInterrupt. A signal during a
step's preflight, before its command, stops the plan before that command (T-068). The
step's process is in a session of its own (core.executor.command), and so is every probe
(core.probe.command): the terminal and sudo send Ctrl-C and the hangup to the whole
process group, and they reach diskmap alone — a check's `diskutil` killed by them would
fail the step instead of stopping the plan. A second signal changes nothing. Only the
timeout still kills a command.

Root (T-051, D32): a plan with a step that needs root is refused before anything runs
unless the process has it — the shortfall shows before the first step, not half-way.
`diskmap apply` gets root by running under sudo as a whole; AS_USER, the user who called
sudo, runs every step that does not need root (core.executor.privilege).
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from core.executor.checks import NotMet, check, settled
from core.executor.command import StepFailed, run_step_command
from core.executor.journal import Journal
from core.executor.privilege import Invoker
from core.planner.plan import ExecutionPlan, Step
from core.probe.errors import ProbeError
from core.probe.snapshot import take
from core.safety.guard import NotAvailable, require_available

# Backstop for a command that hangs for a reason other than a question (core.executor.command).
TIMEOUT = 3600.0


class Refused(Exception):
    """The plan was not started; nothing ran and nothing was journaled."""


class Stopped(KeyboardInterrupt):
    """A signal asked the plan to stop; it stopped between steps. The message says which
    signal came during which step."""


SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@dataclass(frozen=True)
class StepResult:
    n: int
    step: Step
    rc: int | None  # None: no exit code — refused before the command, or killed at the timeout
    stdout: str
    stderr: str
    asked: tuple[tuple[str, str], ...]  # questions the command printed, and the answers (T-054)
    error: str | None  # what failed the step; None: it did what it declared


def require_root(plan: ExecutionPlan, have_root: bool) -> None:
    """Refuse PLAN if a step needs root and HAVE_ROOT is false."""
    need = [f"  {n}. {s.intent}" for n, s in enumerate(plan.steps, 1) if s.needs_root]
    if need and not have_root:
        raise Refused(
            "these steps need root — run the same command through sudo; sudo asks for the password, "
            "and the steps that do not need root still run as you:\n" + "\n".join(need)
        )


def apply(
    plan: ExecutionPlan,
    journal: Journal,
    environment: dict[str, Any],
    *,
    have_root: bool,
    as_user: Invoker | None = None,
    gate: Callable[[Step], None] = lambda step: None,
    started: Callable[[int, Step], None] = lambda n, step: None,
    finished: Callable[[StepResult], None] = lambda result: None,
    signalled: Callable[[str, int | None], None] = lambda name, n: None,
    timeout: float = TIMEOUT,
) -> list[StepResult]:
    """Run PLAN's steps, journaled; the results of the steps that ran, the last one failed
    if the plan stopped. STARTED and FINISHED are told about each step, for progress, and
    SIGNALLED about a stop signal (its name, and the step whose command is running; None
    between steps or before a command) when it comes. An exception from any of them or
    from GATE stops the plan and is journaled as the reason; a signal raises Stopped before
    the next command. AS_USER runs the steps that
    do not need root (None: this process runs them all). Call from the main thread: only
    it can take the signals."""
    require_root(plan, have_root)
    results: list[StepResult] = []
    current: int | None = None  # the step in flight
    running = False  # its command has started
    stop: list[str] = []  # why to stop before the next command, once a signal came

    def on_signal(name: str) -> None:
        if stop:
            return  # a second signal changes nothing
        if current is None:
            stop.append(f"{name} came between steps; the plan stopped before the next step")
        elif not running:
            stop.append(f"{name} came during step {current}, before its command; the plan stopped before it")
        else:
            stop.append(f"{name} came during step {current}; the plan stopped before the next step")
        signalled(name, current if running else None)

    def begin(n: int) -> None:
        nonlocal running
        if stop:
            raise Stopped(stop[0])
        running = True
        record.step_started(n)

    def snapshot() -> Any:
        return settled(lambda: take(plan.disk))

    with _deferred(on_signal), journal.run(plan, environment, before=snapshot, after=snapshot) as record:
        for n, step in enumerate(plan.steps, 1):
            if stop:
                raise Stopped(stop[0])
            current, running = n, False
            started(n, step)
            r = _step(n, step, plan.disk, gate, timeout, as_user, lambda n=n: begin(n))
            record.step_finished(n, r.rc, r.stdout, r.stderr, r.asked, r.error)
            results.append(r)
            current, running = None, False
            finished(r)
            if r.error is not None:
                break
    return results


@contextmanager
def _deferred(on_signal: Callable[[str], None]) -> Iterator[None]:
    """While inside, the SIGNALS only call ON_SIGNAL with the signal's name; the previous
    handlers are back on leaving."""

    def handler(signum: int, frame: object) -> None:
        on_signal(signal.Signals(signum).name)

    previous = {sig: signal.signal(sig, handler) for sig in SIGNALS}
    try:
        yield
    finally:
        for sig, h in previous.items():
            signal.signal(sig, h)


def _step(
    n: int,
    step: Step,
    disk_name: str,
    gate: Callable[[Step], None],
    timeout: float,
    as_user: Invoker | None,
    begin: Callable[[], None],
) -> StepResult:
    """BEGIN journals step-started: once the preflight holds, right before the command. A
    step refused by its preflight never started — the journal's outcome counts on that.
    BEGIN raises Stopped instead when a signal came meanwhile: the command never starts."""
    try:
        disk = require_available(disk_name)
        for a in step.preflight:
            check(a)
    except (NotAvailable, NotMet, ProbeError) as e:
        return StepResult(n, step, None, "", "", (), f"preflight not met: {e}")
    gate(step)
    begin()
    try:
        ran = run_step_command(step, disk, timeout, as_user)
    except StepFailed as e:
        error = str(e) + (_after_failure(step) if e.started else "")
        return StepResult(n, step, e.rc, e.stdout, e.stderr, (), error)
    asked = tuple((a.question, a.answer) for a in ran.asked)
    if ran.returncode != 0:
        error = f"exit {ran.returncode}" + _after_failure(step)
        return StepResult(n, step, ran.returncode, ran.stdout, ran.stderr, asked, error)
    try:
        for a in step.postflight:
            check(a)
    except NotMet as e:
        return StepResult(n, step, ran.returncode, ran.stdout, ran.stderr, asked, f"postflight not met: {e}")
    return StepResult(n, step, 0, ran.stdout, ran.stderr, asked, None)


def _after_failure(step: Step) -> str:
    """What the postflight finds after a command that failed once started (D21)."""
    if not step.postflight:
        return ""
    try:
        for a in step.postflight:
            check(a)
    except NotMet as e:
        return f"; postflight not met either: {e}"
    return "; yet its postflight holds — the command did what the step declares, and still failed"
