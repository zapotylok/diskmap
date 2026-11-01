"""Strict accessors for tool plists: unknown keys and wrong types stop (rules.md §S9)."""

from __future__ import annotations

import re
from typing import Any

from core.probe.errors import UnfamiliarInput


def check_keys(obj: dict[str, Any], allowed: set[str], what: str) -> None:
    unknown = set(obj) - allowed
    if unknown:
        raise UnfamiliarInput(f"{what}: unfamiliar keys {sorted(unknown)}")


def get(obj: dict[str, Any], key: str, typ: type, what: str) -> Any:
    if key not in obj:
        raise UnfamiliarInput(f"{what}: missing key {key!r}")
    return typed(obj[key], key, typ, what)


def optional(obj: dict[str, Any], key: str, typ: type, what: str) -> Any:
    return typed(obj[key], key, typ, what) if key in obj else None


def typed(value: Any, key: str, typ: type, what: str) -> Any:
    # type() rather than isinstance(): a bool must not pass for an int.
    if type(value) is not typ:
        raise UnfamiliarInput(f"{what}: {key!r} is {type(value).__name__}, expected {typ.__name__}")
    return value


_SLICE = re.compile(r"^disk[0-9]+(s[0-9]+)+$")


def dicts(value: Any, what: str) -> list[dict[str, Any]]:
    if type(value) is not list or not all(type(v) is dict for v in value):
        raise UnfamiliarInput(f"{what}: expected a list of dicts")
    return value


def slice_id(obj: dict[str, Any], key: str, what: str) -> str:
    """A BSD slice identifier (diskNsM, diskNsMsK) under KEY."""
    name = get(obj, key, str, what)
    if not _SLICE.match(name):
        raise UnfamiliarInput(f"{what}: {key} {name!r} is not a slice identifier")
    return name
