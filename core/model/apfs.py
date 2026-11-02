"""APFS containers as diskmap sees them (T-022). Their volumes are never planned for (non-goal);
the partition a container lives on may be shrunk whole (T-044, K10).

A container lives on one or more partitions (physical stores) and appears to the
system as a synthesized whole disk (`reference`); its volumes are slices of that disk.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.model.volume import VolumeInfo


@dataclass(frozen=True)
class ApfsVolume:
    bsd_name: str
    name: str
    roles: tuple[str, ...]  # empty for an ordinary user volume
    capacity_in_use: int  # bytes
    capacity_quota: int  # bytes, 0 = none
    capacity_reserve: int  # bytes, 0 = none
    encrypted: bool
    filevault: bool
    locked: bool
    volume: VolumeInfo | None = None  # mount point etc. (T-021); the guard does without


@dataclass(frozen=True)
class PhysicalStore:
    bsd_name: str  # a partition, diskNsM; or a whole disk without a partition table (K20)
    size: int  # bytes


@dataclass(frozen=True)
class ApfsContainer:
    reference: str  # the synthesized whole disk
    capacity_ceiling: int  # bytes
    capacity_free: int  # bytes
    physical_stores: tuple[PhysicalStore, ...]
    volumes: tuple[ApfsVolume, ...]

    @property
    def store_names(self) -> tuple[str, ...]:
        return tuple(s.bsd_name for s in self.physical_stores)
