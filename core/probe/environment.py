"""The system a plan runs on, as the journal records it (T-013, rules.md §S9). Read-only.

An update of macOS or of a file system driver can change what a command does; the
journal says which versions a run had, so a run can be read against them later:

  diskmap   its own version (core.version)
  macos     sw_vers: product, version, build
  machine   the CPU architecture; python — the interpreter diskmap ran in
  kexts     loaded kexts that are not Apple's or are Apple's file systems, with versions
            (kmutil, as doctor reads them) — Paragon's drivers are here
  bundles   every .fs bundle diskutil can format with, with its bundle version
  tools     every third-party formatter doctor finds: path, where it resolves, the first
            line of its version output and that command's exit code

The sources are doctor's (T-024): one way to read each of them.
"""

from __future__ import annotations

import os
import platform
import plistlib
from typing import Any

from core.probe.command import run_read_only
from core.probe.doctor import find_tools, parse_kexts
from core.probe.errors import UnfamiliarInput
from core.probe.filesystems import read_registry
from core.version import TESTED_MACOS, VERSION

SW_VERS = ["/usr/bin/sw_vers"]
SW_VERS_KEYS = {"ProductName", "ProductVersion", "ProductVersionExtra", "BuildVersion"}
SW_VERS_REQUIRED = {"ProductName", "ProductVersion", "BuildVersion"}


def read_environment(search_path: str, home: str) -> dict[str, Any]:
    """SEARCH_PATH is the caller's PATH, HOME expands "~" — as for doctor."""
    registry = read_registry()
    return {
        "diskmap": VERSION,
        "macos": parse_sw_vers(run_read_only(SW_VERS).decode(errors="replace")),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "kexts": file_system_kexts(
            parse_kexts(run_read_only(["/usr/bin/kmutil", "showloaded", "--list-only"]).decode())
        ),
        "bundles": {
            p.bundle: bundle_version(p.bundle_path) for p in registry.personalities if p.bundle_path is not None
        },
        "tools": {
            name: {
                "path": t.path,
                "resolved": t.resolved,
                "code": t.code,
                "version": t.output.strip().splitlines()[0] if t.output.strip() else "",
            }
            for name, t in sorted(find_tools(search_path, home).items())
        },
    }


def parse_sw_vers(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or key not in SW_VERS_KEYS:
            raise UnfamiliarInput(f"sw_vers: unfamiliar line {line!r}")
        out[key] = value.strip()
    missing = SW_VERS_REQUIRED - out.keys()
    if missing:
        raise UnfamiliarInput(f"sw_vers: no {', '.join(sorted(missing))}")
    return out


def untested_macos(macos: dict[str, str]) -> str | None:
    """What a plan says first on a macOS diskmap was not tested on (K19); None on one it was."""
    version = macos["ProductVersion"]
    if version.split(".")[0] in TESTED_MACOS:
        return None
    tested = ", ".join(f"macOS {v}" for v in TESTED_MACOS)
    return (
        f"diskmap has not been tested on macOS {version} (tested: {tested}). The plan predicts what diskutil "
        "does from runs on those; here diskutil may do something else, and the result may differ from AFTER. "
        "Try it on a disk image or a drive whose data you can lose. The journal keeps what every step printed "
        "(diskmap journal)."
    )


def read_untested_macos() -> str | None:
    return untested_macos(parse_sw_vers(run_read_only(SW_VERS).decode(errors="replace")))


def file_system_kexts(kexts: dict[str, str]) -> dict[str, str]:
    """Third-party kexts and Apple's file systems: the ones that decide how a volume mounts."""
    return {
        k: v
        for k, v in sorted(kexts.items())
        if not k.startswith("com.apple.") or k.startswith("com.apple.filesystems.")
    }


def bundle_version(path: str) -> str | None:
    """CFBundleShortVersionString of the .fs bundle at PATH (else CFBundleVersion); None:
    no readable Info.plist there — recorded as such, not guessed."""
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None
    v = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
    return v if isinstance(v, str) else None
