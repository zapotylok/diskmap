"""The formats a layout can name on this Mac, for a program (T-061, the web UI): what the
editor offers, with why one is not planned and how this Mac mounted it in the last trial
(T-026). Read-only.

The list is the trial's (core.planner.trial.trial_formats: diskutil's personalities that
format, in its order, then ntfs and ext4) plus efi, which a layout names on request only
(K9). A format is `plannable` when the planner would take it; whether a given size or
label fits it is still the plan's to say.
"""

from __future__ import annotations

import json
from typing import Any

from core.model.doctor import TrialNow
from core.model.filesystems import Registry
from core.planner import formats as F
from core.planner.common import Tools, label_rule
from core.planner.trial import trial_formats

FORMAT = 1
NAMES = {F.NTFS: "NTFS (mkntfs)", F.EXT4: "ext4 (mke2fs)", F.EFI: "EFI system partition (FAT32)"}


def formats_json(registry: Registry, tools: Tools, trial: TrialNow) -> str:
    listed = [*trial_formats(registry, tools), (F.EFI, None)]
    out = []
    for name, why in listed:
        entry: dict[str, Any] = {"format": name, "name": NAMES.get(name), "plannable": why is None, "why": why}
        if why is None:
            try:
                f = F.resolve_format(name, registry)
            except F.FormatError as e:
                entry |= {"plannable": False, "why": str(e)}
            else:
                entry |= {
                    "kind": f.kind,
                    "type": f.type,
                    "min_size": f.min_size,
                    "max_size": f.max_size,
                    "label": label_rule(f),  # required / optional / none
                }
                if f.personality is not None:
                    entry["name"] = f.personality.user_visible_name
        if entry["name"] is None:
            p = registry.find(name)
            entry["name"] = p.user_visible_name if p is not None else name
        entry["mount"] = _mount(trial, name)
        out.append(entry)
    t = trial.trial
    doc = {
        "format": FORMAT,
        "formats": out,
        "trial": {
            "at": t.at if t is not None else None,
            "holds": trial.holds,
            "changed": list(trial.changed),
            "problem": trial.problem,
        },
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _mount(trial: TrialNow, name: str) -> dict[str, Any] | None:
    """How the last trial mounted NAME; None: no trial, or it did not get that far."""
    c = trial.trial.find(name) if trial.trial is not None else None
    if c is None or c.mount is None:
        return None
    return {"mode": c.mount.mode, "by": c.mount.by, "detail": c.mount.detail}


def read_formats(search_path: str, home: str) -> str:
    """formats_json of this Mac, now."""
    from core.probe.doctor import mke2fs_path, working_tool
    from core.probe.filesystems import read_registry
    from core.probe.trial import read_setup, read_trial

    tools = Tools(working_tool("mkntfs", search_path, home), mke2fs_path(search_path, home))
    return formats_json(read_registry(), tools, read_trial(home, read_setup(search_path, home)))
