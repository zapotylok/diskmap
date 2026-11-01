"""`diskutil listFilesystems -plist` → core.model.filesystems.Registry (T-023). Read-only.

Every key is required: all ten were present in all eighteen entries on 2026-09-26
(macOS 26.6.2, arm64, Paragon NTFS + ExtFS installed). An entry without a bundle path
is a partition type only and must report sizes 0/0; anything else stops (§S9).
Aliases (JHFS+, FAT32, …) exist only in the text output and are not used.

The partition type a personality gets is not in listFilesystems: it is the
FSFormatContentMask of the personality in its bundle's Info.plist (T-042, D22).
Only that path of the plist is read, and read strictly: the rest of an Info.plist is
the vendor's business, but a listed personality missing from its own bundle, or a
mask that is not a string, stops.
"""

from __future__ import annotations

import dataclasses
import os
import plistlib
from collections.abc import Callable
from typing import Any

from core.model.filesystems import Personality, Registry
from core.probe.command import run_read_only
from core.probe.errors import UnfamiliarInput
from core.probe.strict import check_keys, dicts, get, optional

KEYS = {
    "Bootable",
    "Bundle",
    "BundlePath",
    "MaximumSize",
    "MinimumSize",
    "Personality",
    "RequiresBooterToBoot",
    "RequiresWholeDiskFormat",
    "SupportsResize",
    "UserVisibleName",
}


def read_filesystems() -> Any:
    return plistlib.loads(run_read_only(["/usr/sbin/diskutil", "listFilesystems", "-plist"]))


def read_info_plist(bundle_path: str) -> Any:
    with open(os.path.join(bundle_path, "Contents", "Info.plist"), "rb") as f:
        return plistlib.load(f)


def read_registry(read_info: Callable[[str], Any] = read_info_plist) -> Registry:
    """listFilesystems with every personality's content mask attached."""
    return with_content_masks(parse_filesystems(read_filesystems()), read_info)


def with_content_masks(registry: Registry, read_info: Callable[[str], Any]) -> Registry:
    """REGISTRY with content_mask filled from each bundle's Info.plist, read once per bundle."""
    infos: dict[str, Any] = {}
    out = []
    for p in registry.personalities:
        if p.bundle_path is None:
            out.append(p)
            continue
        if p.bundle_path not in infos:
            infos[p.bundle_path] = read_info(p.bundle_path)
        out.append(dataclasses.replace(p, content_mask=parse_content_mask(infos[p.bundle_path], p.name, p.bundle_path)))
    return Registry(tuple(out))


def parse_content_mask(info: Any, personality: str, bundle_path: str) -> str | None:
    what = f"{bundle_path} Info.plist"
    if type(info) is not dict:
        raise UnfamiliarInput(f"{what}: not a dictionary")
    table = get(info, "FSPersonalities", dict, what)
    entry = table.get(personality)
    if type(entry) is not dict:
        raise UnfamiliarInput(f"{what}: personality {personality!r}, listed by diskutil, is not in its bundle")
    mask = optional(entry, "FSFormatContentMask", str, f"{what} {personality!r}")
    return mask or None


def parse_filesystems(plist: Any) -> Registry:
    what = "diskutil listFilesystems"
    out = []
    for e in dicts(plist, what):
        ewhat = f"{what} {e.get('Personality')!r}"
        if set(e) != KEYS:
            check_keys(e, KEYS, ewhat)
            raise UnfamiliarInput(f"{ewhat}: missing keys {sorted(KEYS - set(e))}")
        path = get(e, "BundlePath", str, ewhat) or None
        lo, hi = get(e, "MinimumSize", int, ewhat), get(e, "MaximumSize", int, ewhat)
        if path is None and (lo, hi) != (0, 0):
            raise UnfamiliarInput(f"{ewhat}: sizes {lo}..{hi} without a bundle")
        if path is not None and not 0 < lo <= hi:
            raise UnfamiliarInput(f"{ewhat}: sizes {lo}..{hi}")
        out.append(
            Personality(
                name=get(e, "Personality", str, ewhat),
                user_visible_name=get(e, "UserVisibleName", str, ewhat),
                bundle=get(e, "Bundle", str, ewhat),
                bundle_path=path,
                min_size=lo if path is not None else None,
                max_size=hi if path is not None else None,
                bootable=get(e, "Bootable", bool, ewhat),
                requires_booter=get(e, "RequiresBooterToBoot", bool, ewhat),
                whole_disk_format=get(e, "RequiresWholeDiskFormat", bool, ewhat),
                supports_resize=get(e, "SupportsResize", bool, ewhat),
            )
        )
    names = [p.name.casefold() for p in out]
    if len(set(names)) != len(names):
        raise UnfamiliarInput(f"{what}: personality names repeat (case-insensitively)")
    if not out:
        raise UnfamiliarInput(f"{what}: no personalities at all")
    return Registry(tuple(out))
