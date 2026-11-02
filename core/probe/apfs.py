"""`diskutil apfs list -plist` → core.model.apfs (T-022). Read-only, no root.

Shared by the system-disk guard (core.probe.system) and the partition map
(core.probe.layout). Key allowlists were collected on 2026-09-26 (macOS 26.6.2) from
this Mac's three internal containers and a rig image's container (§S9). macOS 15 adds one
container key, `Fusion` (a tester's Mac, 15.7.3, 2026-11-01, T-080): false is an ordinary
container; true — a Fusion Drive, SSD and HDD in one container — is not modelled, so it stops.
"""

from __future__ import annotations

import plistlib
import re
from typing import Any

from core.model.apfs import ApfsContainer, ApfsVolume, PhysicalStore
from core.probe.command import run_read_only
from core.probe.errors import UnfamiliarInput
from core.probe.strict import FUSION, check_keys, dicts, get, not_fusion, slice_id, store_id

TOP_KEYS = {"Containers"}
CONTAINER_KEYS = {
    "APFSContainerUUID",
    "CapacityCeiling",
    "CapacityFree",
    "ContainerReference",
    "DesignatedPhysicalStore",
    FUSION,  # macOS 15 only (core.probe.strict.not_fusion)
    "PhysicalStores",
    "Volumes",
}
STORE_KEYS = {"DeviceIdentifier", "DiskUUID", "Size"}
VOLUME_KEYS = {
    "APFSVolumeUUID",
    "CapacityInUse",
    "CapacityQuota",
    "CapacityReserve",
    "CryptoMigrationOn",
    "DeviceIdentifier",
    "Encryption",
    "FileVault",
    "Locked",
    "Name",
    "Roles",
}

_WHOLE = re.compile(r"^disk[0-9]+$")


def read_apfs() -> Any:
    return plistlib.loads(run_read_only(["/usr/sbin/diskutil", "apfs", "list", "-plist"]))


def parse_apfs(apfs: Any) -> tuple[ApfsContainer, ...]:
    what = "diskutil apfs list"
    if not isinstance(apfs, dict):
        raise UnfamiliarInput(f"{what}: top level is not a dict")
    check_keys(apfs, TOP_KEYS, what)
    return tuple(_container(c, what) for c in dicts(get(apfs, "Containers", list, what), what))


def _container(c: dict[str, Any], what: str) -> ApfsContainer:
    cwhat = f"{what} container {c.get('ContainerReference')!r}"
    check_keys(c, CONTAINER_KEYS, cwhat)
    reference = get(c, "ContainerReference", str, cwhat)
    if not _WHOLE.match(reference):
        raise UnfamiliarInput(f"{cwhat}: not a whole-disk identifier")
    not_fusion(c, cwhat)
    stores = []
    for s in dicts(get(c, "PhysicalStores", list, cwhat), f"{cwhat} PhysicalStores"):
        swhat = f"{cwhat} physical store"
        check_keys(s, STORE_KEYS, swhat)
        stores.append(PhysicalStore(bsd_name=store_id(s, "DeviceIdentifier", swhat), size=get(s, "Size", int, swhat)))
    if not stores:
        raise UnfamiliarInput(f"{cwhat}: no physical store")
    if get(c, "DesignatedPhysicalStore", str, cwhat) not in [s.bsd_name for s in stores]:
        raise UnfamiliarInput(f"{cwhat}: DesignatedPhysicalStore is not one of its physical stores")
    volumes = []
    for v in dicts(get(c, "Volumes", list, cwhat), f"{cwhat} Volumes"):
        vwhat = f"{cwhat} volume {v.get('DeviceIdentifier')!r}"
        check_keys(v, VOLUME_KEYS, vwhat)
        roles = get(v, "Roles", list, vwhat)
        if not all(type(r) is str for r in roles):
            raise UnfamiliarInput(f"{vwhat}: Roles is not a list of strings")
        volumes.append(
            ApfsVolume(
                bsd_name=slice_id(v, "DeviceIdentifier", vwhat),
                name=get(v, "Name", str, vwhat),
                roles=tuple(roles),
                capacity_in_use=get(v, "CapacityInUse", int, vwhat),
                capacity_quota=get(v, "CapacityQuota", int, vwhat),
                capacity_reserve=get(v, "CapacityReserve", int, vwhat),
                encrypted=get(v, "Encryption", bool, vwhat),
                filevault=get(v, "FileVault", bool, vwhat),
                locked=get(v, "Locked", bool, vwhat),
            )
        )
    for v in volumes:
        if not v.bsd_name.startswith(reference + "s"):
            raise UnfamiliarInput(f"{cwhat}: volume {v.bsd_name} is not a slice of {reference}")
    return ApfsContainer(
        reference=reference,
        capacity_ceiling=get(c, "CapacityCeiling", int, cwhat),
        capacity_free=get(c, "CapacityFree", int, cwhat),
        physical_stores=tuple(stores),
        volumes=tuple(volumes),
    )
