"""System-disk guard: which whole disks diskmap may ever plan for (T-010, K6, rules.md §S8).

Every whole disk is judged from a fresh SystemView and is either Protected, with the
reasons, or an AvailableDisk. The planner accepts only AvailableDisk, and only this
module issues one. There is no list of protected disks kept between calls: diskN
numbers are reused on hotplug, so the verdict is recomputed at every show and in the
preflight of every step (`require_available`).

Two stages:

1. Protection. A disk is protected when anything ties it to the running system or
   when diskmap cannot tell: it hosts the boot volume `/`; a volume on it is mounted
   at `/` or anywhere under /System/ (K20); Internal is true; it is an AppleRAID member or
   CoreStorage; it belongs to an APFS container that spans several physical stores
   (Fusion) or holds an unfamiliar or boot-policy role; the sources disagree about
   it; its `diskutil info` did not parse. Protection spreads through APFS both ways —
   a container on a protected disk, the disk under a protected container — until
   nothing changes.

2. Availability, by positive evidence only (allowlist). An unprotected disk is
   available when it is a physical disk on a bus verified as external
   (`EXTERNAL_BUSES`), Internal false, RemovableMediaOrExternalDevice true — or a disk
   image itself (K6: images are available to everyone, recognized explicitly rather
   than because their keys happen to match). A synthesized APFS container is never
   available: containers are read-only (non-goal). Anything else is protected with
   "no evidence it is external". These reasons do not spread: an external disk
   carrying an APFS container stays available.

An available disk carrying a macOS installation that is not the running system gets
a warning, not protection (K6).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from core.probe.system import SystemView, Unreadable, probe

# Buses a disk may be on to count as external. Verified on real devices only;
# a bus not here (Thunderbolt/PCI enclosures, SATA, SD readers…) is protected until
# a real device of that kind has been recorded and judged (K6, T-010 AC).
EXTERNAL_BUSES = {"USB"}
DISK_IMAGE_BUS = "Disk Image"

# APFS roles of a macOS installation. On an available disk they give a warning.
MACOS_ROLES = {"Data", "Preboot", "Recovery", "System", "Update", "VM"}
# Roles seen only in the internal boot-policy container (disk1 on Apple Silicon).
BOOT_POLICY_ROLES = {"Hardware", "xART"}

# The system's own mount points: /System/Volumes/* (Data, Preboot, ...) and, on macOS 15, the
# cryptex images it attaches for its assets under /System/Library/AssetsV2/ (K20).
SYSTEM_MOUNT_PREFIX = "/System/"
CORESTORAGE = "Apple_CoreStorage"

_OWNER = re.compile(r"^(disk[0-9]+)(s[0-9]+)*$")

Protect = Callable[[str, str, str], bool]


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str


@dataclass(frozen=True)
class Protected:
    bsd_name: str
    reasons: tuple[Reason, ...]


_ISSUER = object()


class AvailableDisk:
    """A whole disk the planner may work on. Issued only by this module."""

    __slots__ = ("bsd_name", "kind", "warnings")
    bsd_name: str
    kind: str  # "external" or "disk-image"
    warnings: tuple[str, ...]

    def __init__(self, issuer: object, bsd_name: str, kind: str, warnings: tuple[str, ...]) -> None:
        if issuer is not _ISSUER:
            raise TypeError("AvailableDisk is issued only by core.safety.guard")
        object.__setattr__(self, "bsd_name", bsd_name)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "warnings", warnings)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("AvailableDisk is immutable")

    def __repr__(self) -> str:
        return f"AvailableDisk({self.bsd_name!r}, kind={self.kind!r}, warnings={self.warnings!r})"


Verdict = Protected | AvailableDisk


class NotAvailable(Exception):
    """The disk may not be planned for. Carries the reasons."""

    def __init__(self, bsd_name: str, reasons: tuple[Reason, ...]) -> None:
        self.bsd_name = bsd_name
        self.reasons = reasons
        super().__init__(f"{bsd_name} is protected: " + "; ".join(f"{r.code}: {r.detail}" for r in reasons))


def require_available(bsd_name: str) -> AvailableDisk:
    """Probe the system now and return BSD_NAME as available, or raise NotAvailable."""
    verdict = assess(probe()).get(bsd_name)
    if verdict is None:
        raise NotAvailable(bsd_name, (Reason("absent", "not a whole disk present in the system now"),))
    if isinstance(verdict, Protected):
        raise NotAvailable(bsd_name, verdict.reasons)
    return verdict


def assess(view: SystemView) -> dict[str, Verdict]:
    """Judge every whole disk in VIEW. Pure; the same view gives the same verdicts."""
    names = [d.bsd_name for d in view.disks]
    hard: dict[str, list[Reason]] = {n: [] for n in names}

    def protect(name: str, code: str, detail: str) -> bool:
        """Add a reason; True if it is new."""
        if name not in hard:
            raise _Inconsistent(f"{detail}: {name} is not a listed whole disk")
        if Reason(code, detail) in hard[name]:
            return False
        hard[name].append(Reason(code, detail))
        return True

    try:
        stores = _container_stores(view, protect)
        _boot(view, stores, protect)
        _per_disk(view, protect)
        _containers(view, stores, protect)
        _spread(stores, hard, protect)
    except _Inconsistent as e:
        # The view contradicts itself about which disk is which: nothing is judged.
        reason = Reason("inconsistent", str(e))
        return {n: Protected(n, (reason,)) for n in names}

    verdicts: dict[str, Verdict] = {}
    for name in names:
        if hard[name]:
            verdicts[name] = Protected(name, tuple(hard[name]))
        else:
            verdicts[name] = _availability(view, name, stores)
    return verdicts


class _Inconsistent(Exception):
    pass


def _owner(store: str) -> str:
    """The whole disk of STORE: of a slice, or a whole disk itself (K20)."""
    m = _OWNER.match(store)
    if not m:
        raise _Inconsistent(f"{store!r} is not a disk or slice")
    return m.group(1)


def _container_stores(view: SystemView, protect: Protect) -> dict[str, tuple[str, ...]]:
    """Synthesized disk → whole disks under it, from all three sources.

    list, apfs list and info must agree on the physical stores; where they do not,
    the container is protected and the union is used, so protection spreads to every
    disk any source names.
    """
    by_list = {d.bsd_name: set(d.physical_stores) for d in view.disks if d.physical_stores}
    by_info = {
        n: set(i.physical_stores) for n, i in view.info.items() if not isinstance(i, Unreadable) and i.physical_stores
    }
    by_apfs = {c.reference: set(c.store_names) for c in view.containers}
    listed_slices = {v.bsd_name for d in view.disks for v in d.volumes}
    # A store may be a whole disk only when the list shows it bare: no partitions, no
    # container of its own (K20).
    bare_wholes = {d.bsd_name for d in view.disks if not d.volumes and not d.physical_stores}

    stores: dict[str, tuple[str, ...]] = {}
    for name in sorted(set(by_list) | set(by_info) | set(by_apfs)):
        seen = [by_list.get(name, set()), by_apfs.get(name, set())]
        if not isinstance(view.info.get(name), Unreadable):
            seen.append(by_info.get(name, set()))
        union = set().union(*seen)
        whole = sorted({_owner(s) for s in union})
        stores[name] = tuple(whole)
        if any(s != union for s in seen):
            detail = f"list, apfs list and info disagree on the physical stores of container {name}"
            for n in [name, *whole]:
                protect(n, "inconsistent", detail)
        for s in sorted(union - listed_slices - bare_wholes):
            for n in [name, _owner(s)]:
                detail = f"physical store {s} of container {name} is neither a listed partition nor a bare whole disk"
                protect(n, "inconsistent", detail)
    return stores


def _boot(view: SystemView, stores: dict[str, tuple[str, ...]], protect: Protect) -> None:
    boot = view.boot
    detail = f"hosts the boot volume / ({boot.bsd_name})"
    for name in {boot.parent_whole_disk, boot.container}:
        protect(name, "boot-volume", detail)
    for s in boot.physical_stores:
        protect(_owner(s), "boot-volume", detail)
    for n in stores.get(boot.container, ()):
        protect(n, "boot-volume", detail)


def _per_disk(view: SystemView, protect: Protect) -> None:
    for disk in view.disks:
        mounts = [(v.bsd_name, mp) for v in disk.volumes for mp in v.mount_points]
        if disk.mount_point is not None:  # a file system over the whole disk
            mounts.append((disk.bsd_name, disk.mount_point))
        for name, mp in mounts:
            if mp == "/" or mp.startswith(SYSTEM_MOUNT_PREFIX):
                protect(disk.bsd_name, "system-mount", f"{name} is mounted at {mp}")
        for v in disk.volumes:
            if v.content == CORESTORAGE:
                protect(disk.bsd_name, "corestorage", f"{v.bsd_name} is a CoreStorage physical volume")
        info = view.info[disk.bsd_name]
        if isinstance(info, Unreadable):
            protect(disk.bsd_name, "unreadable", info.reason)
            continue
        if info.internal:
            protect(disk.bsd_name, "internal", "diskutil reports Internal = true")
        if info.raid:
            protect(disk.bsd_name, "raid", "AppleRAID set or member")


def _containers(view: SystemView, stores: dict[str, tuple[str, ...]], protect: Protect) -> None:
    for c in view.containers:
        if len(c.physical_stores) > 1:
            detail = f"APFS container {c.reference} spans {len(c.physical_stores)} physical stores (Fusion)"
            for n in [c.reference, *stores[c.reference]]:
                protect(n, "multi-store", detail)
        for v in c.volumes:
            unfamiliar = set(v.roles) - MACOS_ROLES - BOOT_POLICY_ROLES
            if unfamiliar:
                protect(c.reference, "unfamiliar-role", f"{v.bsd_name} has APFS roles {sorted(unfamiliar)}")
            policy = set(v.roles) & BOOT_POLICY_ROLES
            if policy:
                protect(c.reference, "boot-policy", f"{v.bsd_name} has APFS roles {sorted(policy)}")


def _spread(stores: dict[str, tuple[str, ...]], hard: dict[str, list[Reason]], protect: Protect) -> None:
    # A reason is not reflected back to the disk it came from: "disk7 is under a
    # protected disk8" must not make disk8 "on a protected disk7". Terminates: each
    # pass either adds a reason or stops, and reasons are finite.
    changed = True
    while changed:
        changed = False
        for container, wholes in stores.items():
            for whole in wholes:
                down = Reason("under-protected", f"physical store of protected container {container}")
                up = Reason("on-protected", f"container on protected disk {whole}")
                if any(r != up for r in hard[container]):
                    changed |= protect(whole, down.code, down.detail)
                if any(r != down for r in hard[whole]):
                    changed |= protect(container, up.code, up.detail)


def _availability(view: SystemView, name: str, stores: dict[str, tuple[str, ...]]) -> Verdict:
    info = view.info[name]
    assert not isinstance(info, Unreadable)  # protected in _per_disk
    if name in stores:
        detail = "synthesized APFS container: containers are read-only in diskmap"
        return Protected(name, (Reason("synthesized", detail),))
    if info.internal or not info.external:
        kind = None
    elif info.bus == DISK_IMAGE_BUS and info.virtual_or_physical == "Virtual":
        kind = "disk-image"
    elif info.bus in EXTERNAL_BUSES and info.virtual_or_physical == "Physical":
        kind = "external"
    else:
        kind = None
    if kind is None:
        detail = (
            f"no evidence it is external: BusProtocol={info.bus!r}, Internal={info.internal}, "
            f"RemovableMediaOrExternalDevice={info.external}, VirtualOrPhysical={info.virtual_or_physical!r}"
        )
        return Protected(name, (Reason("not-external", detail),))
    return AvailableDisk(_ISSUER, name, kind, _warnings(view, name, stores))


def _warnings(view: SystemView, name: str, stores: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    out = []
    for c in view.containers:
        if name in stores.get(c.reference, ()) and any("System" in v.roles for v in c.volumes):
            out.append(f"APFS container {c.reference} holds a macOS installation (not the running system)")
    return tuple(out)
