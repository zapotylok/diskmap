"""Text rendering of the doctor report: one table, four columns (T-024 AC, D5), and the
last trial mount — create and mount per format, side by side but apart (T-026, D34). And
the same report as JSON for the window's Formats dialog (T-081, K22)."""

from __future__ import annotations

import json
import textwrap
from collections.abc import Collection
from itertools import zip_longest
from typing import Any

from core.model.doctor import ABSENT, FAILS, LISTED, NO, Capability, Creator, Driver, MountFact, Report, Row, TrialNow
from core.planner import formats as F
from core.probe.doctor import TOOLS

HEADERS = ("FORMAT", "CREATED BY", "DRIVER", "MOUNTED NOW")
WIDTHS = (7, 46, 46, 30)
NOT_CHECKED = "not checked: nothing mounted now (see TRIAL MOUNT)"
TRIAL_WIDTHS = (30, 7, 92)
NO_TRIAL = (
    "No trial mount yet. diskmap doctor --trial makes and mounts every format on a disk image of its own "
    "(a few minutes; nothing else is touched) and keeps the result for plan previews."
)


JSON_FORMAT = 1
# The families macOS creates and mounts itself; the dialog sums them up in one line.
BUILTIN = ("apfs", "hfs", "exfat", "msdos")
TITLES = {
    "apfs": "APFS",
    "hfs": "Mac OS Extended (HFS+)",
    "exfat": "ExFAT",
    "msdos": "MS-DOS (FAT)",
    "ntfs": "NTFS",
    "ext": "ext4",
}
TOOL_FORMATS = {"ntfs": F.NTFS, "ext": F.EXT4}  # the format a layout names for a family's own tool
# What macOS does with a volume of a third-party family by itself, whatever is installed.
BY_ITSELF = {"ntfs": "macOS itself mounts NTFS read-only.", "ext": "macOS itself does not mount ext4."}
HOW_TO = "https://github.com/zapotylok/diskmap#ntfs-and-ext4"


def to_json(report: Report, trial: TrialNow, installable: Collection[str]) -> str:
    """REPORT and the last TRIAL, one entry per family, for the Formats dialog. A creator
    that is absent names its package in `action` when INSTALLABLE holds it: the window
    offers it to Terminal (ui/install.py); every other way to get one is a pointer only."""
    first = sorted((r for r in report.rows if r.family in BUILTIN), key=lambda r: BUILTIN.index(r.family))
    rest = [r for r in report.rows if r.family not in BUILTIN]
    families = [_family(row, trial, installable) for row in (*first, *rest)]
    t = trial.trial
    doc = {
        "format": JSON_FORMAT,
        "families": families,
        "unattributed": [_mount_json(m) for m in report.unattributed],
        "trial": {"at": t.at if t is not None else None, "holds": trial.holds, "changed": list(trial.changed)},
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _family(row: Row, trial: TrialNow, installable: Collection[str]) -> dict[str, Any]:
    spec = TOOLS.get(row.family)
    package = spec.package if spec is not None else None
    creators = [
        {
            "how": c.how,
            "state": c.state,
            "detail": c.detail,
            "install": c.install,
            "action": package if c.state == ABSENT and package in installable else None,
        }
        for c in row.creators
    ]
    formats = [c.how.removeprefix("diskutil ") for c in row.creators if c.how.startswith("diskutil ")]
    if row.family in TOOL_FORMATS:
        formats.append(TOOL_FORMATS[row.family])
    tried = []
    if trial.holds and trial.trial is not None:
        for name in formats:
            c = trial.trial.find(name)
            if c is not None:
                tried.append(
                    {
                        "format": c.format,
                        "created": c.created,
                        "mount": c.mount.mode if c.mount else None,
                        "by": c.mount.by if c.mount else None,
                    }
                )
    return {
        "family": row.family,
        "title": TITLES.get(row.family, row.family),
        "builtin": row.family in BUILTIN,
        "creators": creators,
        "drivers": [
            {"name": d.name, "loaded": d.loaded, "detail": d.detail, "install": d.install} for d in row.drivers
        ],
        "mounts": [_mount_json(m) for m in row.mounts],
        "trial": tried,
        "by_itself": BY_ITSELF.get(row.family),
        "how_to": HOW_TO if row.family in BY_ITSELF else None,
    }


def _mount_json(m: MountFact) -> dict[str, Any]:
    return {"driver": m.driver, "writable": m.writable, "count": m.count}


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
