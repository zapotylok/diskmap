"""Text rendering of the doctor report: one table, four columns (T-024 AC, D5), and the
last trial mount — create and mount per format, side by side but apart (T-026, D34)."""

from __future__ import annotations

import textwrap
from itertools import zip_longest

from core.model.doctor import ABSENT, FAILS, LISTED, NO, Capability, Creator, Driver, MountFact, Report, TrialNow

HEADERS = ("FORMAT", "CREATED BY", "DRIVER", "MOUNTED NOW")
WIDTHS = (7, 46, 46, 30)
NOT_CHECKED = "not checked: nothing mounted now (see TRIAL MOUNT)"
TRIAL_WIDTHS = (30, 7, 92)
NO_TRIAL = (
    "No trial mount yet. diskmap doctor --trial makes and mounts every format on a disk image of its own "
    "(a few minutes; nothing else is touched) and keeps the result for plan previews."
)


def render(report: Report, trial: TrialNow | None = None) -> str:
    lines = [_line(HEADERS), _line(tuple("-" * w for w in WIDTHS))]
    for row in report.rows:
        cells = (
            [row.family],
            _items(_creator(c) for c in row.creators),
            _items(_driver(d) for d in row.drivers) or ["none known"],
            _items(_mount(m) for m in row.mounts) or _wrap(NOT_CHECKED, WIDTHS[3]),
        )
        for parts in zip_longest(*cells, fillvalue=""):
            lines.append(_line(parts))
        lines.append("")
    if report.unattributed:
        lines.append(
            "Mounted by drivers doctor maps to no format: " + ", ".join(_mount(m) for m in report.unattributed)
        )
    lines.append("Formats diskutil lists are 'listed', not tried; whether a volume mounts is only known")
    lines.append("from a mounted one — doctor reads; only doctor --trial formats and mounts, on its own image.")
    if trial is not None:
        lines += ["", *render_trial(trial)]
    return "\n".join(ln.rstrip() for ln in lines)


def render_trial(now: TrialNow) -> list[str]:
    if now.trial is None:
        if now.problem:
            return _wrap(f"TRIAL MOUNT — the kept trial cannot be read: {now.problem}", 100)
        return _wrap(NO_TRIAL, 100)
    t = now.trial
    if now.changed:
        head = [f"TRIAL MOUNT — {t.at}, on another setup: it no longer says how this Mac mounts. Changed since:"]
        head += [f"  {c}" for c in now.changed]
        head.append("Run diskmap doctor --trial again.")
    else:
        head = [f"TRIAL MOUNT — {t.at}, this Mac's setup unchanged since"]
    out = [*head, _trial_line(("FORMAT", "CREATE", "MOUNT")), _trial_line(tuple("-" * w for w in TRIAL_WIDTHS))]
    for c in t.capabilities:
        create, mount = _capability(c)
        for parts in zip_longest([c.format], [create], _wrap(mount, TRIAL_WIDTHS[2]), fillvalue=""):
            out.append(_trial_line(parts))
    return out


def _capability(c: Capability) -> tuple[str, str]:
    if c.created is None:
        return "-", f"not tried: {c.detail}"
    if not c.created:
        return "no", f"not tried, not created: {c.detail}"
    if c.mount is None:
        return "yes", f"not tried ({c.detail})"
    if c.mount.mode == NO:
        return "yes", f"no — {c.mount.detail}"
    return "yes", f"{c.mount.mode} by {c.mount.by} ({c.detail})"


def _trial_line(parts) -> str:
    return "  ".join(str(p).ljust(w) for p, w in zip(parts, TRIAL_WIDTHS))


def _creator(c: Creator) -> list[str]:
    mark = {ABSENT: "✗", FAILS: "!", LISTED: "·"}.get(c.state, "✓")
    out = [f"{mark} {c.how} — {c.state}", f"  {c.detail}"]
    if c.install:
        out.append(f"  → {c.install}")
    return out


def _driver(d: Driver) -> list[str]:
    mark = {True: "✓", False: "✗", None: "?"}[d.loaded]
    out = [f"{mark} {d.name}", f"  {d.detail}"]
    if d.install:
        out.append(f"  → {d.install}")
    return out


def _mount(m: MountFact) -> str:
    return f"{m.driver} {'rw' if m.writable else 'ro'} ×{m.count}"


def _items(items) -> list[str]:
    out: list[str] = []
    for item in items:
        for text in [item] if isinstance(item, str) else item:
            out.extend(_wrap(text, 46))
    return out


def _wrap(text: str, width: int) -> list[str]:
    indent = text[: len(text) - len(text.lstrip())]
    return textwrap.wrap(text, width, subsequent_indent=indent + "  ", break_on_hyphens=False) or [""]


def _line(parts) -> str:
    return "  ".join(str(p).ljust(w) for p, w in zip(parts, WIDTHS))
