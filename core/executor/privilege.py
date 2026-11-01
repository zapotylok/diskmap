"""Who runs which step (T-051, D32): `diskmap apply` runs under sudo as a whole, and a
step that does not need root runs as the user who called sudo.

Not sudo step by step: sudo's ticket is keyed by the terminal (timestamp_type=tty), and a
step has none (core.executor.command, T-054) — `sudo -n` there finds no ticket. Given the
terminal, sudo's pty passes what the user types to the command, which is an answer the plan
does not declare (§S10). Measured on macOS 26.6.2, sudo 1.9.17p2: testrig/measure_sudo.py.

So the process is root, and root goes only where the plan says (needs_root, `sudo ` in the
preview): every other step gets the invoking user's uid, gid and groups back, as if it
were typed without sudo. Files diskmap writes itself (the journal) are given to that user.

The groups are set by initgroups, as login and sudo do, not as a list (setgroups, the
`extra_groups` of subprocess): macOS caps that list at NGROUPS_MAX, 16, and a user may be
in more — the kernel resolves the rest through the directory service once initgroups has
named the user (found under sudo on this Mac: 17 groups, `too many extra_groups`).
"""

from __future__ import annotations

import os
import pwd
from collections.abc import Callable, Mapping
from dataclasses import dataclass


class PrivilegeError(Exception):
    """Root, with a SUDO_UID that does not name a user: nobody to give the steps back to."""


@dataclass(frozen=True)
class Invoker:
    """The user who called sudo."""

    name: str
    uid: int
    gid: int
    home: str


def invoker(euid: int, environ: Mapping[str, str]) -> Invoker | None:
    """The user to run steps without needs_root as; None when there is nobody to drop to:
    EUID is not root (every step runs as this user already), or root not through sudo."""
    if euid != 0 or "SUDO_UID" not in environ:
        return None
    try:
        uid, gid = int(environ["SUDO_UID"]), int(environ["SUDO_GID"])
    except (KeyError, ValueError):
        raise PrivilegeError(
            f"SUDO_UID={environ.get('SUDO_UID')!r} SUDO_GID={environ.get('SUDO_GID')!r}: not what sudo sets"
        ) from None
    if uid == 0:
        return None  # sudo from root: root already
    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        raise PrivilegeError(f"SUDO_UID={uid}: no such user") from None
    return Invoker(pw.pw_name, uid, gid, pw.pw_dir)


def become(user: Invoker) -> Callable[[], None]:
    """What a child does, as root, right before exec, to be USER: their groups, their
    gid, their uid. In that order: the first two need the root that the last gives up —
    setuid as root sets the saved uid too, there is no way back."""

    def drop() -> None:
        os.initgroups(user.name, user.gid)
        os.setgid(user.gid)
        os.setuid(user.uid)

    return drop
