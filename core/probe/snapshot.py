"""Normalized partition-map snapshot for diff tests (T-003, K7).

Built from ioreg + `diskutil list -plist` only — the same with and without root;
`gpt` is never called. Two deployments of the same layout give byte-identical JSON:
everything that differs between attaches is left out —

  device identifiers (diskN), GPT disk and partition GUIDs, volume UUIDs, mount
  points and mount state, IORegistry IDs and names (the latter unreliable, K7).

Values that are not read but derived live under a "computed" key of the object
they belong to, together with the assumption they rest on:

  disk.computed       — usable LBA range: GPT assumes the 128 × 128-byte entry
                        table diskutil creates; MBR is bounded by 32-bit LBAs.
  partition.computed  — MBR type byte, recovered from the kernel's own name table.

A disk without a partition table has scheme null, no "computed" and no partitions:
there is no map to back up (T-030, D17).

Usage: python -m core.probe.snapshot diskN   (prints JSON, read-only)
"""

from __future__ import annotations

import json
import sys
from typing import Any

from core.model.layout import GPT, Disk, Partition, Unpartitioned
from core.probe.diskutil import read_list
from core.probe.errors import ProbeError
from core.probe.ioreg import read_iomedia
from core.probe.layout import build_layout

FORMAT = 1

GPT_BASIS = "assumed standard GPT: 128 entries x 128 bytes at both ends (K7)"
MBR_BASIS = "MBR: LBA 0 holds the table; 32-bit LBA fields"
MBR_TYPE_BASIS = "IOFDiskPartitionScheme Content Table, name -> byte (D8)"


def take(bsd_name: str) -> dict[str, Any]:
    """Probe whole disk BSD_NAME and return its snapshot. Read-only."""
    return build(read_iomedia(), read_list(bsd_name), bsd_name)


def build(ioreg_roots: Any, diskutil_plist: Any, bsd_name: str) -> dict[str, Any]:
    """Pure part of take(): raw tool plists in, snapshot dict out."""
    return of_layout(build_layout(ioreg_roots, diskutil_plist, bsd_name))


def of_layout(disk: Disk | Unpartitioned) -> dict[str, Any]:
    """The snapshot is the layout minus identity; free space is not stored — it follows."""
    if isinstance(disk, Unpartitioned):
        return {
            "format": FORMAT,
            "disk": {"scheme": None, "size": disk.size, "block_size": disk.block_size},
            "partitions": [],
        }
    return {
        "format": FORMAT,
        "disk": {
            "scheme": disk.scheme,
            "size": disk.size,
            "block_size": disk.block_size,
            "computed": {
                "first_usable_lba": disk.first_usable_lba,
                "last_usable_lba": disk.last_usable_lba,
                "basis": GPT_BASIS if disk.scheme == GPT else MBR_BASIS,
            },
        },
        "partitions": [_partition(disk, p) for p in disk.partitions],
    }


def to_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _partition(disk: Disk, p: Partition) -> dict[str, Any]:
    out: dict[str, Any] = {
        "partition_id": p.partition_id,
        "base": p.start_lba * disk.block_size,
        "size": p.blocks * disk.block_size,
        "type": p.type,
        "type_name": p.type_name,
        "volume_name": p.volume_name,
    }
    if disk.scheme == GPT:
        out["gpt_attributes"] = p.gpt_attributes
    else:
        assert p.mbr_type is not None
        out["computed"] = {"mbr_type": f"0x{p.mbr_type:02X}", "basis": MBR_TYPE_BASIS}
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m core.probe.snapshot diskN", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(to_json(take(argv[0])))
    except ProbeError as e:
        print(f"snapshot: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
