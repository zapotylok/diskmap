"""From a plan in the web UI to `sudo diskmap apply` in a new Terminal window (T-062, K14).

The UI applies nothing. It writes what `diskmap apply` reads — the layout file — and a
`.command` script that runs `diskmap apply` under sudo, and asks Terminal to open the
script. There sudo asks for the password, and `apply` builds the plan again, shows it
and asks for the disk's name and, when volumes are destroyed, a label (cli/apply.py):
what is confirmed is what that process runs, whatever the browser showed.

The script is the one shell text diskmap writes itself (K14, a reading of §S4). Nothing in
it comes from the user: the interpreter and the project folder, the kind of plan from a
fixed list, the disk's name checked as diskN (diskNsM for shrink and grow), and the path of the
layout file written here. Labels, formats and sizes stay in the layout file — but for the new
size of a shrink or a grow, which `apply` takes only as --to: that word is written here from the
size parsed to whole bytes, digits and a unit, not as typed (K15, K17). Every word is shlex-quoted.

Everything goes into a new folder under ~/.diskmap/ui, named by the time and the disk,
private to the user; it stays there as a record of what was handed to the terminal.

When `apply` ends, the script says how, in colour, and writes its exit status into the
same folder (T-065): the page, waiting on the folder's name, learns from it that the
window is done and reads the disks again. The script runs as the user — only `apply`
runs under sudo — so the file is the user's, like the folder.

What `apply` did, the page learns from the journal (T-068): the window it ran in may be
closed by then, and the exit status alone does not say whether a step ran.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.executor.journal import JournalError, RunState, journal_path, read_journal
from core.model.desired import FIXED, LayoutError, parse_size
from core.planner import addpart, backup, grow, repartition, shrink

KINDS = ("repartition", "add", "shrink", "grow", "backup-map")
RESIZE = ("shrink", "grow")  # take a partition and --to
WHOLE = re.compile(r"disk[0-9]+")
SLICE = re.compile(r"(?P<whole>disk[0-9]+)s[0-9]+")
ROOT = str(Path(__file__).resolve().parent.parent)  # where `python -m cli` finds diskmap
SUDO = "/usr/bin/sudo"
OPEN = "/usr/bin/open"
LAYOUT = "layout.json"
SCRIPT = "apply.command"
DONE = "exit-status"  # the exit status of `diskmap apply`, written when the script ends
# The plan's strategy in the journal, by kind.
STRATEGIES = {
    "repartition": repartition.STRATEGY,
    "add": addpart.STRATEGY,
    "shrink": shrink.STRATEGY,
    "grow": grow.STRATEGY,
    "backup-map": backup.BACKUP,
}
# Units of size_word, largest first: binary for a size given in binary units, else decimal.
BINARY = (("Ei", 2**60), ("Pi", 2**50), ("Ti", 2**40), ("Gi", 2**30), ("Mi", 2**20), ("Ki", 2**10))
DECIMAL = (("E", 10**18), ("P", 10**15), ("T", 10**12), ("G", 10**9), ("M", 10**6), ("K", 10**3))
# A folder's name as _new_folder makes it, by the whole disk; the page names a handoff by it.
HANDOFF_ID = re.compile(
    r"(?P<at>[0-9]{8}-[0-9]{6})-(?P<disk>disk[0-9]+)-(?P<kind>" + "|".join(KINDS) + r")(?:-[0-9]{1,2})?"
)
STATUS = re.compile(r"[0-9]{1,3}\n")


class HandoffError(Exception):
    """Nothing was handed to the terminal; the message says why."""


@dataclass(frozen=True)
class Handoff:
    id: str  # the folder's name: what the page asks about (result)
    folder: str
    layout: str | None  # the layout file; None for a plan without one (backup-map)
    script: str
    argv: tuple[str, ...]  # what the script runs, in ROOT
    command_line: str  # the same, as a person types it: cd …  && sudo …


def prepare(
    kind: str,
    disk: str,
    layout: Any | None,
    home: str,
    at: datetime,
    python: str = sys.executable,
    root: str = ROOT,
    *,
    to: str | None = None,
    after: str | None = None,
) -> Handoff:
    """Write the layout and the script for `diskmap apply KIND DISK` into a new folder. For
    shrink DISK is the partition, TO its new size, and the layout (the partitions to add after
    it) may be None; grow is the same, with no layout (T-074). AFTER: add only, the partition
    before the gap the new ones go into (--after, T-076)."""
    if kind not in KINDS:
        raise HandoffError(f"kind: expected one of {', '.join(KINDS)}")
    m = (SLICE if kind in RESIZE else WHOLE).fullmatch(disk)
    if m is None:
        raise HandoffError("disk: expected diskNsM" if kind in RESIZE else "disk: expected diskN")
    if kind in RESIZE:
        if to is None:
            raise HandoffError(f"to: {kind} needs the new size")
        options = ("--to", size_word(to))
    elif to is not None:
        raise HandoffError("to: only shrink and grow take a size")
    else:
        options = ()
    if after is not None:
        a = SLICE.fullmatch(after)
        if kind != "add" or a is None or a["whole"] != disk:
            raise HandoffError(f"after: only add takes one, a partition of {disk}")
        options = ("--after", after)
    if kind in ("add", "repartition") and layout is None or kind in ("backup-map", "grow") and layout is not None:
        raise HandoffError("layout: add and repartition need one, backup-map and grow take none")
    if not (os.path.isabs(python) and os.path.isabs(root)):
        raise HandoffError("internal: the interpreter and the project folder must be absolute paths")
    whole = m["whole"] if kind in RESIZE else disk
    folder = _new_folder(os.path.join(home, ".diskmap", "ui"), f"{at:%Y%m%d-%H%M%S}-{whole}-{kind}")
    path = None
    if layout is not None:
        path = os.path.join(folder, LAYOUT)
        _write(path, json.dumps(layout, indent=2, ensure_ascii=False) + "\n", 0o600)
    argv = (SUDO, python, "-m", "cli", "apply", kind, disk, *((path,) if path else ()), *options)
    line = f"cd {shlex.quote(root)} && {shlex.join(argv)}"
    script = os.path.join(folder, SCRIPT)
    _write(script, script_text(root, argv, os.path.join(folder, DONE)), 0o700)
    return Handoff(os.path.basename(folder), folder, path, script, argv, line)


def size_word(text: str) -> str:
    """TEXT, a size as `--to` takes it, written anew from the bytes it means (K15): the
    largest unit of its own kind (binary for 512Mi, decimal for 20G) that divides them, else
    bytes — 1.5Gi is 1536Mi. Only digits and a unit reach the shell. Raises HandoffError for
    what is not a fixed size."""
    try:
        size = parse_size(text)
    except LayoutError as e:
        raise HandoffError(f"to: {e}") from None
    if size.kind != FIXED:
        raise HandoffError(f"to: {text!r}: give a size such as 900Mi or 20G, not a percentage or rest")
    n, t = int(size.value), text.casefold()  # a fixed size: digits, a dot, a unit — its letters say which kind
    units = BINARY if "i" in t else DECIMAL if any(c in t for c in "kmgtpe") else ()
    for name, factor in units:
        if n % factor == 0:
            return f"{n // factor}{name}"
    return f"{n}B"


def script_text(root: str, argv: tuple[str, ...], done: str) -> str:
    """The script: `apply` in ROOT, its exit status into DONE, whole or not at all (written
    aside, then moved), then how it ended — green when applied, red otherwise: refused or
    cancelled (nothing ran), a step failed or interrupted (part ran; the journal says which).
    A project folder that is gone runs nothing and ends as status 1.

    The window closed mid-run (T-066): SIGHUP is trapped, so the script outlives it — sh
    runs the trap only once `apply` has ended — and DONE still comes. `apply` gets the
    SIGHUP itself (sudo passes it on) and stops between steps; the trap is caught, not
    ignored, so the commands the script runs start with SIGHUP as it was. DONE is written
    before anything goes to the terminal: when the terminal is gone, the sh of macOS keeps
    an echo it failed to write and puts it into the next file a builtin writes."""
    return "\n".join(
        [
            "#!/bin/sh",
            "# Written by diskmap ui (T-062, T-065). sudo asks for your password; diskmap apply then",
            "# shows the plan again and runs it only once you type what it asks for.",
            "# A closed window (SIGHUP) waits for apply, which stops between steps, then leaves its status.",
            "trap : HUP",
            f"cd {shlex.quote(root)} && {shlex.join(argv)}",
            "status=$?",
            f"printf '%s\\n' \"$status\" > {shlex.quote(done + '.new')} && mv -f {shlex.quote(done + '.new')} {shlex.quote(done)}",
            "echo",
            'if [ "$status" -eq 0 ]; then',
            "  printf '\\033[1;32m%s\\033[0m\\n' 'diskmap apply: applied. You can close this window.'",
            "else",
            "  printf '\\033[1;31m%s\\033[0m\\n' \"diskmap apply ended with status $status: not applied, or not in full - see above. You can close this window.\"",
            "fi",
            "",
        ]
    )


def result(home: str, handoff: str) -> int | None:
    """The exit status `diskmap apply` left for HANDOFF (a folder's name), or None while the
    script has not ended. Raises HandoffError for a name prepare does not make, a folder that
    is not there, or a status that is not one."""
    if not HANDOFF_ID.fullmatch(handoff):
        raise HandoffError("id: not a handoff")
    folder = os.path.join(home, ".diskmap", "ui", handoff)
    if not os.path.isdir(folder):
        raise HandoffError(f"no handoff {handoff}")
    try:
        with open(os.path.join(folder, DONE), encoding="ascii") as f:
            text = f.read(16)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise HandoffError(f"cannot read the exit status of {handoff}: {e}") from None
    if not STATUS.fullmatch(text) or int(text) > 255:
        raise HandoffError(f"the exit status of {handoff} is not one: {text!r}")
    return int(text)


def journaled(home: str, handoff: str) -> RunState | None:
    """The run `diskmap apply` journaled for HANDOFF, once its exit status is there: the
    first run of its kind on its disk that started no earlier than the handoff (the folder's
    name, to the second) and no later than the status was written. None: nothing was run —
    refused, cancelled, or the window closed before the plan began. Raises HandoffError as
    result() does, and for a journal that cannot be read."""
    m = HANDOFF_ID.fullmatch(handoff)
    if m is None:
        raise HandoffError("id: not a handoff")
    try:
        ended = os.stat(os.path.join(home, ".diskmap", "ui", handoff, DONE)).st_mtime
    except FileNotFoundError:
        raise HandoffError(f"{handoff} has not ended") from None
    begun = datetime.strptime(m["at"], "%Y%m%d-%H%M%S").astimezone().timestamp()
    path = journal_path(home)
    try:
        runs, _ = read_journal(path)
    except (JournalError, OSError) as e:
        raise HandoffError(f"cannot read {path}: {e}") from None
    for r in runs:
        mine = (r.strategy, r.disk) == (STRATEGIES[m["kind"]], m["disk"])
        if mine and begun <= datetime.fromisoformat(r.started).timestamp() <= ended:
            return r
    return None


def open_terminal(script: str) -> None:
    """Ask Terminal to open SCRIPT in a new window. Raises HandoffError."""
    try:
        r = subprocess.run(
            [OPEN, "-a", "Terminal", script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HandoffError(f"could not open Terminal: {e}") from None
    if r.returncode != 0:
        raise HandoffError(f"could not open Terminal: {(r.stderr or r.stdout).strip() or f'exit {r.returncode}'}")


def _new_folder(parent: str, name: str) -> str:
    os.makedirs(parent, mode=0o700, exist_ok=True)
    for n in range(1, 100):
        folder = os.path.join(parent, name if n == 1 else f"{name}-{n}")
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            continue
        return folder
    raise HandoffError(f"no free folder name for {name} in {parent}")


def _write(path: str, text: str, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
