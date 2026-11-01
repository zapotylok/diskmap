"""`diskmap apply …` (T-050): the plan `diskmap plan` shows, applied.

The plan is built here, in this process, from the same words as `diskmap plan` and by
the same code (cli/plan.py), and its preview is printed: what is confirmed is what runs,
with no plan file in between to go stale or be edited. Then, before anything runs:
- a plan with a step that needs root this process lacks is refused: `diskmap apply` runs
  under sudo as a whole, and the steps that do not need root run as the user who called
  it, AS_USER; the journal is given to them too (T-051, D32);
- the user types the disk's name; anything else, or the end of input, cancels. No flag
  answers for them;
- a plan that destroys volumes asks once more (T-062): the label of the volume holding the
  most data, exactly as it is, or its device when no lost volume has a label. Not "OK":
  a name only the volume about to go has.
The steps run through core.executor.run, journaled in ~/.diskmap/journal.jsonl, one line
of progress per step. Ctrl-C stops the plan once the step in flight has finished — a step
is never killed half-way (T-052); it is said at once, so nobody presses it again in
alarm. The run is summed up as `diskmap journal` shows it; when it did not finish, with
the disk's map as it is now and the map backup to go back to. Nothing is rolled back.

The terminal can go away mid-run: its window closed, SIGHUP (T-066). The plan stops as on
Ctrl-C, and nothing more is written — there is nowhere to write, and a write to a terminal
gone fails or, behind sudo's own terminal, may never return. The journal says where it
stopped. A write that fails means the terminal is gone too. From the confirmation to the end a
SIGHUP never kills the process: the exit status is what the script handing `apply` to
Terminal leaves for the web UI (ui/terminal.py). Before it nothing has run, and SIGHUP
ends the process as it always does.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from cli import show
from cli.journal import render_run
from cli.plan import Built
from core.executor.journal import Journal, JournalError, journal_path, read_journal
from core.executor.privilege import Invoker
from core.executor.run import Refused, StepResult, apply, require_root
from core.planner.plan import ExecutionPlan, Loss, Step
from core.probe.environment import read_environment
from core.probe.errors import ProbeError
from core.probe.show import read_map

CANCELLED = "cancelled; nothing was run"
# Said before the first question: the backup a plan makes is of the map (K11), never of the files (K18).
BACKUP_FIRST = "Back up the files on this drive first: diskmap saves only the partition map, not the files on it."


def run(
    built: Built,
    home: str,
    search_path: str,
    ask: Callable[[str], str],
    have_root: bool,
    as_user: Invoker | None = None,
    out: Callable[[str], None] = print,
    gate: Callable[[Step], None] = lambda step: None,
    width: int = 80,
) -> int:
    """Show BUILT, confirm it through ASK, apply it. The exit status: 0 applied, 1 not
    (refused, cancelled, a step failed), 130 interrupted. GATE: core.executor.run's —
    nothing in the product, the rig guard in tests."""
    plan = built.plan
    gone: list[str] = []  # why the terminal is taken as gone; nothing is written once it is

    def say(text: str) -> None:
        if gone:
            return
        try:
            out(text)
        except OSError as e:
            gone.append(f"write: {e}")

    say(built.preview)
    say("")
    try:
        require_root(plan, have_root)
    except Refused as e:
        say(f"apply: {e}")
        return 1
    say(BACKUP_FIRST)
    if built.untested:
        say(f"Not tested here: {built.untested}")  # last, next to the question (K19)
    if ask(f"Type {plan.disk} to apply this plan, anything else cancels: ").strip() != plan.disk:
        say(CANCELLED)
        return 1
    victim = confirming_loss(plan)
    if victim is not None:
        lost = sum(len(s.destroys) for s in plan.steps)
        if victim.label is not None:
            prompt = (
                f'It destroys {lost} volume(s) — see DATA LOST. Type the label of {victim.device}, "{victim.label}": '
            )
            expected = victim.label
        else:
            prompt = f"It destroys {lost} volume(s) — see DATA LOST. Type {victim.device}, which has no label: "
            expected = victim.device
        if ask(prompt).rstrip("\n") != expected:
            say(CANCELLED)
            return 1
    path = journal_path(home)
    total = len(plan.steps)

    def started(n: int, step: Step) -> None:
        say(f"[{n}/{total}] {step.intent}")

    def finished(r: StepResult) -> None:
        if r.error is None:
            say("      done")
            return
        say(f"      FAILED: {r.error}")
        for name, text in (("stdout", r.stdout), ("stderr", r.stderr)):
            if text.strip():
                say(f"      {name}:\n" + "\n".join(f"        {line}" for line in text.strip().splitlines()[-20:]))

    def signalled(name: str, n: int | None) -> None:
        if name == "SIGHUP":
            gone.append(name)
            return
        what = f"step {n} is not interrupted: the plan stops once it has finished" if n else "the plan stops now"
        say(f"      {name}: {what}")

    with _hung_up(lambda: gone.append("SIGHUP")):
        try:
            env = read_environment(search_path, home)
            results = apply(
                plan,
                Journal(path, None if as_user is None else (as_user.uid, as_user.gid)),
                env,
                have_root=have_root,
                as_user=as_user,
                gate=gate,
                started=started,
                finished=finished,
                signalled=signalled,
            )
        except KeyboardInterrupt:
            say("\nstopped; nothing is rolled back")
            _summary(path, plan.disk, width, say, gone)
            return 130
        except (JournalError, ProbeError, Refused) as e:
            say(f"apply: {e}")
            return 1
        applied = len(results) == total and results[-1].error is None
        _summary(path, None if applied else plan.disk, width, say, gone)
        return 0 if applied else 1


@contextmanager
def _hung_up(then: Callable[[], None]) -> Iterator[None]:
    """While inside, SIGHUP only calls THEN; the plan's own handler takes it while the plan
    runs (core.executor.run). The previous handler is back on leaving."""
    previous = signal.signal(signal.SIGHUP, lambda signum, frame: then())
    try:
        yield
    finally:
        signal.signal(signal.SIGHUP, previous)


def _summary(path: str, disk: str | None, width: int, out: Callable[[str], None], gone: list[str]) -> None:
    """The run just made, as `diskmap journal` shows it; DISK: show its map as it is now.
    Nothing once the terminal is GONE: the map is not even read."""
    if gone:
        return
    if disk is not None:
        out("")
        out(f"{disk} now:")
        try:
            out(show.render(read_map([disk]), width))
        except ProbeError as e:
            out(f"cannot read the map of {disk}: {e}")
    try:
        runs, _ = read_journal(path)
    except JournalError as e:
        out(f"journal: {e}")
        return
    if runs:
        out("")
        out("\n".join(render_run(runs[-1])).rstrip())
    out(f"journal: {path}")


def confirming_loss(plan: ExecutionPlan) -> Loss | None:
    """The volume whose name confirms PLAN's destruction: of the labelled ones the one with
    the most data in use (unknown counts as none; the first on a tie), else the first lost.
    None: the plan destroys nothing."""
    losses = [x for s in plan.steps for x in s.destroys]
    labelled = [x for x in losses if x.label]
    if labelled:
        return max(labelled, key=lambda x: x.used or 0)  # max keeps the first of equals
    return losses[0] if losses else None
