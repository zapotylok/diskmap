"""Which devices a disk image is attached as, from `hdiutil info -plist` (T-026). Read-only.

Only the entry of the image asked about is read, and strictly (§S9): the user may have
other images attached, of kinds diskmap was never checked against, and they are none of
its business. An APFS container on the image is listed under it as well (rig), so the
devices of an image are every whole disk and slice that its data backs.
"""

from __future__ import annotations

import os
import plistlib
import re
from typing import Any

from core.probe.command import run_read_only
from core.probe.errors import UnfamiliarInput
from core.probe.strict import check_keys, dicts, get

TOP_KEYS = {"framework", "images", "revision", "vendor"}
# Seen on this Mac (rig, 2026-10-18): a sparse image, blank, partitioned, mounted, with APFS.
IMAGE_KEYS = {
    "autodiskmount",
    "blockcount",
    "blocksize",
    "diskimages2",
    "hdid-pid",
    "icon-path",
    "image-encrypted",
    "image-path",
    "image-type",
    "owner-uid",
    "removable",
    "system-entities",
    "writeable",
}
ENTITY_KEYS = {"content-hint", "dev-entry", "mount-point"}
_DEV = re.compile(r"^/dev/(disk[0-9]+(?:s[0-9]+)*)$")


def parse_image_devices(doc: Any, image: str) -> tuple[str, ...]:
    """Devices ("disk4", "disk4s1", "disk5") of the image at path IMAGE; empty: not attached."""
    what = "hdiutil info"
    if type(doc) is not dict:
        raise UnfamiliarInput(f"{what}: expected a dict")
    check_keys(doc, TOP_KEYS, what)
    want = os.path.realpath(image)
    for entry in dicts(get(doc, "images", list, what), f"{what} images"):
        path = get(entry, "image-path", str, f"{what} image")
        if os.path.realpath(path) != want:
            continue
        where = f"{what} {path}"
        check_keys(entry, IMAGE_KEYS, where)
        out = []
        for e in dicts(get(entry, "system-entities", list, where), f"{where} system-entities"):
            check_keys(e, ENTITY_KEYS, f"{where} system-entity")
            m = _DEV.match(get(e, "dev-entry", str, f"{where} system-entity"))
            if m is None:
                raise UnfamiliarInput(f"{where}: dev-entry {e['dev-entry']!r} is not a disk device")
            out.append(m[1])
        return tuple(out)
    return ()


def read_image_devices(image: str) -> tuple[str, ...]:
    return parse_image_devices(plistlib.loads(run_read_only(["/usr/bin/hdiutil", "info", "-plist"])), image)
