"""A map backup in its folder, and whether it is the map the kernel shows (T-012, K11).

read_backup() reads head.bin and tail.bin and parses them (core.model.mapdump).
differences() compares a backup with the partition map the kernel publishes (ioreg, K7):
the scheme, the block size, every used entry by index — start, size, type, and on GPT
its partition GUID and attributes — and the GPT disk GUID and size. It is the explicit
reconciliation K7 asks for: the backup is read with root on a real disk, the map
without, and the two must agree before anything writes.

These are the checks behind MapBackedUp and MapIs (core.planner.plan). The executor
(T-050) is to call them; until then the rig tests do. Read-only.
"""

from __future__ import annotations

import os

from core.model.layout import GPT, MBR
from core.model.mapdump import HEAD_FILE, TAIL_FILE, BadBackup, MapDump, parse
from core.probe import ioreg
from core.probe.ioreg import IoDisk

SCHEME = {ioreg.GPT: GPT, ioreg.MBR: MBR}
LIMIT = 1 << 20  # a backup is tens of KiB; anything near this is not one


def read_backup(folder: str) -> MapDump:
    """The backup in FOLDER, parsed; BadBackup if it is missing, unreadable or not one."""
    parts = []
    for name in (HEAD_FILE, TAIL_FILE):
        path = os.path.join(folder, name)
        try:
            with open(path, "rb") as f:
                data = f.read(LIMIT + 1)
        except OSError as e:
            raise BadBackup(f"{path}: {e.strerror or e}") from None
        if len(data) > LIMIT:
            raise BadBackup(f"{path}: larger than {LIMIT} bytes — not a map backup")
        parts.append(data)
    return parse(parts[0], parts[1])


def fits(dump: MapDump, io: IoDisk) -> list[str]:
    """Why DUMP cannot be a backup of a disk shaped like IO — its block size and size."""
    blocks = io.size // io.block_size
    if dump.block_size != io.block_size:
        return [f"the backup has {dump.block_size}-byte blocks, {io.bsd_name} {io.block_size}-byte ones"]
    if dump.blocks is not None and dump.blocks != blocks:
        return [f"the backup is of a disk of {dump.blocks} blocks, {io.bsd_name} has {blocks}"]
    past = [e.index for e in dump.entries if e.end_lba >= blocks]
    if past:
        return [f"entries {past} of the backup end past the last block of {io.bsd_name} ({blocks - 1})"]
    return []


def differences(dump: MapDump, io: IoDisk) -> list[str]:
    """Where the kernel's map of IO differs from DUMP; empty: it is exactly that map."""
    out = fits(dump, io)
    if out:
        return out
    scheme = SCHEME.get(io.scheme)
    if scheme != dump.scheme:
        return [f"{io.bsd_name} has {scheme or 'no partition table'}, the backup {dump.scheme}"]
    if scheme == GPT and io.scheme_uuid != dump.disk_guid:
        out.append(f"disk GUID {io.scheme_uuid}, the backup's {dump.disk_guid}")
    bs = io.block_size
    have = {p.partition_id: p for p in io.partitions}
    want = {e.index: e for e in dump.entries}
    for index in sorted(have.keys() - want.keys()):
        out.append(f"entry {index} ({have[index].bsd_name}) is not in the backup")
    for index in sorted(want.keys() - have.keys()):
        out.append(f"entry {index} of the backup is not on {io.bsd_name}")
    for index in sorted(have.keys() & want.keys()):
        p, e = have[index], want[index]
        if scheme == GPT:
            now = (p.base // bs, p.size // bs, p.content, p.uuid, p.gpt_attributes)
            was = (e.start_lba, e.blocks, e.type, e.guid, e.attributes)
        else:
            byte = (io.mbr_type_names or {}).get(p.content)
            now = (p.base // bs, p.size // bs, f"0x{byte:02X}" if byte is not None else p.content)
            was = (e.start_lba, e.blocks, e.type)
        if now != was:
            out.append(f"entry {index} ({p.bsd_name}): {_show(now)} now, {_show(was)} in the backup")
    return out


def _show(fields: tuple[object, ...]) -> str:
    return "LBA {} +{} blocks, type {}".format(*fields[:3]) + "".join(f", {f}" for f in fields[3:])
