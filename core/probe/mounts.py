"""Who mounted what, from /sbin/mount — the only fact about mounting (T-024, D16, T-053).

Read-only, no root. Only /dev lines are ours (devfs, autofs maps and network shares are
skipped); a /dev line of an unfamiliar shape stops the reading (§S9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.probe.command import run_read_only
from core.probe.errors import UnfamiliarInput

_LINE = re.compile(r"^/dev/(\S+) on (.+) \(([^,)]+)((?:, [^,)]+)*)\)$")


@dataclass(frozen=True)
class Mount:
    device: str  # "disk4s3"
    point: str
    type: str  # "ufsd_ExtFS", "hfs", "msdos"
    options: tuple[str, ...]

    @property
    def by(self) -> str:
        """What mounted it: "fskit" for an FSKit module (its type is the file system's),
        else the mount type."""
        return "fskit" if "fskit" in self.options else self.type

    @property
    def writable(self) -> bool:
        return "read-only" not in self.options


def parse_mounts(text: str) -> list[Mount]:
    out = []
    for raw in text.splitlines():
        if not raw.startswith("/dev/"):
            continue
        m = _LINE.match(raw)
        if m is None:
            raise UnfamiliarInput(f"mount: unfamiliar line {raw!r}")
        out.append(Mount(m[1], m[2], m[3], tuple(o.strip() for o in m[4].split(",") if o.strip())))
    return out


def read_mounts() -> list[Mount]:
    return parse_mounts(run_read_only(["/sbin/mount"]).decode(errors="replace"))
