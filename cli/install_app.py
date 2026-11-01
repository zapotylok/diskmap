"""`diskmap install-app` (T-079, D54): copy the window, diskmap.app, into Applications.

Homebrew builds the window into the formula's own folder and may not write anywhere else;
this puts a copy where Launchpad and Spotlight find it — /Applications when the user may
write there, else ~/Applications, or the folder --to names. The copy runs the installed
`diskmap` (DiskmapCommand, D54), so `brew upgrade` needs this again only for a new window.

Not a disk operation: nothing here touches a partition map. It copies with `ditto`, the
way macOS copies bundles, checks the copy's signature, and replaces only a diskmap.app of
its own — another app of that name is left alone.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

NAME = "diskmap.app"
BUNDLE_ID = "io.github.zapotylok.diskmap"
DITTO = "/usr/bin/ditto"
CODESIGN = "/usr/bin/codesign"
# Where `python -m cli` runs from (libexec in the formula, the project folder in a checkout).
CODE = Path(__file__).resolve().parent.parent


class InstallError(Exception):
    """Nothing was installed; the message says why."""


def find_app(code: Path = CODE) -> Path:
    """The window that belongs to this diskmap: the formula's, next to libexec; else a
    checkout's own build (app/build.sh)."""
    for app in (code.parent / NAME, code / "app" / "build" / NAME):
        if bundle_id(app) == BUNDLE_ID:
            return app
    raise InstallError(f"no {NAME} next to this diskmap: install it with Homebrew, or build it with app/build.sh")


def destination(home: str, system: str = "/Applications") -> Path:
    """/Applications when it can be written to, as for an admin user; else ~/Applications."""
    if os.access(system, os.W_OK):
        return Path(system)
    return Path(home) / "Applications"


def bundle_id(app: Path) -> str | None:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            value = plistlib.load(f).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, str) else None


def verify(app: Path) -> None:
    r = subprocess.run([CODESIGN, "--verify", "--strict", str(app)], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise InstallError(f"the copy at {app} fails its signature check: {(r.stderr or r.stdout).strip()}")


def install(app: Path, folder: Path, check: Callable[[Path], None] = verify) -> Path:
    """Copy APP into FOLDER as diskmap.app, replacing an older diskmap.app, never another app."""
    target = folder / NAME
    if target.exists() and bundle_id(target) != BUNDLE_ID:
        raise InstallError(f"{target} is not diskmap's window ({bundle_id(target) or 'no bundle id'}); left alone")
    folder.mkdir(parents=True, exist_ok=True)
    new, old = folder / f".{NAME}.new", folder / f".{NAME}.old"
    for leftover in (new, old):
        if leftover.exists():
            shutil.rmtree(leftover)
    r = subprocess.run([DITTO, str(app), str(new)], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        shutil.rmtree(new, ignore_errors=True)
        raise InstallError(f"copying {app} failed: {(r.stderr or r.stdout).strip()}")
    try:
        check(new)
    except InstallError:
        shutil.rmtree(new, ignore_errors=True)
        raise
    # In place at once: the old copy steps aside, the new one takes its name, the old goes.
    if target.exists():
        target.rename(old)
    new.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    return target
