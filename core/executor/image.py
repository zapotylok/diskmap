"""The trial mount's disk image comes into being (T-026, D34, K13).

`hdiutil create` makes a sparse image file in a folder diskmap just made for it, and
`hdiutil attach -nomount` turns that file into a blank device. These two are the only
commands diskmap runs outside an ExecutionPlan, and they cannot be in one: the executor
asks the guard about the plan's disk before every step, and before the attach there is
no disk. Neither touches a device that existed before: the one device involved is the
one they make. Everything that writes to it — and the detach — is a plan run by the
executor, under the trial's own guard (core.safety.image).

Fixed argv, stdin closed (§S4, §S10). The device is read from the attach's own answer;
anything but exactly one blank whole disk is a failure, and the image is detached and
removed again.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess

HDIUTIL = "/usr/bin/hdiutil"
SIZE = "256m"  # every format diskutil lists fits (UFSD_* need 20 MiB, FAT32 33 MiB); sparse: no space taken


class ImageError(Exception):
    """The image could not be made or attached; nothing of it is left behind."""


def create_and_attach(path: str, size: str = SIZE) -> str:
    """Make a blank sparse image at PATH (it must not exist) and attach it unmounted; its whole disk, "diskN"."""
    if os.path.lexists(path):
        raise ImageError(f"{path} already exists")
    _run([HDIUTIL, "create", "-quiet", "-size", size, "-type", "SPARSE", "-layout", "NONE", path], "create")
    if not os.path.isfile(path):
        raise ImageError(f"hdiutil create did not produce {path}")
    try:
        out = _run([HDIUTIL, "attach", "-nomount", "-plist", path], "attach")
    except ImageError:
        os.remove(path)
        raise
    try:
        entities = plistlib.loads(out)["system-entities"]
        devs = [e["dev-entry"] for e in entities]
    except (plistlib.InvalidFileException, KeyError, TypeError) as e:
        devs = [f"unreadable attach output: {e}"]
    m = re.fullmatch(r"/dev/(disk[0-9]+)", devs[0]) if len(devs) == 1 else None
    if m is None:
        for d in devs:
            if re.fullmatch(r"/dev/disk[0-9]+", d):
                subprocess.run([HDIUTIL, "detach", d], stdin=subprocess.DEVNULL, capture_output=True, check=False)
                break
        os.remove(path)
        raise ImageError(f"{path} attached as {devs}, expected one blank whole disk; detached and removed")
    return m[1]


def _run(argv: list[str], what: str) -> bytes:
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=120)
    if proc.returncode != 0:
        raise ImageError(f"hdiutil {what} exited {proc.returncode}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout
