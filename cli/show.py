"""Text and JSON rendering of the partition map (T-030): a proportional bar, a table of
partitions and free space, totals, and what was computed rather than read (T-005, K7).

The bar gives every partition at least one cell, so a 200 MB EFI on a 512 GB disk is
still seen. Free extents under 1 MiB are alignment slack: summed in the totals, not
drawn or listed. The model keeps every one of them (D10), and so does --json.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from core.model import gpt_types
from core.model.apfs import ApfsContainer, ApfsVolume
from core.model.layout import GPT, Disk, FreeSpace, Partition, Unpartitioned
from core.model.volume import VolumeInfo
from core.probe.show import Access, DiskMap, Failed
from core.probe.snapshot import GPT_BASIS, MBR_BASIS, MBR_TYPE_BASIS

FORMAT = 1
MIN_WIDTH = 60
SLACK = 1 << 20  # bytes: a free extent below this is alignment slack
SYMBOLS = "123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
MORE = "+"  # partitions beyond SYMBOLS
FREE = "·"
WHOLE_FS = "="
KINDS = {"external": "external disk", "disk-image": "disk image"}
NBSP = "\u00a0"

# Table columns: symbol, device, start LBA, size, then free text to the right edge.
_COLS = (3, 11, 12, 9)
_TEXT = sum(_COLS) + 2 + 1 + 1 + 2  # where the free-text column starts

NOTE_COMPUTED = (
    "Usable LBA ranges are computed, not read: they assume the standard 128-entry GPT "
    "that diskutil writes. Reading the GPT headers takes root (sudo gpt -r show /dev/diskN); "
    "diskmap reads only what needs no root — ioreg and diskutil."
)
NOTE_MBR = (
    "MBR type bytes are derived from the kernel's names for them, not read from the table "
    "(reading sector 0 takes root); an MBR's usable range is LBA 1 to the end of the disk or 2^32-1."
)
NOTE_SLACK = "Free extents under 1 MiB (alignment slack) are counted in the totals, not listed; --json lists them."
NOTE_UNCONFIRMED = (
    "'?': diskutil names this file system after the partition type but finds no label, UUID "
    "or mount — it may be wiped, or be an unlabelled file system. Assume data may be there."
)
NOTE_NO_BAR = "Too many partitions to draw a bar at this width."


def render(m: DiskMap, width: int = 80) -> str:
    width = max(width, MIN_WIDTH)
    notes: list[str] = []
    blocks = []
    for d in m.disks:
        access = _access(m.access.get(d.bsd_name), width)
        if isinstance(d, Failed):
            blocks.append("\n".join([_wrap(f"{d.bsd_name} — cannot map: {d.reason}", width), *access]))
        elif isinstance(d, Unpartitioned):
            blocks.append(_unpartitioned(d, access, width))
        else:
            blocks.append(_disk(d, m.snapshots, access, width, notes))
    if not m.disks:
        blocks.append("No whole disks to show.")
    if m.hidden:
        blocks.append(_wrap(_hidden(m), width))
    out = "\n\n".join(blocks)
    if notes:
        out += "\n\n" + "\n".join(_wrap(n, width) for n in notes)
    # Sizes carry a no-break space so that wrapping never splits "1.4 MB".
    return "\n".join(ln.rstrip() for ln in out.replace(NBSP, " ").splitlines())


def _note(notes: list[str], note: str) -> None:
    if note not in notes:
        notes.append(note)


# --- a partitioned disk --------------------------------------------------------


def _disk(d: Disk, snaps: Mapping[str, tuple[str, ...]], access: list[str], width: int, notes: list[str]) -> str:
    _note(notes, NOTE_COMPUTED if d.scheme == GPT else NOTE_MBR)
    lines = [
        _wrap(f"{d.bsd_name} — {size(d.size)}, {d.scheme}, {d.block_size}-byte blocks", width),
        _wrap(f"{d.size} bytes; usable LBA {d.first_usable_lba}..{d.last_usable_lba} (computed)", width, "  "),
        *access,
    ]

    symbols = {p.bsd_name: SYMBOLS[i] if i < len(SYMBOLS) else MORE for i, p in enumerate(d.partitions)}
    shown = [e for e in d.extents() if isinstance(e, Partition) or e.blocks * d.block_size >= SLACK]
    slack = [e for e in d.free if e.blocks * d.block_size < SLACK]
    if shown:
        cells = allocate([e.blocks for e in shown], width - 2)
        if cells is None:
            _note(notes, NOTE_NO_BAR)
        else:
            bar = "".join((symbols[e.bsd_name] if isinstance(e, Partition) else FREE) * n for e, n in zip(shown, cells))
            lines.append(f"[{bar}]")

    lines.append(_row("#", "DEVICE", "START LBA", "SIZE", "TYPE / FILE SYSTEM", width=width))
    for e in shown:
        if isinstance(e, FreeSpace):
            lines.append(_row(FREE, "free", str(e.start_lba), size(e.blocks * d.block_size), width=width))
            continue
        lines.append(
            _row(
                symbols[e.bsd_name], e.bsd_name, str(e.start_lba), size(e.blocks * d.block_size), *_type(e), width=width
            )
        )
        if e.apfs is not None:
            lines.append(_sub(_container(e.apfs, e.bsd_name), width))
            for v in e.apfs.volumes:
                lines.append(_sub(f"└ {v.bsd_name}  {_apfs_volume(v, snaps, notes)}", width, hang=4))
        elif e.volume is not None:
            lines.append(_sub(_fs(e.volume, (), notes), width))

    used = sum(p.blocks for p in d.partitions) * d.block_size
    big = [f for f in d.free if f not in slack]
    total = f"{_count(len(d.partitions), 'partition')}, {size(used)} · "
    total += f"free {size(_bytes(big, d))} in {_count(len(big), 'extent')}" if big else "no free space"
    if slack:
        _note(notes, NOTE_SLACK)
        total += f" · alignment slack {size(_bytes(slack, d))} in {_count(len(slack), 'gap')}"
    lines.append(_wrap(total, width))
    return "\n".join(lines)


def _type(p: Partition) -> list[str]:
    """Name and GUID, stacked (T-025); an unknown GUID as is, plus diskutil's name if it has one."""
    if p.mbr_type is not None:
        return [f"0x{p.mbr_type:02X} {p.type_name}"]
    if gpt_types.known(p.type) is not None:
        return [p.type_name, p.type]
    if p.type_name == p.type:
        return [p.type]
    return [p.type, f"{p.type_name} (diskutil)"]


def _container(c: ApfsContainer, store: str) -> str:
    text = f"APFS container {c.reference} · {size(c.capacity_free)} free of {size(c.capacity_ceiling)}"
    others = [s for s in c.store_names if s != store]
    if others:
        text += f" · also on {', '.join(others)}"
    return text


def _apfs_volume(v: ApfsVolume, snaps: Mapping[str, tuple[str, ...]], notes: list[str]) -> str:
    text = f'"{v.name}"'
    if v.roles:
        text += f" [{', '.join(v.roles)}]"
    parts = [text]
    if v.volume is not None:
        parts.append(_mount(v.volume, snaps.get(v.bsd_name, ())))
    parts.append(f"{size(v.capacity_in_use)} used")
    if v.locked:
        parts.append("locked")
    return " · ".join(parts)


def _fs(v: VolumeInfo, snaps: tuple[str, ...], notes: list[str]) -> str:
    if v.filesystem is None:
        return "no file system diskutil recognizes"
    name = v.filesystem_name or v.filesystem
    if not v.confirmed:
        _note(notes, NOTE_UNCONFIRMED)
        return f"{name}?"
    parts = [name if v.label is None else f'{name} "{v.label}"', _mount(v, snaps)]
    if v.space is not None:
        parts.append(f"{size(v.space.used)} of {size(v.space.capacity)} used")
    return " · ".join(parts)


def _mount(v: VolumeInfo, snaps: tuple[str, ...]) -> str:
    if v.mount_point is not None:
        return f"at {v.mount_point}" + ("" if v.writable else ", read-only")
    if snaps:
        return f"not mounted; its snapshot at {', '.join(snaps)}"
    return "not mounted"


# --- the guard's verdict (T-031, K6) -------------------------------------------


def _access(a: Access | None, width: int) -> list[str]:
    if a is None:
        # Every listed disk is judged; a disk shown without a verdict is not one diskmap may touch.
        return [_wrap("READ-ONLY — not judged by the guard", width, "  ")]
    if not a.available:
        return [_wrap("READ-ONLY — protected:", width, "  ")] + [
            _wrap(f"{r.code}: {r.detail}", width, "    ") for r in a.reasons
        ]
    lines = [_wrap(f"available: {KINDS.get(a.kind or '', a.kind or '?')}", width, "  ")]
    lines += [_wrap(f"warning: {w}", width, "  ") for w in a.warnings]
    return lines


def _hidden(m: DiskMap) -> str:
    parts = []
    for name in m.hidden:
        a = m.access[name]
        codes = ", ".join(dict.fromkeys(r.code for r in a.reasons))
        parts.append(f"{name} ({codes})")
    return f"Hidden by --external, protected: {'; '.join(parts)}. Without --external they are shown read-only."


# --- a disk without a partition table ------------------------------------------


def _unpartitioned(d: Unpartitioned, access: list[str], width: int) -> str:
    has_fs = d.volume is not None and d.volume.filesystem is not None
    lines = [
        _wrap(f"{d.bsd_name} — {size(d.size)}, no partition table, {d.block_size}-byte blocks", width),
        _wrap(f"{d.size} bytes", width, "  "),
        *access,
        "[" + (WHOLE_FS if has_fs or d.apfs is not None else FREE) * (width - 2) + "]",
    ]
    if d.apfs is not None:  # K20
        lines.append(_sub(f"whole disk: {_container(d.apfs, d.bsd_name)}", width))
        for v in d.apfs.volumes:
            lines.append(_sub(f"└ {v.bsd_name}  {_apfs_volume(v, {}, [])}", width, hang=4))
    elif has_fs:
        assert d.volume is not None
        lines.append(_wrap(f"whole disk: {_fs(d.volume, (), [])}", width, indent="  "))
    else:
        lines.append(_wrap("no file system diskutil recognizes on the whole disk", width, indent="  "))
    return "\n".join(lines)


# --- layout helpers ------------------------------------------------------------


def allocate(weights: list[int], cells: int) -> list[int] | None:
    """Split CELLS between WEIGHTS proportionally, at least one cell each; None if they
    do not fit. An item whose share is under one cell gets exactly one, and the room
    left is shared again among the others (largest remainder). Terminates with at least
    one item sharing: the shares of the rest add up to the room, which is never smaller
    than the number of items in it."""
    n = len(weights)
    if n == 0 or n > cells:
        return None
    ones: set[int] = set()
    while True:
        rest = [i for i in range(n) if i not in ones]
        room = cells - len(ones)
        total = sum(weights[i] for i in rest)
        small = {i for i in rest if weights[i] * room < total}
        if not small:
            break
        ones |= small
    out = [1] * n
    shares = {i: weights[i] * room / total for i in rest}
    for i in rest:
        out[i] = int(shares[i])
    left = room - sum(out[i] for i in rest)
    for i in sorted(rest, key=lambda i: shares[i] - out[i], reverse=True)[:left]:
        out[i] += 1
    return out


def size(n: int) -> str:
    """Decimal units, as diskutil prints them."""
    if n < 1000:
        return f"{n}{NBSP}B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 999.95:
            break
    return f"{value:.1f}{NBSP}{unit}"


def _bytes(extents: list[FreeSpace], d: Disk) -> int:
    return sum(e.blocks for e in extents) * d.block_size


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _row(sym: str, dev: str, start: str, sz: str, *texts: str, width: int) -> str:
    """One table row; each of TEXTS starts a line of its own in the last column."""
    left = f"{sym:>{_COLS[0]}}  {dev:<{_COLS[1]}} {start:>{_COLS[2]}} {sz:>{_COLS[3]}}  "
    body = [b for t in texts for b in textwrap.wrap(t, width - _TEXT, break_on_hyphens=False)] or [""]
    return "\n".join([left + body[0]] + [" " * _TEXT + b for b in body[1:]])


def _sub(text: str, width: int, hang: int = 2) -> str:
    """A line under a partition row, from the DEVICE column to the right edge."""
    indent = " " * (_COLS[0] + 2)
    return "\n".join(
        textwrap.wrap(text, width, initial_indent=indent, subsequent_indent=indent + " " * hang, break_on_hyphens=False)
    )


def _wrap(text: str, width: int, indent: str = "") -> str:
    return "\n".join(
        textwrap.wrap(text, width, initial_indent=indent, subsequent_indent=indent + "  ", break_on_hyphens=False)
    )


# --- JSON ----------------------------------------------------------------------


def to_json(m: DiskMap) -> str:
    disks = []
    for d in m.disks:
        out = _disk_json(d, m.snapshots)
        a = m.access.get(d.bsd_name)
        out["access"] = _access_json(a) if a is not None else None
        disks.append(out)
    hidden = [{"device": n, "access": _access_json(m.access[n])} for n in m.hidden]
    doc = {"format": FORMAT, "disks": disks, "hidden": hidden}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _access_json(a: Access) -> dict[str, Any]:
    return {
        "available": a.available,
        "kind": a.kind,
        "reasons": [asdict(r) for r in a.reasons],
        "warnings": list(a.warnings),
    }


def _disk_json(d: Disk | Unpartitioned | Failed, snaps: Mapping[str, tuple[str, ...]]) -> dict[str, Any]:
    if isinstance(d, Failed):
        return {"device": d.bsd_name, "error": d.reason}
    if isinstance(d, Unpartitioned):
        return {
            "device": d.bsd_name,
            "size": d.size,
            "block_size": d.block_size,
            "scheme": None,
            "volume": asdict(d.volume) if d.volume is not None else None,
            "apfs": asdict(d.apfs) if d.apfs is not None else None,
        }
    return {
        "device": d.bsd_name,
        "size": d.size,
        "block_size": d.block_size,
        "scheme": d.scheme,
        "usable": {
            "first_lba": d.first_usable_lba,
            "last_lba": d.last_usable_lba,
            "computed": GPT_BASIS if d.scheme == GPT else MBR_BASIS,
        },
        "extents": [_extent_json(e, d, snaps) for e in d.extents()],
    }


def _extent_json(e: Partition | FreeSpace, d: Disk, snaps: Mapping[str, tuple[str, ...]]) -> dict[str, Any]:
    if isinstance(e, FreeSpace):
        return {"kind": "free", "start_lba": e.start_lba, "blocks": e.blocks, "bytes": e.blocks * d.block_size}
    out: dict[str, Any] = {
        "kind": "partition",
        "device": e.bsd_name,
        "partition_id": e.partition_id,
        "start_lba": e.start_lba,
        "blocks": e.blocks,
        "bytes": e.blocks * d.block_size,
        "type": e.type,
        "type_name": e.type_name,
        "volume_name": e.volume_name,
    }
    if d.scheme == GPT:
        out["gpt_attributes"] = e.gpt_attributes
    else:
        out["mbr_type"] = {"value": f"0x{e.mbr_type:02X}", "computed": MBR_TYPE_BASIS}
    out["volume"] = asdict(e.volume) if e.volume is not None else None
    if e.apfs is not None:
        c = asdict(e.apfs)
        for v in c["volumes"]:
            v["snapshot_mount_points"] = list(snaps.get(v["bsd_name"], ()))
        out["apfs"] = c
    return out
