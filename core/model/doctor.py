"""What `diskmap doctor` reports: per file system, how it is created, which drivers
could mount it, and how volumes of it are mounted right now (T-024, D5, D16).

Pure data. Assembled read-only by core.probe.doctor. Each column says what was
observed, never what "should" happen: a format diskutil lists is "listed", not
"works"; a mount column without a mounted volume is empty.

What a format can do on this Mac — be created, be mounted rw, ro or not at all, and by
what — is known only from trying (T-026, D34): `diskmap doctor --trial` makes and mounts
each format on a disk image of its own and keeps a Trial. A Trial holds for the setup it
was made on (macOS, file system kexts and bundles, FSKit modules, formatters); once any of
them differs, it says nothing about this Mac any more.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Creator states. A diskutil personality is only ever LISTED: formatting is not tried.
LISTED = "listed"
ABSENT = "absent"
FAILS = "fails"
WORKS = "works"


@dataclass(frozen=True)
class Creator:
    how: str  # "diskutil UFSD_NTFS", "mkntfs"
    state: str  # LISTED / ABSENT / FAILS / WORKS
    detail: str  # version line, error, bundle or resolved path
    install: str | None = None  # a concrete command or pointer, when ABSENT


@dataclass(frozen=True)
class Driver:
    name: str  # "Paragon NTFS kext"
    loaded: bool | None  # None: cannot be observed read-only
    detail: str
    install: str | None = None


@dataclass(frozen=True)
class MountFact:
    """Volumes of this file system mounted now, grouped by who mounted them and how."""

    driver: str  # "fskit", or the mount type: "hfs", "apfs", "ufsd_NTFS", ...
    writable: bool
    count: int


@dataclass(frozen=True)
class Row:
    family: str  # the file system as `mount` and FilesystemType call it: "ntfs", "hfs", "ext"
    creators: tuple[Creator, ...]
    drivers: tuple[Driver, ...]
    mounts: tuple[MountFact, ...]  # empty: nothing mounted now


@dataclass(frozen=True)
class Report:
    rows: tuple[Row, ...]
    unattributed: tuple[MountFact, ...]  # mounted by a driver doctor maps to no family


@dataclass(frozen=True)
class Ext4Mounting:
    """Who would mount a new ext4 volume on this Mac, as observed now (T-053, T-026): the
    file system bundle whose probe takes ext, and whether what it mounts with is loaded.
    A prediction from the same sources as doctor; the plan's postflight checks the fact."""

    bundle: str | None  # "ufsd_ExtFS"; None: diskutil lists no bundle that takes ext
    driver: str | None  # "Paragon ExtFS kext, loaded 46.0.14"; None: not loaded (or no bundle)


# How a volume of a format mounts on this Mac, from a trial mount (T-026).
RW = "rw"
RO = "ro"
NO = "no"


@dataclass(frozen=True)
class Mounting:
    """can_mount, as found: the volume mounted read-write or read-only by BY, or not at all."""

    mode: str  # RW / RO / NO
    by: str | None  # what mounted it, as `mount` names it: "fskit", or the mount type ("ufsd_ExtFS",
    #                 "hfs"); None with NO
    detail: str  # NO: why, as the failed step or `mount` said it; else where it was mounted


@dataclass(frozen=True)
class Capability:
    """One format on this Mac, as a trial mount found it: can_create and can_mount."""

    format: str  # as a layout writes it: a diskutil personality's name, "ntfs", "ext4"
    created: bool | None  # None: not tried — diskmap does not plan this format (detail says why)
    detail: str  # created: what diskutil names the new file system; else what failed or why not tried
    mount: Mounting | None  # None: not tried — not created, or the trial stopped before mounting


@dataclass(frozen=True)
class Trial:
    at: str  # when, ISO 8601 with the offset
    setup: Mapping[str, Any]  # what decides mounting, as read then (core.probe.trial.read_setup)
    capabilities: tuple[Capability, ...]

    def find(self, fmt: str) -> Capability | None:
        """By format as a layout writes it; case is ignored, as diskutil ignores it."""
        folded = fmt.strip().casefold()
        return next((c for c in self.capabilities if c.format.casefold() == folded), None)


@dataclass(frozen=True)
class TrialNow:
    """The last trial, read against this Mac now."""

    trial: Trial | None  # None: none was made, or it cannot be read (PROBLEM)
    changed: tuple[str, ...]  # what of the setup differs now; empty: the same setup
    problem: str | None = None  # why the kept trial cannot be read

    @property
    def holds(self) -> bool:
        return self.trial is not None and not self.changed
