"""From the Formats dialog to a package install in a new Terminal window (T-081, K22).

The UI installs nothing. For a package on the fixed list below it writes a `.command`
script and asks Terminal to open it, as Apply does (ui/terminal.py, D37). The script prints
the command and runs it only once Return is pressed; a window closed before that runs
nothing. The person at the keyboard decides, and sees what runs: the same words they would
type themselves (§S6).

The script is the second shell text diskmap writes itself, after the Apply handoff (K14,
K22). Nothing in it comes from the page but the name of a package on the list: the command
is the list's, `brew` the absolute path found when the script is written, every word
shlex-quoted. `brew` runs as the user, never under sudo.

When the script ends it writes, into the same folder under ~/.diskmap/ui, whether the
command ran and its exit status (whole or not at all: aside, then moved); the dialog,
waiting on the folder's name, learns from them that the window is done and reads the
formats again.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime

from ui.terminal import STATUS, HandoffError, _new_folder, _write

# Package → what `brew` is given. Only a package one command installs, with nothing to
# approve in System Settings after it (K22); macFUSE, ntfs-3g and Paragon are pointers.
INSTALLS: dict[str, tuple[str, ...]] = {"e2fsprogs": ("install", "e2fsprogs")}
BREWS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")  # Apple silicon, Intel: tried after PATH
SCRIPT = "install.command"
RAN = "ran"  # written just before the command starts: it was not cancelled
DONE = "exit-status"
INSTALL_ID = re.compile(r"[0-9]{8}-[0-9]{6}-install-(?P<package>" + "|".join(INSTALLS) + r")(?:-[0-9]{1,2})?")


@dataclass(frozen=True)
class Install:
    id: str  # the folder's name: what the dialog asks about (result)
    script: str
    argv: tuple[str, ...]
    command_line: str  # the same, as a person types it


@dataclass(frozen=True)
class Outcome:
    done: bool
    ran: bool  # Return was pressed and the command started
    status: int | None  # its exit status; None while running, or when it never ran


def find_brew(search_path: str) -> str | None:
    """`brew` on SEARCH_PATH, else where Homebrew puts it; None: no Homebrew here."""
    found = shutil.which("brew", path=search_path)
    if found is not None:
        return os.path.abspath(found)
    return next((b for b in BREWS if os.access(b, os.X_OK)), None)


def prepare(package: str, home: str, at: datetime, brew: str | None) -> Install:
    """Write the script that installs PACKAGE with BREW into a new folder."""
    if package not in INSTALLS:
        raise HandoffError(f"package: expected one of {', '.join(INSTALLS)}")
    if brew is None:
        raise HandoffError("Homebrew is not found: see https://brew.sh")
    if not os.path.isabs(brew):
        raise HandoffError("internal: brew must be an absolute path")
    argv = (brew, *INSTALLS[package])
    folder = _new_folder(os.path.join(home, ".diskmap", "ui"), f"{at:%Y%m%d-%H%M%S}-install-{package}")
    script = os.path.join(folder, SCRIPT)
    _write(script, script_text(argv, os.path.join(folder, RAN), os.path.join(folder, DONE)), 0o700)
    return Install(os.path.basename(folder), script, argv, shlex.join(argv))


def script_text(argv: tuple[str, ...], ran: str, done: str) -> str:
    """The script: the command printed, then run only after Return (`read` fails on a closed
    window or Control-D: nothing runs); RAN before it starts, its status into DONE after,
    then how it ended in colour. SIGHUP is trapped, as in the Apply script: a window closed
    while `brew` runs leaves its status all the same."""
    line = shlex.join(argv)
    q = shlex.quote
    return "\n".join(
        [
            "#!/bin/sh",
            "# Written by diskmap ui (T-081, K22). Nothing runs until you press Return.",
            "trap : HUP",
            "echo 'diskmap: this installs a formatter with Homebrew:'",
            "echo",
            f"printf '  %s\\n\\n' {q(line)}",
            "printf 'Press Return to run it, or close this window to cancel. '",
            "if read -r _; then",
            f"  : > {q(ran)}",
            f"  {line}",
            "  status=$?",
            "else",
            "  status=1",
            "fi",
            f"printf '%s\\n' \"$status\" > {q(done + '.new')} && mv -f {q(done + '.new')} {q(done)}",
            "echo",
            f"if [ ! -e {q(ran)} ]; then",
            "  printf '\\033[1;31m%s\\033[0m\\n' 'Not run. You can close this window.'",
            'elif [ "$status" -eq 0 ]; then',
            "  printf '\\033[1;32m%s\\033[0m\\n' 'Installed. diskmap reads the formats again; you can close this window.'",
            "else",
            "  printf '\\033[1;31m%s\\033[0m\\n' \"brew ended with status $status - see above. You can close this window.\"",
            "fi",
            "",
        ]
    )


def result(home: str, install: str) -> Outcome:
    """How the script for INSTALL (a folder's name) ended, or that it has not yet."""
    if not INSTALL_ID.fullmatch(install):
        raise HandoffError("id: not an install")
    folder = os.path.join(home, ".diskmap", "ui", install)
    if not os.path.isdir(folder):
        raise HandoffError(f"no install {install}")
    ran = os.path.exists(os.path.join(folder, RAN))
    try:
        with open(os.path.join(folder, DONE), encoding="ascii") as f:
            text = f.read(16)
    except FileNotFoundError:
        return Outcome(False, ran, None)
    except (OSError, UnicodeDecodeError) as e:
        raise HandoffError(f"cannot read the exit status of {install}: {e}") from None
    if not STATUS.fullmatch(text) or int(text) > 255:
        raise HandoffError(f"the exit status of {install} is not one: {text!r}")
    return Outcome(True, ran, int(text) if ran else None)
