"""Partition-map backup: the raw blocks a disk's partition table lives in (T-012, K11).

A backup is a folder of its own holding two files, written by `dd` steps of a plan (§S6):

  head.bin — blocks [0, H): the MBR or protective MBR, the GPT header, the entries
  tail.bin — the last T blocks: the backup GPT's entries and its header

H and T are those of the standard GPT diskutil writes (K7) for the disk's block size:
H = 2 + 16 KiB / block size, T = 1 + 16 KiB / block size — 34 and 33 at 512 bytes. An MBR
disk gets the same two ranges: block 0 is its table and the rest is whatever lay there,
kept so that a GPT written over the disk since can be taken back byte for byte.

The files describe themselves: the block size follows from their lengths, the scheme from
block 0 (a single protective 0xEE entry: GPT), the disk's size in blocks from the GPT
header — an MBR does not record it. parse() refuses whatever it does not recognise in
full (§S9): a GPT other than the standard one, a hybrid MBR, an extended partition (its
EBR chain is not read), a CRC that does not match, primary and backup tables that differ.

Pure: bytes in, MapDump out. Reading the folder and comparing with the kernel's map is
core.probe.mapbackup.
"""

from __future__ import annotations

import struct
import uuid
import zlib
from dataclasses import dataclass

from core.model.layout import GPT, MBR

HEAD_FILE = "head.bin"
TAIL_FILE = "tail.bin"
ENTRIES = 128
ENTRY_SIZE = 128
TABLE = ENTRIES * ENTRY_SIZE  # 16 KiB at either end
BLOCK_SIZES = (512, 4096)

MBR_ENTRIES_AT = 446
MBR_SIGNATURE = b"\x55\xaa"
PROTECTIVE = 0xEE
EXTENDED = frozenset({0x05, 0x0F, 0x85})  # an EBR chain follows; not read

GPT_SIGNATURE = b"EFI PART"
GPT_REVISION = 0x00010000
GPT_HEADER = struct.Struct("<8sIIIIQQQQ16sQIII")  # UEFI 2.x, 92 bytes
GPT_ENTRY = struct.Struct("<16s16sQQQ72s")


class BadBackup(Exception):
    """The files are not a backup diskmap can restore; the message says why."""


def head_blocks(block_size: int) -> int:
    return 2 + TABLE // block_size


def tail_blocks(block_size: int) -> int:
    return 1 + TABLE // block_size


@dataclass(frozen=True)
class Entry:
    index: int  # GPT entry / MBR slot, 1-based: the kernel's Partition ID
    start_lba: int
    blocks: int
    type: str  # GPT: type GUID; MBR: the type byte, "0x07"
    guid: str | None  # GPT partition GUID
    attributes: int | None  # GPT attributes

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.blocks - 1


@dataclass(frozen=True)
class MapDump:
    scheme: str  # GPT or MBR (core.model.layout)
    block_size: int
    blocks: int | None  # the disk's size in blocks; None: an MBR does not record it
    disk_guid: str | None  # GPT only
    entries: tuple[Entry, ...]  # the used ones, by index
    head: bytes
    tail: bytes


def parse(head: bytes, tail: bytes) -> MapDump:
    bs = (len(head) - TABLE) // 2
    if bs not in BLOCK_SIZES or len(head) != head_blocks(bs) * bs:
        expected = " or ".join(f"{head_blocks(b) * b}" for b in BLOCK_SIZES)
        raise BadBackup(f"{HEAD_FILE}: {len(head)} bytes, expected {expected} (the first blocks of a disk)")
    if len(tail) != tail_blocks(bs) * bs:
        raise BadBackup(f"{TAIL_FILE}: {len(tail)} bytes, expected {tail_blocks(bs) * bs} for {bs}-byte blocks")
    slots = _mbr_slots(head[:bs])
    if any(s[1] == PROTECTIVE for s in slots):
        if [s[0] for s in slots] != [1] or slots[0][2] != 1:
            raise BadBackup(
                "block 0: a protective 0xEE entry next to others, or not in slot 1 from LBA 1 — a hybrid MBR, not read"
            )
        return _gpt(head, tail, bs)
    entries = []
    for index, kind, start, count in slots:
        if kind in EXTENDED:
            raise BadBackup(f"MBR slot {index}: extended partition (type 0x{kind:02X}) — its EBR chain is not read")
        if count == 0 or start == 0:
            raise BadBackup(f"MBR slot {index}: starts at LBA {start}, {count} blocks — not a partition")
        entries.append(Entry(index, start, count, f"0x{kind:02X}", None, None))
    return MapDump(MBR, bs, None, None, tuple(entries), head, tail)


def _mbr_slots(block0: bytes) -> list[tuple[int, int, int, int]]:
    """(slot, type, start LBA, blocks) of every used slot of block 0."""
    if block0[510:512] != MBR_SIGNATURE:
        raise BadBackup("block 0: no 55 AA signature — neither an MBR nor a protective MBR")
    out = []
    for i in range(4):
        raw = block0[MBR_ENTRIES_AT + 16 * i : MBR_ENTRIES_AT + 16 * (i + 1)]
        status, kind = raw[0], raw[4]
        start, count = struct.unpack_from("<II", raw, 8)
        if kind == 0:
            if any(raw):
                raise BadBackup(f"MBR slot {i + 1}: type 0 with other bytes set")
            continue
        if status not in (0x00, 0x80):
            raise BadBackup(f"MBR slot {i + 1}: status byte 0x{status:02X}")
        out.append((i + 1, kind, start, count))
    return out


@dataclass(frozen=True)
class _Header:
    my_lba: int
    alt_lba: int
    first_usable: int
    last_usable: int
    disk_guid: str
    entries_lba: int
    entries_crc: int


def _header(block: bytes, what: str) -> _Header:
    sig, rev, size, crc, reserved, my, alt, first, last, guid, at, n, esz, ecrc = GPT_HEADER.unpack_from(block)
    if sig != GPT_SIGNATURE:
        raise BadBackup(f"{what}: no 'EFI PART' signature")
    if rev != GPT_REVISION or size != GPT_HEADER.size or reserved:
        raise BadBackup(f"{what}: revision 0x{rev:08X}, header size {size} — not the GPT diskutil writes")
    if zlib.crc32(block[:16] + b"\0\0\0\0" + block[20:size]) != crc:
        raise BadBackup(f"{what}: header CRC does not match")
    if any(block[size:]):
        raise BadBackup(f"{what}: the rest of the block after the header is not zero")
    if (n, esz) != (ENTRIES, ENTRY_SIZE):
        raise BadBackup(f"{what}: {n} entries of {esz} bytes — not the standard 128 x 128 (K7)")
    return _Header(my, alt, first, last, _guid(guid), at, ecrc)


def _gpt(head: bytes, tail: bytes, bs: int) -> MapDump:
    h, t = head_blocks(bs), tail_blocks(bs)
    primary = _header(head[bs : 2 * bs], "primary GPT header")
    blocks = primary.alt_lba + 1
    if (primary.my_lba, primary.entries_lba, primary.first_usable, primary.last_usable) != (1, 2, h, blocks - t - 1):
        raise BadBackup(
            f"primary GPT header: at LBA {primary.my_lba}, entries at {primary.entries_lba}, usable "
            f"{primary.first_usable}..{primary.last_usable} — not the standard layout of a "
            f"{blocks}-block disk ({h}..{blocks - t - 1}, K7)"
        )
    backup = _header(tail[TABLE:], "backup GPT header")
    if (backup.my_lba, backup.alt_lba, backup.entries_lba) != (blocks - 1, 1, blocks - t):
        raise BadBackup(
            f"backup GPT header: at LBA {backup.my_lba}, entries at {backup.entries_lba}, primary at "
            f"{backup.alt_lba} — expected {blocks - 1}, {blocks - t}, 1"
        )
    if (backup.first_usable, backup.last_usable, backup.disk_guid) != (
        primary.first_usable,
        primary.last_usable,
        primary.disk_guid,
    ):
        raise BadBackup("the primary and backup GPT headers describe different disks")
    table = head[2 * bs : 2 * bs + TABLE]
    if zlib.crc32(table) != primary.entries_crc:
        raise BadBackup("primary GPT entries: CRC does not match")
    if tail[:TABLE] != table or backup.entries_crc != primary.entries_crc:
        raise BadBackup("the primary and backup GPT entry tables differ")

    entries = []
    for i in range(ENTRIES):
        raw = table[i * ENTRY_SIZE : (i + 1) * ENTRY_SIZE]
        kind, unique, first, last, attributes, _name = GPT_ENTRY.unpack(raw)
        if not any(kind):
            if any(raw):
                raise BadBackup(f"GPT entry {i + 1}: no type, yet other bytes set")
            continue
        if not any(unique):
            raise BadBackup(f"GPT entry {i + 1}: no partition GUID")
        if not primary.first_usable <= first <= last <= primary.last_usable:
            raise BadBackup(
                f"GPT entry {i + 1}: LBA {first}..{last} outside the usable area "
                f"{primary.first_usable}..{primary.last_usable}"
            )
        entries.append(Entry(i + 1, first, last - first + 1, _guid(kind), _guid(unique), attributes))
    return MapDump(GPT, bs, blocks, primary.disk_guid, tuple(entries), head, tail)


def _guid(raw: bytes) -> str:
    return str(uuid.UUID(bytes_le=raw)).upper()
