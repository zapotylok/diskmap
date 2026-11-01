"""`diskmap journal`: what earlier runs did (T-013). Read-only.

One block per run, oldest first: when, which plan on which disk, the outcome, the steps
done, why it stopped, and the map backups it made — with the command that puts one back
(K11), since after a partial run that is the first thing to look for. A run still open
is either running now or was killed; the next run that starts closes it in the journal.
"""

from __future__ import annotations

from core.executor.journal import APPLIED, RunState


def render(runs: list[RunState], torn: bool, path: str) -> str:
    if not runs:
        lines = [f"No runs in {path}."]
    else:
        lines = [f"{len(runs)} run(s) in {path}, oldest first:", ""]
        for r in runs:
            lines += render_run(r)
    if torn:
        lines.append(f"The last line of {path} is cut off; the next run will refuse to start until it is moved aside.")
    return "\n".join(lines).rstrip()


def render_run(r: RunState) -> list[str]:
    head = f"{r.run}  {r.strategy} {r.disk}  {r.outcome}"
    if not r.closed:
        head += " (not closed: running now, or its process was killed)"
    out = [head, "    " + " · ".join(details(r))]
    if r.outcome != APPLIED or not r.closed:
        for b in r.backups:
            out.append(f"    map backup: diskmap restore-map {b} {r.disk}")
    if r.closed_by:
        out.append(f"    closed by run {r.closed_by}")
    out.append("")
    return out


def details(r: RunState) -> list[str]:
    """What R did, as `diskmap journal` says it: the steps done, the one failed, the one in
    flight, why it stopped (diskmap ui shows the same, T-068)."""
    out = [f"done: {_ranges(r.done)} of {r.steps}" if r.done else f"no step done of {r.steps}"]
    if r.failed is not None:
        out.append(f"failed: step {r.failed}")
    if r.in_flight is not None:
        out.append(f"in flight: step {r.in_flight} — its result is not known")
    if r.reason:
        out.append(r.reason)
    return out


def _ranges(ns: tuple[int, ...]) -> str:
    """1, 2, 3, 5 → "steps 1-3, 5"."""
    parts: list[str] = []
    start = prev = None
    for n in ns:
        if start is not None and prev is not None and n == prev + 1:
            prev = n
            continue
        if start is not None:
            parts.append(f"{start}-{prev}" if prev != start else f"{start}")
        start = prev = n
    if start is not None:
        parts.append(f"{start}-{prev}" if prev != start else f"{start}")
    return ("step " if len(ns) == 1 else "steps ") + ", ".join(parts)
