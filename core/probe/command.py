"""The only way probe runs a process (rules.md §S3, §S4).

Commands are argv lists with absolute paths; stdin is closed (§S10). An argv that
does not start with one of READ_ONLY, is not exactly one of READ_ONLY_EXACT, and does
not match one of READ_ONLY_SLICE word for word is refused before anything is executed.
run_version() is the one way to run a third-party tool: only its version flag, from a
fixed list (T-024).

Every process starts in a session of its own (T-068): the signals a terminal sends to
its whole process group — Ctrl-C, SIGHUP when its window closes, and sudo passing them
on — never reach a probe. What they mean is diskmap's to decide (core.executor.run): a
`diskutil` killed by them would read as a check that failed, not as a stop.
"""

from __future__ import annotations

import os
import re
import subprocess

from core.probe.errors import CommandFailed, NotReadOnly

# Exact argv prefixes probe may execute. Arguments after the prefix are the
# caller's (e.g. the disk identifier), validated by the caller.
READ_ONLY: tuple[tuple[str, ...], ...] = (
    ("/usr/sbin/ioreg", "-a", "-c", "IOMedia", "-r", "-l"),
    ("/usr/sbin/diskutil", "list", "-plist"),
    ("/usr/sbin/diskutil", "info", "-plist"),
    ("/usr/sbin/diskutil", "apfs", "list", "-plist"),
    ("/usr/sbin/diskutil", "listFilesystems", "-plist"),
    ("/usr/sbin/lsof", "-n", "-P", "+D"),  # one mount point after it (core.probe.holders)
)
# Whole argv, nothing appended: `mount` with arguments mounts, and a prefix rule
# would allow that.
READ_ONLY_EXACT: tuple[tuple[str, ...], ...] = (
    ("/sbin/mount",),
    ("/usr/bin/sw_vers",),
    ("/usr/bin/kmutil", "showloaded", "--list-only"),
    ("/usr/bin/hdiutil", "info", "-plist"),
    ("/usr/bin/pluginkit", "-m", "-A", "-v", "-p", "com.apple.fskit.fsmodule"),
    ("/usr/bin/pluginkit", "-m", "-A", "-v", "-i", "io.macfuse.app.fsmodule.macfuse"),
)
# Whole argv with one slice identifier in it. Never a prefix: `resizeVolume <slice>`
# followed by anything but `limits -plist` resizes (T-044).
SLICE = "<slice>"
READ_ONLY_SLICE: tuple[tuple[str, ...], ...] = (
    ("/usr/sbin/diskutil", "resizeVolume", SLICE, "limits", "-plist"),
    ("/usr/sbin/diskutil", "apfs", "resizeContainer", SLICE, "limits", "-plist"),
)
_SLICE_ARG = re.compile(r"disk[0-9]+s[0-9]+")
# Third-party tool basename → the flag that makes it print its version and exit.
VERSION_FLAGS = {"mkntfs": "--version", "mkfs.ext4": "-V", "mke2fs": "-V"}


def run_read_only(argv: list[str]) -> bytes:
    """Run a read-only command and return its stdout. Raises on refusal or failure."""
    rc, out, err = run_read_only_answer(argv)
    if rc != 0:
        raise CommandFailed(f"{argv[0]} exited {rc}: {err.decode(errors='replace').strip()}")
    return out


def run_read_only_answer(argv: list[str]) -> tuple[int, bytes, bytes]:
    """Run a read-only command: (exit code, stdout, stderr). For a command whose refusal
    is an answer (resize limits of a file system that cannot be resized); the caller
    tells a known refusal from a failure. Raises only when the command is not allowed."""
    if not _allowed(argv):
        raise NotReadOnly(f"not a read-only probe command: {argv!r}")
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False, start_new_session=True)
    return proc.returncode, proc.stdout, proc.stderr


def _allowed(argv: list[str]) -> bool:
    if any(tuple(argv[: len(prefix)]) == prefix for prefix in READ_ONLY) or tuple(argv) in READ_ONLY_EXACT:
        return True
    return any(
        len(argv) == len(pattern)
        and all(_SLICE_ARG.fullmatch(a) is not None if w == SLICE else a == w for a, w in zip(argv, pattern))
        for pattern in READ_ONLY_SLICE
    )


def run_version(path: str) -> tuple[int, str]:
    """Run a found third-party tool with its version flag: (exit code, output).

    A non-zero exit is an answer here ("found, does not run"), not an error.
    """
    flag = VERSION_FLAGS.get(os.path.basename(path))
    if flag is None or not os.path.isabs(path):
        raise NotReadOnly(f"not a known tool to ask for its version: {path!r}")
    try:
        proc = subprocess.run(
            [path, flag], stdin=subprocess.DEVNULL, capture_output=True, check=False, start_new_session=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    return proc.returncode, (proc.stdout + proc.stderr).decode(errors="replace")
