"""Probe failures. None of them is ever caught to substitute a default."""


class ProbeError(Exception):
    """A read-only probe could not produce a trustworthy answer."""


class UnfamiliarInput(ProbeError):
    """Tool output does not match what diskmap was verified against (rules.md §S9).

    A macOS update that changes a key, a structure or a value domain must stop
    diskmap loudly rather than let it guess.
    """


class StalePartitions(ProbeError):
    """The kernel's partition objects are out of date with the table on disk (D24).

    Not unfamiliar input: ioreg says so itself (`Live` = false). The slice names of such
    a disk do not match its table entries, and commands address slices by name.
    """


class CommandFailed(ProbeError):
    """A read-only command exited non-zero."""


class NotReadOnly(ProbeError):
    """Probe was asked to run a command outside its read-only allowlist."""
