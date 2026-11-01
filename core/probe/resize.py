"""`diskutil resizeVolume|apfs resizeContainer <slice> limits -plist` → core.model.resize (T-044).

Read-only: `limits` takes no action (man diskutil), and the argv is allowed only word for
word (core.probe.command.READ_ONLY_SLICE), never as a prefix of a real resize.

An APFS physical store is asked through `apfs resizeContainer`; `resizeVolume` sends it
there. A refusal diskutil was seen to give is an answer, not a failure:
- "file system format does not support resizing" — ExFAT, FAT32, UFSD_NTFS, UFSD_EXTFS4,
  a blank slot (rig, 2026-10-02): only HFS+ and APFS can be shrunk in place;
- "is this volume … unmounted?" — HFS+ answers limits only while mounted.
Anything else stops (§S9): rc 0 without a plist, unfamiliar keys, an unknown refusal.

Key allowlist collected on 2026-10-02 (macOS 26.6.2, arm64) on rig images: Journaled
HFS+, Case-sensitive Journaled HFS+, APFS physical stores (corpus tests/data/resize/).
"""

from __future__ import annotations

import plistlib
from typing import Any

from core.model.resize import UNMOUNTED, UNSUPPORTED, NotResizable, ResizeLimits
from core.probe.command import run_read_only_answer
from core.probe.errors import CommandFailed, UnfamiliarInput
from core.probe.strict import check_keys, get

DISKUTIL = "/usr/sbin/diskutil"
LIMITS_KEYS = {"ContainerCurrentSize", "CurrentSize", "MaximumSize", "MinimumSizeNoGuard", "MinimumSizePreferred"}
APFS_TYPE = "APFSPhysicalStore"
# diskutil's words for "no limits for this slice", and what they mean.
KNOWN_REFUSALS = {
    "file system format does not support resizing": UNSUPPORTED,
    "is this volume (or its logical volume(s)) unmounted?": UNMOUNTED,
}


def read_limits(slice_name: str, apfs: bool) -> ResizeLimits | NotResizable:
    argv = (
        [DISKUTIL, "apfs", "resizeContainer", slice_name, "limits", "-plist"]
        if apfs
        else [DISKUTIL, "resizeVolume", slice_name, "limits", "-plist"]
    )
    rc, out, err = run_read_only_answer(argv)
    return parse_limits(slice_name, apfs, rc, out, err)


def parse_limits(slice_name: str, apfs: bool, rc: int, out: bytes, err: bytes) -> ResizeLimits | NotResizable:
    what = f"resize limits of {slice_name}"
    if rc != 0:
        said = (err + out).decode(errors="replace").strip()
        for words, why in KNOWN_REFUSALS.items():
            if words in said:
                return NotResizable(slice_name, why, said)
        raise CommandFailed(f"{what}: diskutil exited {rc}: {said}")
    try:
        doc: Any = plistlib.loads(out)
    except plistlib.InvalidFileException:
        raise UnfamiliarInput(f"{what}: diskutil exited 0 without a plist: {out[:200]!r}") from None
    if type(doc) is not dict:
        raise UnfamiliarInput(f"{what}: expected a dict")
    if apfs:
        check_keys(doc, LIMITS_KEYS | {"Type"}, what)
        if get(doc, "Type", str, what) != APFS_TYPE:
            raise UnfamiliarInput(f"{what}: Type {doc['Type']!r}, expected {APFS_TYPE!r}")
    else:
        check_keys(doc, LIMITS_KEYS, what)
    lim = ResizeLimits(
        slice_name,
        apfs,
        current=get(doc, "CurrentSize", int, what),
        container_current=get(doc, "ContainerCurrentSize", int, what),
        minimum=get(doc, "MinimumSizeNoGuard", int, what),
        minimum_preferred=get(doc, "MinimumSizePreferred", int, what),
        maximum=get(doc, "MaximumSize", int, what),
    )
    if not 0 < lim.minimum <= lim.current <= lim.maximum:
        raise UnfamiliarInput(f"{what}: minimum {lim.minimum}, current {lim.current}, maximum {lim.maximum}")
    return lim
