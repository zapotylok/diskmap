"""The layout the user asks for, before any planning (T-040, D20).

Pure data and arithmetic, no probing. Parsed from a JSON document already loaded
into Python objects; every rejection happens here, before a plan is generated.

Sizes use diskutil's units so that what the user writes means what diskutil would
read: K/M/G/T/P/E are powers of ten, Ki/Mi/Gi/Ti/Pi/Ei powers of two, an optional
trailing B, any case. Three deliberate departures, each a trap in a declarative
file: a number without a unit is refused (diskutil takes it as bytes), zero is
refused (diskutil's "all possible" is spelled `rest`), and a percentage never
reaches diskutil — `resolve` turns it into bytes of a capacity the strategy names,
so the preview shows exact sizes a person can repeat (rules.md §S6).

What capacity is — the whole usable area, one free gap — and what diskutil takes
off it (EFI, booters, alignment) is the planner's business (T-041..T-043).
The format is kept as written: resolving it to a diskutil personality or to
ntfsprogs (K4, D7) is the strategy's job (T-042).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any

FIXED = "fixed"
PERCENT = "percent"
REST = "rest"

_UNITS = {
    "": 1,
    "k": 10**3,
    "m": 10**6,
    "g": 10**9,
    "t": 10**12,
    "p": 10**15,
    "e": 10**18,
    "ki": 2**10,
    "mi": 2**20,
    "gi": 2**30,
    "ti": 2**40,
    "pi": 2**50,
    "ei": 2**60,
}
_NUMBER = r"(?P<num>[0-9]+(?:\.[0-9]+)?)"
_FIXED = re.compile(_NUMBER + r"(?P<unit>[kmgtpe]i?)?(?P<b>b)?", re.IGNORECASE)
_PERCENT = re.compile(_NUMBER + r"%")
_REST = {"rest", "r"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_DOC_KEYS = {"partitions"}
_PART_KEYS = {"format", "label", "size"}


class LayoutError(ValueError):
    """The requested layout cannot be planned; the message says why and where."""


@dataclass(frozen=True)
class Size:
    kind: str  # FIXED / PERCENT / REST
    value: Fraction  # bytes for FIXED, percent (0 < p <= 100) for PERCENT, 0 for REST
    text: str  # as the user wrote it, for messages

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class DesiredPartition:
    format: str  # as written; resolved by the strategy (T-042)
    label: str | None
    size: Size


@dataclass(frozen=True)
class DesiredLayout:
    partitions: tuple[DesiredPartition, ...]


def parse_size(text: Any) -> Size:
    """`200G`, `1.5TiB`, `20%`, `rest`. Raises LayoutError with the reason."""
    if type(text) is not str:
        raise LayoutError(f'size must be a string such as "200G", "20%" or "rest", got {text!r}')
    s = text.strip()
    if s.casefold() in _REST:
        return Size(REST, Fraction(0), text)
    if m := _PERCENT.fullmatch(s):
        pct = Fraction(Decimal(m["num"]))
        if not 0 < pct <= 100:
            raise LayoutError(f"size {text!r}: a percentage must be above 0 and at most 100")
        return Size(PERCENT, pct, text)
    m = _FIXED.fullmatch(s)
    if not m:
        raise LayoutError(
            f"size {text!r} is not understood: expected a number with a unit (200G, 1.5TiB, 512MiB), "
            "a percentage (20%) or rest"
        )
    unit = (m["unit"] or "").casefold()
    if not unit and not m["b"]:
        # diskutil would take this as bytes; in a layout file it is almost never meant so.
        raise LayoutError(f"size {text!r} has no unit: write {s}B for bytes, or {s}G, {s}GiB, ...")
    num = Fraction(Decimal(m["num"]))
    if num == 0:
        raise LayoutError(f"size {text!r} is zero: to take all that is left, write rest")
    nbytes = num * _UNITS[unit]
    if nbytes.denominator != 1:
        raise LayoutError(f"size {text!r} is not a whole number of bytes")
    return Size(FIXED, nbytes, text)


def parse_layout(doc: Any) -> DesiredLayout:
    """A layout document as loaded from JSON. Unknown keys stop, as in probe (§S9):
    a misspelt "lable" must not be silently dropped."""
    if type(doc) is not dict:
        raise LayoutError('layout must be an object with a "partitions" list')
    _check_keys(doc, _DOC_KEYS, "layout")
    items = doc.get("partitions")
    if type(items) is not list or not items:
        raise LayoutError('layout: "partitions" must be a non-empty list')
    parts = tuple(_parse_partition(item, i) for i, item in enumerate(items, 1))
    layout = DesiredLayout(parts)
    _check_static(layout)
    return layout


def _parse_partition(item: Any, n: int) -> DesiredPartition:
    where = f"partition {n}"
    if type(item) is not dict:
        raise LayoutError(f"{where}: must be an object with format, size and optionally label")
    _check_keys(item, _PART_KEYS, where)
    for key in ("format", "size"):
        if key not in item:
            raise LayoutError(f"{where}: missing {key!r}")
    fmt = item["format"]
    if type(fmt) is not str or not fmt.strip():
        raise LayoutError(f"{where}: format must be a non-empty string")
    label = item.get("label")
    if label is not None:
        if type(label) is not str or not label:
            raise LayoutError(f"{where}: label must be a non-empty string, or left out")
        if _CONTROL.search(label):
            # It would not survive a preview line or a journal record intact (§S6).
            raise LayoutError(f"{where}: label {label!r} contains a control character")
    try:
        size = parse_size(item["size"])
    except LayoutError as e:
        raise LayoutError(f"{where}: {e}") from None
    return DesiredPartition(fmt, label, size)


def _check_keys(obj: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(obj) - allowed
    if unknown:
        raise LayoutError(f"{where}: unknown keys {sorted(unknown)}, expected only {sorted(allowed)}")


def _check_static(layout: DesiredLayout) -> None:
    """What is wrong whatever the capacity turns out to be."""
    rests = [n for n, p in enumerate(layout.partitions, 1) if p.size.kind == REST]
    if len(rests) > 1:
        raise LayoutError(f"rest may appear only once, found in partitions {rests}")
    pct = sum((p.size.value for p in layout.partitions if p.size.kind == PERCENT), Fraction(0))
    if pct > 100:
        raise LayoutError(f"percentages add up to {_pct(pct)}, more than 100%")
    if pct == 100 and len(layout.partitions) > sum(p.size.kind == PERCENT for p in layout.partitions):
        raise LayoutError("percentages add up to 100%, leaving nothing for the other partitions")


def resolve(layout: DesiredLayout, capacity: int) -> tuple[int, ...]:
    """Bytes per partition out of CAPACITY bytes: percentages of it rounded down, rest
    what is left. Raises LayoutError when the request does not fit. No alignment and
    no diskutil overhead here — that is T-041's prediction, which calls this with the
    capacity it predicts."""
    if capacity <= 0:
        raise LayoutError(f"no capacity to lay out ({capacity} bytes)")
    sizes: list[int | None] = []
    for p in layout.partitions:
        if p.size.kind == FIXED:
            sizes.append(int(p.size.value))
        elif p.size.kind == PERCENT:
            sizes.append(int(capacity * p.size.value / 100))  # Fraction: exact, then floor
        else:
            sizes.append(None)
    for n, (p, b) in enumerate(zip(layout.partitions, sizes), 1):
        if b == 0:
            raise LayoutError(f"partition {n}: {p.size} of {human(capacity)} is less than one byte")
    taken = sum(b for b in sizes if b is not None)
    left = capacity - taken
    if None in sizes:
        n = sizes.index(None) + 1
        if left <= 0:
            short = f"short by {human(-left)}" if left < 0 else "nothing is left"
            raise LayoutError(
                f"partition {n}: rest has no room — the other partitions take {human(taken)} "
                f"of {human(capacity)}, {short}"
            )
        sizes[n - 1] = left
    elif left < 0:
        raise LayoutError(
            f"partitions take {human(taken)}, more than the {human(capacity)} available by {human(-left)}"
        )
    return tuple(b for b in sizes if b is not None)


def human(nbytes: int) -> str:
    """diskutil-style base-ten size with the exact count: "2.0 GB (2000000000 bytes)"."""
    for unit, mult in (("EB", 10**18), ("PB", 10**15), ("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if nbytes >= mult:
            return f"{nbytes / mult:.1f} {unit} ({nbytes} bytes)"
    return f"{nbytes} bytes"


def _pct(p: Fraction) -> str:
    return f"{float(p):g}%"
