"""What `diskmap show` maps: every physical whole disk, or the ones asked for (T-030). Read-only.

The list of disks comes from the full `diskutil list -plist` (WholeDisks — the text
listing hides disk1/disk2 on Apple Silicon), read through core.probe.system, the
same view the guard judges. A synthesized APFS container is not a disk of its own
here: it hangs on the partition that is its physical store (D11).

A disk whose map cannot be read is kept as Failed with the reason, and the rest are
still mapped (as core.probe.system does with Unreadable, K6). Failed is shown, never
filled in: nothing about that disk is guessed.

Mounted snapshots come from the same view: `/` is a sealed snapshot of the System
volume, and the volume's own `diskutil info` says "not mounted" (D12).

Every disk carries the guard's verdict on that same view (T-031, K6): protected disks
are shown read-only with the reasons, never hidden unless asked (--external), and then
with a line saying what was hidden. The map holds Access — what the verdict says —
not the AvailableDisk itself: the planner's token does not travel through the UI (D19).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from core.model.layout import Disk, Unpartitioned
from core.probe import system
from core.probe.errors import ProbeError
from core.probe.layout import read_layout
from core.safety.guard import AvailableDisk, Reason, Verdict, assess


@dataclass(frozen=True)
class Failed:
    bsd_name: str
    reason: str


@dataclass(frozen=True)
class Access:
    """The guard's verdict on a disk, for display only (D19)."""

    available: bool
    kind: str | None  # "external" / "disk-image" when available
    reasons: tuple[Reason, ...]  # why it is protected; empty when available
    warnings: tuple[str, ...]  # on an available disk (K6: a macOS install on it)


@dataclass(frozen=True)
class DiskMap:
    disks: tuple[Disk | Unpartitioned | Failed, ...]
    snapshots: Mapping[str, tuple[str, ...]]  # volume BSD name → mount points of its mounted snapshots
    access: Mapping[str, Access] = field(default_factory=dict)  # whole disk → verdict, shown or hidden
    hidden: tuple[str, ...] = ()  # protected disks left out by --external


def read_map(names: Sequence[str] = (), external: bool = False) -> DiskMap:
    """Map NAMES (whole disks, `diskN` or `/dev/diskN`), or every physical disk. Read-only.
    EXTERNAL leaves protected disks out, and says which (T-031)."""
    return build_map(system.probe(), names, external, read_layout)


def build_map(
    view: system.SystemView,
    names: Sequence[str],
    external: bool,
    read: Callable[[str], Disk | Unpartitioned],
) -> DiskMap:
    """read_map() on a given VIEW, with READ (read_layout) mapping one whole disk."""
    access = {name: access_of(v) for name, v in assess(view).items()}
    disks: list[Disk | Unpartitioned | Failed] = []
    hidden: list[str] = []
    for name, refusal in select(view, names):
        if external and name in access and not access[name].available:
            hidden.append(name)
            continue
        if refusal is not None:
            disks.append(Failed(name, refusal))
            continue
        try:
            disks.append(read(name))
        except ProbeError as e:
            disks.append(Failed(name, str(e)))
    return DiskMap(tuple(disks), snapshots(view), access, tuple(hidden))


def access_of(verdict: Verdict) -> Access:
    if isinstance(verdict, AvailableDisk):
        return Access(True, verdict.kind, (), verdict.warnings)
    return Access(False, None, verdict.reasons, ())


def select(view: system.SystemView, names: Sequence[str]) -> list[tuple[str, str | None]]:
    """(whole disk, why it cannot be mapped or None), in WholeDisks order when NAMES is empty."""
    listed = {d.bsd_name: d for d in view.disks}
    if not names:
        return [(d.bsd_name, None) for d in view.disks if not d.physical_stores]
    out: list[tuple[str, str | None]] = []
    for raw in names:
        name = raw.removeprefix("/dev/")
        disk = listed.get(name)
        if disk is None:
            out.append((name, "not a whole disk in `diskutil list`"))
        elif disk.physical_stores:
            stores = ", ".join(disk.physical_stores)
            owners = " ".join(sorted({s.rsplit("s", 1)[0] for s in disk.physical_stores}))
            out.append((name, f"APFS container on {stores}: shown under its store — diskmap show {owners}"))
        else:
            out.append((name, None))
    return out


def snapshots(view: system.SystemView) -> dict[str, tuple[str, ...]]:
    return {v.bsd_name: v.snapshot_mount_points for d in view.disks for v in d.volumes if v.snapshot_mount_points}
