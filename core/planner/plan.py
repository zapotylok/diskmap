"""ExecutionPlan: the only thing the executor will run (rules.md §S3; backlog §1).

Pure data. A step is an argv, never a shell string (§S4), with an explicit stdin
behaviour and no default for it (§S10, T-054). Assertions are declarations the
executor (T-050) checks; the planner only states them. None of them is optional:
every step that writes to a disk with a partition table carries MapBackedUp of the
backup its own plan made first (§S5, T-012, K11); a disk without a table has no map
to back up (D17) and carries LayoutIs(scheme None) instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.model.layout import GPT
from core.planner.predict import Predicted

# --- stdin (§S10) ---


@dataclass(frozen=True)
class Closed:
    """stdin is /dev/null; a step that asks anything not listed here fails instead of waiting.

    TOOL_ANSWERS: questions the tool prints and answers itself, stdin or not — measured, not
    hoped for: diskutil prints `Proceed with Format (y/n)?y` for UFSD_NTFS and goes on with
    stdin closed, piped `n` or a terminal (D27). Listed so the preview shows them (§S6:
    pasted into a terminal, the same question appears) and a run can tell them from a
    question nobody expected."""

    tool_answers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Answers:
    """Questions the step is expected to ask and what diskmap answers on its stdin, in
    order, shown in the preview."""

    pairs: tuple[tuple[str, str], ...]


Stdin = Closed | Answers


# --- assertions ---


@dataclass(frozen=True)
class DiskAvailable:
    """The guard, run now, still issues this whole disk (§S8, K6)."""

    disk: str


@dataclass(frozen=True)
class MapBackedUp:
    """FOLDER holds an intact map backup that fits DISK — its block size and size
    (§S5, T-012, K11; core.probe.mapbackup.read_backup and fits)."""

    disk: str
    folder: str


@dataclass(frozen=True)
class MapIs:
    """The partition map the kernel shows for DISK now is exactly the one in FOLDER:
    every entry, GUIDs included (core.probe.mapbackup.differences)."""

    disk: str
    folder: str


@dataclass(frozen=True)
class DiskUnmounted:
    """No volume of DISK is mounted — its partitions' nor the APFS volumes on them (T-011)."""

    disk: str


@dataclass(frozen=True)
class Extent:
    start_lba: int
    blocks: int
    type: str  # GPT type GUID / MBR type name


@dataclass(frozen=True)
class LayoutIs:
    """The disk's partitions are exactly these. scheme None: no partition table."""

    disk: str
    scheme: str | None
    partitions: tuple[Extent, ...]


@dataclass(frozen=True)
class SliceIs:
    """SLICE is the partition at this place with this type — the slice is named by
    where it is, and the name is checked, never trusted (slices renumber, D22)."""

    slice: str
    extent: Extent


@dataclass(frozen=True)
class Unmounted:
    slice: str


@dataclass(frozen=True)
class Mounted:
    """SLICE is mounted, whoever mounted it (K16)."""

    slice: str


@dataclass(frozen=True)
class VolumeIs:
    """diskutil info of SLICE says this file system (Type (Bundle)) and name."""

    slice: str
    bundle: str
    name: str | None


@dataclass(frozen=True)
class Probed:
    """DiskArbitration has probed SLICE again and names this file system (Type (Bundle))
    and name. Polled for a while: after the kernel re-reads the map, the re-probe comes
    a moment later (rig, 2026-10-17: the first diskutil info still said none) (T-053).
    BUNDLE None: any file system diskutil confirms (D13) — a trial mount does not
    presume who takes it (T-026)."""

    slice: str
    bundle: str | None
    name: str | None


@dataclass(frozen=True)
class MountedBy:
    """SLICE is mounted, and `mount` names BY as what mounted it: "fskit" for an FSKit
    module, else the mount type ("ufsd_ExtFS", "hfs") (T-053, T-026)."""

    slice: str
    by: str


@dataclass(frozen=True)
class ShrinkAllowed:
    """Resize limits of SLICE, asked now, allow SIZE bytes (T-044): files written since
    the plan was made may have raised the minimum."""

    slice: str
    apfs: bool  # asked of apfs resizeContainer
    size: int


@dataclass(frozen=True)
class GrowAllowed:
    """SLICE, asked now, still gives resize limits (HFS+ only while mounted) and is smaller than
    SIZE bytes (T-074). Not compared with MaximumSize, which says up to a MiB less than diskutil
    grows to (D48): the free space after it is the plan's, as the LayoutIs beside it says."""

    slice: str
    apfs: bool  # asked of apfs resizeContainer
    size: int


Assertion = (
    DiskAvailable
    | MapBackedUp
    | MapIs
    | DiskUnmounted
    | LayoutIs
    | SliceIs
    | Unmounted
    | Mounted
    | VolumeIs
    | Probed
    | MountedBy
    | ShrinkAllowed
    | GrowAllowed
)


@dataclass(frozen=True)
class Loss:
    """What a step destroys, as the preview names it (§S7, T-014): a volume by its name,
    not only a device."""

    device: str  # the partition, APFS volume or whole disk
    what: str  # 'APFS volume "Mac"', 'ExFAT "B"', 'Microsoft Basic Data, no file system recognized'
    used: int | None  # bytes in use; None: not known (not mounted, or nothing recognized)
    certain: bool  # False: diskutil cannot confirm what is there (D13) — assume data is
    # The volume's name, which `diskmap apply` asks to be typed before destroying it (T-062);
    # None: it has none (or nothing is recognized), and the device is typed instead.
    label: str | None = None


@dataclass(frozen=True)
class Step:
    intent: str  # human-readable, English (rules.md §12)
    command: tuple[str, ...]  # argv; argv[0] an absolute path
    needs_root: bool
    # Writes blocks of the device — a table, a file system. False: reads it, mounts or
    # unmounts, or touches only diskmap's own files. A writing step needs MapBackedUp.
    writes_disk: bool
    stdin: Stdin
    preflight: tuple[Assertion, ...]
    postflight: tuple[Assertion, ...]
    rollback: Step | None  # None: irreversible, marked so in the preview (§S7)
    destroys: tuple[Loss, ...]  # what loses its data (§S7, T-014); empty: nothing of the user's

    def __post_init__(self) -> None:
        if type(self.command) is not tuple or not self.command or not all(type(a) is str for a in self.command):
            raise TypeError(f"command must be a non-empty tuple of str: {self.command!r}")
        if not os.path.isabs(self.command[0]):
            raise ValueError(f"command must start with an absolute path: {self.command[0]!r}")
        if any("\0" in a for a in self.command):
            raise ValueError("an argument contains NUL")
        if not isinstance(self.stdin, Closed | Answers):
            raise TypeError(f"stdin must be Closed or Answers, got {self.stdin!r}")
        pairs = self.stdin.tool_answers if isinstance(self.stdin, Closed) else self.stdin.pairs
        if not all(type(q) is str and type(a) is str and q.strip() and "\n" not in q + a for q, a in pairs):
            raise ValueError(f"questions and answers must be single non-empty lines: {pairs!r}")


@dataclass(frozen=True)
class PlannedPartition:
    """One partition of the desired layout, as the plan makes it."""

    format: str  # as written
    label: str | None
    size_text: str  # as written: "20%", "rest"
    makes: str  # what is created, for the preview: "Mac OS Extended (Journaled)", "NTFS (mkntfs)"
    slice: str  # diskNsM right after the plan's partitionDisk
    predicted: Predicted


@dataclass(frozen=True)
class KeptPartition:
    """A partition the plan leaves where it is, as the preview names it."""

    slice: str
    predicted: Predicted  # its place, unchanged
    what: str  # 'APFS container disk5: "A"', 'ExFAT "B"'
    was: str | None = None  # its name before the plan, when a partition added before it renumbers it (D50)


@dataclass(frozen=True)
class ResizedPartition:
    """A partition the plan resizes in place, its data kept (T-044)."""

    slice: str
    was: Predicted  # its place now
    predicted: Predicted  # its place after
    what: str


@dataclass(frozen=True)
class RestoredPartition:
    """A partition as the backup has it (T-012): the map after a restore."""

    entry: int  # GPT entry / MBR slot
    predicted: Predicted  # its place
    what: str  # the type, as the preview names it


@dataclass(frozen=True)
class ExecutionPlan:
    strategy: str
    disk: str
    blocks: int  # the whole disk, in blocks of block_size: where the free space after lies
    block_size: int
    before: LayoutIs
    after: tuple[Predicted, ...]  # every partition after the plan, diskutil's own included
    partitions: tuple[PlannedPartition, ...]
    steps: tuple[Step, ...]
    notes: tuple[str, ...]  # what the preview must say besides the steps (K9: no EFI)
    kept: tuple[KeptPartition, ...] = ()  # partitions in `after` the plan does not touch
    resized: tuple[ResizedPartition, ...] = ()  # partitions in `after` resized in place
    restored: tuple[RestoredPartition, ...] = ()  # partitions in `after` from a map backup
    scheme: str = GPT  # the map after: GPT, or MBR when a restore brings one back
    # How this Mac mounts the volumes the plan makes, from the last trial mount, said before
    # anything is formatted (T-026, D34): a warning never stops the plan.
    mounting: tuple[str, ...] = ()
