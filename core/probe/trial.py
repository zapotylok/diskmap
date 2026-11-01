"""The last trial mount of `diskmap doctor --trial`, read against this Mac now (T-026, D34).

Read-only. The trial is kept in ~/.diskmap/trial.json with the setup it was made on: what
decides whether and how a volume mounts — macOS, the file system kexts loaded, the .fs
bundles diskutil formats with, the FSKit modules registered, the formatters found. The
setup is read now the same way; any difference and the trial no longer describes this
Mac: TrialNow says what changed, and the preview says "not known" rather than repeat it.

A kept trial that cannot be read is reported as such, never taken for "no trial" in
silence and never guessed at (§S9).
"""

from __future__ import annotations

import json
import os
from typing import Any

from core.model.doctor import NO, RO, RW, Capability, Mounting, Trial, TrialNow
from core.probe.command import run_read_only
from core.probe.doctor import MACFUSE_FSKIT, parse_fskit
from core.probe.environment import read_environment

SETUP_KEYS = ("macos", "machine", "kexts", "bundles", "tools")


def trial_path(home: str) -> str:
    return os.path.join(home, ".diskmap", "trial.json")


def read_setup(search_path: str, home: str) -> dict[str, Any]:
    """What decides mounting on this Mac now: the journal's environment (core.probe.environment)
    less the interpreter, plus the FSKit modules and how each is elected."""
    env = read_environment(search_path, home)
    fskit = {}
    for argv in (
        ["/usr/bin/pluginkit", "-m", "-A", "-v", "-p", "com.apple.fskit.fsmodule"],
        ["/usr/bin/pluginkit", "-m", "-A", "-v", "-i", MACFUSE_FSKIT],
    ):
        fskit.update(parse_fskit(run_read_only(argv).decode(errors="replace")))
    return {**{k: env[k] for k in SETUP_KEYS}, "fskit": dict(sorted(fskit.items()))}


def changes(then: dict[str, Any], now: dict[str, Any]) -> list[str]:
    """What differs between two setups, one line per item: 'kexts: com.x 1.0 → not there'."""
    out = []
    for key in sorted(set(then) | set(now)):
        a, b = then.get(key), now.get(key)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    out.append(f"{key}: {k} {_said(a, k)} → {_said(b, k)}")
        else:
            out.append(f"{key}: {json.dumps(a, sort_keys=True)} → {json.dumps(b, sort_keys=True)}")
    return out


def read_trial(home: str, setup_now: dict[str, Any]) -> TrialNow:
    path = trial_path(home)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return TrialNow(None, ())
    except (OSError, ValueError) as e:
        return TrialNow(None, (), f"{path}: {e}")
    try:
        trial = decode(doc)
    except (KeyError, TypeError, ValueError) as e:
        return TrialNow(None, (), f"{path}: not a trial record — {e}")
    return TrialNow(trial, tuple(changes(dict(trial.setup), setup_now)))


def encode(trial: Trial) -> dict[str, Any]:
    return {
        "at": trial.at,
        "setup": trial.setup,
        "capabilities": [
            {
                "format": c.format,
                "created": c.created,
                "detail": c.detail,
                "mount": None
                if c.mount is None
                else {"mode": c.mount.mode, "by": c.mount.by, "detail": c.mount.detail},
            }
            for c in trial.capabilities
        ],
    }


def decode(doc: Any) -> Trial:
    if set(doc) != {"at", "setup", "capabilities"}:
        raise ValueError(f"keys {sorted(doc)}")
    caps = []
    for c in doc["capabilities"]:
        if set(c) != {"format", "created", "detail", "mount"}:
            raise ValueError(f"capability keys {sorted(c)}")
        m = c["mount"]
        mounting = None
        if m is not None:
            if set(m) != {"mode", "by", "detail"} or m["mode"] not in (RW, RO, NO):
                raise ValueError(f"mount {m!r}")
            mounting = Mounting(_str(m["mode"]), _opt(m["by"]), _str(m["detail"]))
        created = c["created"]
        if created not in (True, False, None):
            raise ValueError(f"created {created!r}")
        caps.append(Capability(_str(c["format"]), created, _str(c["detail"]), mounting))
    if not isinstance(doc["setup"], dict):
        raise TypeError("setup is not an object")
    return Trial(_str(doc["at"]), doc["setup"], tuple(caps))


def _said(d: dict[str, Any], k: str) -> str:
    return json.dumps(d[k], sort_keys=True) if k in d else "not there"


def _str(v: Any) -> str:
    if type(v) is not str:
        raise TypeError(f"expected a string, got {v!r}")
    return v


def _opt(v: Any) -> str | None:
    return None if v is None else _str(v)
