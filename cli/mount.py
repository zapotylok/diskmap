"""`diskmap mount|unmount <diskNsM>` (T-073, K16): the one-step plan, run at once.

No confirmation: the partition map does not change, nothing is destroyed, and the step
is undone by the opposite command — Finder mounts on a click. Still a plan: the guard and
the step's checks run as for any other, through core.executor.run, journaled. As the user,
never as root: a volume root mounts is root's (the web UI starts this command, K14).
"""

from __future__ import annotations

import shlex
from collections.abc import Callable

from cli.plan import Built
from core.executor.journal import Journal, JournalError, journal_path
from core.executor.run import Refused, apply
from core.probe.environment import read_environment
from core.probe.errors import ProbeError


def run(built: Built, home: str, search_path: str, out: Callable[[str], None] = print) -> int:
    """Apply BUILT; 0 done, 1 not (refused, the step failed), 130 interrupted."""
    plan = built.plan
    (step,) = plan.steps
    out(shlex.join(step.command))
    try:
        results = apply(plan, Journal(journal_path(home), None), read_environment(search_path, home), have_root=False)
    except KeyboardInterrupt:
        out("stopped")
        return 130
    except (JournalError, ProbeError, Refused) as e:
        out(f"{plan.strategy}: {e}")
        return 1
    r = results[-1]
    for text in (r.stdout, r.stderr):
        if text.strip():
            out(text.strip())
    if r.error is not None:
        out(f"{plan.strategy}: {r.error}")
        return 1
    return 0
