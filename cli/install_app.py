"""`diskmap install-app` (T-079, D54): copy the window, diskmap.app, into Applications.

Homebrew builds the window into the formula's own folder and may not write anywhere else;
this puts a copy where Launchpad and Spotlight find it — /Applications when the user may
write there, else ~/Applications, or the folder --to names. The copy runs the installed
`diskmap` (DiskmapCommand, D54); after `brew upgrade` it is brought up to the new window the
next time it starts `diskmap ui` (`refresh_started_by`, D58), so install-app is run once.

Not a disk operation: nothing here touches a partition map. It copies with `ditto`, the
way macOS copies bundles, checks the copy's signature, and replaces only a diskmap.app of
its own — another app of that name is left alone.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

NAME = "diskmap.app"
BUNDLE_ID = "io.github.zapotylok.diskmap"
# Set by diskmap.app for the `diskmap ui` it starts: the bundle it runs from (app/diskmap.swift).
APP_ENV = "DISKMAP_APP"
DITTO = "/usr/bin/ditto"
CODESIGN = "/usr/bin/codesign"
# Where `python -m cli` runs from (libexec in the formula, the project folder in a checkout).
CODE = Path(__file__).resolve().parent.parent


class InstallError(Exception):
    """Nothing was installed; the message says why."""


def find_app(code: Path = CODE) -> Path:
    """The window that belongs to this diskmap: the formula's, next to libexec. A checkout's
    own build is "diskmap dev", another bundle id: it runs its own code, never the installed
    diskmap, and is not copied into Applications (D58)."""
    app = code.parent / NAME
    if bundle_id(app) == BUNDLE_ID:
        return app
    raise InstallError(
        f"no {NAME} next to this diskmap: install it with Homebrew (brew install zapotylok/diskmap/diskmap)"
    )


def destination(home: str, system: str = "/Applications") -> Path:
    """/Applications when it can be written to, as for an admin user; else ~/Applications."""
    if os.access(system, os.W_OK):
        return Path(system)
    return Path(home) / "Applications"


def bundle_id(app: Path) -> str | None:
    return _info_string(app, "CFBundleIdentifier")


def _info_string(app: Path, key: str) -> str | None:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None
    value = info.get(key) if isinstance(info, dict) else None
    return value if isinstance(value, str) else None


def refresh(copy: Path, app: Path, check: Callable[[Path], None] | None = None) -> Path | None:
    """Bring COPY — the diskmap.app that started this diskmap — up to the formula's window APP
    (D58). Only a copy of this very installation: named diskmap.app, diskmap's bundle id, running
    the same command as APP, of another version. None: nothing to do."""
    if copy.name != NAME or copy.resolve() == app.resolve():
        return None
    if bundle_id(copy) != BUNDLE_ID or bundle_id(app) != BUNDLE_ID:
        return None
    command = _info_string(app, "DiskmapCommand")
    if command is None or _info_string(copy, "DiskmapCommand") != command:
        return None
    if _info_string(copy, "CFBundleShortVersionString") == _info_string(app, "CFBundleShortVersionString"):
        return None
    return install(app, copy.parent, check or verify)


def refresh_started_by(environ: Mapping[str, str], finder: Callable[[], Path] = find_app) -> str | None:
    """For `diskmap ui`: refresh the copy that started it, if any (APP_ENV). One line saying what
    happened, or None when nothing did. Never raises: the window opens either way."""
    started_by = environ.get(APP_ENV)
    if not started_by:
        return None
    try:
        app = finder()
        target = refresh(Path(started_by), app)
    except (InstallError, OSError) as e:
        return f"ui: {started_by} not brought up to date: {e}"
    if target is None:
        return None
    return f"ui: {target} brought up to {_info_string(target, 'CFBundleShortVersionString')}; the next start runs it"


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
