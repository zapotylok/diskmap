"""What diskutil says about resizing a partition in place (T-044). Pure data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResizeLimits:
    """`diskutil resizeVolume|apfs resizeContainer <slice> limits -plist`, in bytes."""

    slice: str
    apfs: bool  # asked of apfs resizeContainer: the slice is an APFS physical store
    current: int  # the partition on the map
    container_current: int  # the file system (container) in it
    minimum: int  # MinimumSizeNoGuard: as far as file usage allows
    minimum_preferred: int  # "if used for macOS"; not a limit
    maximum: int  # up to the next partition or the end of the map


UNSUPPORTED = "unsupported"  # the file system cannot be resized by diskutil
UNMOUNTED = "unmounted"  # HFS+ gives limits only while mounted


@dataclass(frozen=True)
class NotResizable:
    """diskutil refuses to give limits: WHY is UNSUPPORTED or UNMOUNTED, SAID its own words."""

    slice: str
    why: str
    said: str
