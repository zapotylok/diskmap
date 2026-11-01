"""The trial mount's own guard (T-026, D34): it writes only to the image it created.

`diskmap doctor --trial` makes a disk image, attaches it and formats it over and over.
Before every step — on top of the system-disk guard the executor asks anyway (§S8) — the
plan's disk, and every device the step's argv names, must be one that this very image
file backs, asked of hdiutil at that moment: device numbers are reused after a detach,
so nothing is carried over from the attach. There is no way to point it elsewhere.
"""

from __future__ import annotations

import re

from core.planner.plan import Step
from core.probe.images import read_image_devices

_DEV = re.compile(r"/dev/r?(disk[0-9]+(?:s[0-9]+)*)")


class NotTrialImage(Exception):
    """A device the trial was about to touch is not backed by its own image."""


def require_trial_image(image: str, disk: str, step: Step) -> None:
    """Raise NotTrialImage unless DISK and every device STEP's argv names belong to IMAGE now."""
    devices = set(read_image_devices(image))
    named = (
        {disk}
        | {d for arg in step.command[1:] for d in _DEV.findall(arg)}
        | {a for a in step.command[1:] if re.fullmatch(r"disk[0-9]+(?:s[0-9]+)*", a)}
    )
    alien = sorted(named - devices)
    if alien:
        raise NotTrialImage(
            f"refused: {', '.join(alien)} {'is' if len(alien) == 1 else 'are'} not backed by the trial image {image} "
            f"(it backs {', '.join(sorted(devices)) or 'nothing now'})"
        )
