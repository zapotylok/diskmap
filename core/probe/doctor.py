"""Read-only assembly of the `diskmap doctor` report (T-024, D16). Nothing is written.

Sources, all without root:
  diskutil listFilesystems -plist     what diskutil can format with (T-023)
  kmutil showloaded --list-only       loaded kexts (Paragon, macFUSE, hfs, apfs)
  pluginkit … com.apple.fskit.fsmodule, … io.macfuse.app.fsmodule.macfuse
                                      registered FSKit modules
  /sbin/mount                         who mounted what, rw or ro — the only fact about mounting
  <tool> --version / -V               third-party formatters found on PATH or in known places

Which driver could mount which file system is not in any of these: DRIVERS is a
catalog, like core.model.gpt_types, and every entry reports an observation. Apple's
NTFS driver mounts NTFS read-only via FSKit (rig, 2026-09-26) but is not listed by
pluginkit, so it is reported as not observable, not as absent.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.model.doctor import ABSENT, FAILS, LISTED, WORKS, Creator, Driver, Ext4Mounting, MountFact, Report, Row
from core.model.filesystems import Registry
from core.probe.command import run_read_only, run_version
from core.probe.errors import UnfamiliarInput
from core.probe.filesystems import parse_filesystems, read_filesystems
from core.probe.mounts import parse_mounts as mount_lines

# Third-party .fs bundles → the family their volumes belong to. System bundles are
# named after the family already (apfs, hfs, exfat, msdos). An unknown third-party
# bundle becomes a family of its own, with no drivers known.
THIRD_PARTY_BUNDLES = {"ufsd_NTFS": "ntfs", "ufsd_ExtFS": "ext"}
# `mount` type → family.
MOUNT_TYPES = {
    "apfs": "apfs",
    "hfs": "hfs",
    "exfat": "exfat",
    "msdos": "msdos",
    "ntfs": "ntfs",
    "ufsd_NTFS": "ntfs",
    "ufsd_ExtFS": "ext",
}
KEXT, FSKIT, UNOBSERVABLE = "kext", "fskit", "unobservable"
MACFUSE_FSKIT = "io.macfuse.app.fsmodule.macfuse"
MACFUSE_BUNDLE = "/Library/Filesystems/macfuse.fs"


@dataclass(frozen=True)
class DriverSpec:
    name: str
    kind: str  # KEXT (bundle id prefix), FSKIT (module id) or UNOBSERVABLE
    ident: str
    install: str | None = None  # when not installed
    installed: str | None = None  # a path whose presence means "installed", to tell
    #                               "not installed" from "installed, not active" (§10.1)
    note: str = ""


@dataclass(frozen=True)
class ToolSpec:
    package: str
    names: tuple[str, ...]  # tried in order; each one found is reported
    install: str
    dirs: tuple[str, ...]  # searched after PATH; "~" is the user's home


DRIVERS: dict[str, tuple[DriverSpec, ...]] = {
    "apfs": (DriverSpec("apfs kext", KEXT, "com.apple.filesystems.apfs"),),
    "hfs": (DriverSpec("hfs kext", KEXT, "com.apple.filesystems.hfs.kext"),),
    "exfat": (DriverSpec("Apple FSKit exfat", FSKIT, "com.apple.fskit.exfat"),),
    "msdos": (DriverSpec("Apple FSKit msdos", FSKIT, "com.apple.fskit.msdos"),),
    "ntfs": (
        DriverSpec(
            "Apple NTFS (FSKit)", UNOBSERVABLE, "", note="not listed by pluginkit; mounts read-only (rig, 2026-09-26)"
        ),
        DriverSpec("Paragon NTFS kext", KEXT, "com.paragon-software.filesystems.ntfs"),
        DriverSpec(
            "macFUSE kext (for ntfs-3g)",
            KEXT,
            "io.macfuse.filesystems.macfuse",
            install="brew install --cask macfuse",
            installed=MACFUSE_BUNDLE,
        ),
        DriverSpec(
            "macFUSE FSKit module (for ntfs-3g)",
            FSKIT,
            MACFUSE_FSKIT,
            install="brew install --cask macfuse",
            installed=MACFUSE_BUNDLE,
        ),
    ),
    "ext": (DriverSpec("Paragon ExtFS kext", KEXT, "com.paragon-software.filesystems.extfs"),),
}
# A third-party bundle of family "ext" → the driver its mount executable needs (T-053).
# mount_ufsd_ExtFS fails without the kext (rig, 2026-09-04) and mounts rw with it (2026-10-17).
EXT_MOUNTED_WITH = {"ufsd_ExtFS": DRIVERS["ext"][0]}
TOOLS: dict[str, ToolSpec] = {
    "ntfs": ToolSpec(
        "ntfs-3g",
        ("mkntfs",),
        "build ntfs-3g from https://github.com/tuxera/ntfs-3g (the homebrew-core formula is Linux-only)",
        ("/usr/local/sbin", "/opt/homebrew/sbin", "~/.local/ntfs-3g/sbin"),
    ),
    "ext": ToolSpec(
        "e2fsprogs",
        ("mkfs.ext4", "mke2fs"),
        "brew install e2fsprogs",
        ("/opt/homebrew/opt/e2fsprogs/sbin", "/usr/local/opt/e2fsprogs/sbin", "/usr/local/sbin", "/opt/homebrew/sbin"),
    ),
}


@dataclass(frozen=True)
class ToolProbe:
    path: str  # where it was found
    resolved: str  # after symlinks: a Paragon bundle's mke2fs is not e2fsprogs
    code: int
    output: str


@dataclass(frozen=True)
class RawDoctor:
    filesystems: Any
    kexts: str
    fskit: str
    macfuse_fskit: str
    mounts: str
    tools: Mapping[str, ToolProbe]  # tool name → probe; missing name: not found
    present: frozenset[str]  # which DriverSpec.installed paths exist


def read_raw(search_path: str, home: str) -> RawDoctor:
    """Read everything now. SEARCH_PATH is the caller's PATH, HOME expands "~"."""
    return RawDoctor(
        filesystems=read_filesystems(),
        kexts=_text(["/usr/bin/kmutil", "showloaded", "--list-only"]),
        fskit=_text(["/usr/bin/pluginkit", "-m", "-A", "-v", "-p", "com.apple.fskit.fsmodule"]),
        macfuse_fskit=_text(["/usr/bin/pluginkit", "-m", "-A", "-v", "-i", MACFUSE_FSKIT]),
        mounts=_text(["/sbin/mount"]),
        tools=find_tools(search_path, home),
        present=frozenset(
            d.installed for ds in DRIVERS.values() for d in ds if d.installed and os.path.exists(d.installed)
        ),
    )


def build(raw: RawDoctor) -> Report:
    """Pure part: raw readings in, Report out."""
    registry = parse_filesystems(raw.filesystems)
    kexts = parse_kexts(raw.kexts)
    fskit = {**parse_fskit(raw.fskit), **parse_fskit(raw.macfuse_fskit)}
    mounted, unattributed = parse_mounts(raw.mounts)

    creators = _personality_creators(registry)
    for family, spec in TOOLS.items():
        creators.setdefault(family, []).extend(_tool_creators(spec, raw.tools))
    rows = [
        Row(
            family=family,
            creators=tuple(found),
            drivers=tuple(_driver(d, kexts, fskit, raw.present) for d in DRIVERS.get(family, ())),
            mounts=mounted.get(family, ()),
        )
        for family, found in creators.items()
    ]
    return Report(rows=tuple(rows), unattributed=unattributed)


def parse_kexts(text: str) -> dict[str, str]:
    """Loaded kext bundle id → version, from `kmutil showloaded --list-only`."""
    line = re.compile(r"^\s*\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\S+) \(([^)]*)\) [0-9A-F-]{36} <[0-9 ]*>$")
    out = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = line.match(raw)
        if m is None:
            raise UnfamiliarInput(f"kmutil showloaded: unfamiliar line {raw!r}")
        out[m[1]] = m[2]
    if not out:
        raise UnfamiliarInput("kmutil showloaded: no kexts at all")
    return out


def parse_fskit(text: str) -> dict[str, str]:
    """FSKit module id → pluginkit election ('+' enabled, '-' disabled, ' ' default)."""
    line = re.compile(r"^ {3}([+\- !=]) ([A-Za-z0-9.\-]+)\(.*\)\t[0-9A-F-]{36}\t.*$")
    out = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.strip() == "(no matches)" or re.fullmatch(r"\s*\(\d+ plug-ins?\)", raw):
            continue
        m = line.match(raw)
        if m is None:
            raise UnfamiliarInput(f"pluginkit: unfamiliar line {raw!r}")
        out[m[2]] = m[1]
    return out


def parse_mounts(text: str) -> tuple[dict[str, tuple[MountFact, ...]], tuple[MountFact, ...]]:
    """Mounted /dev volumes grouped by family, and those of no known family."""
    by_family: dict[str, Counter[tuple[str, bool]]] = {}
    other: Counter[tuple[str, bool]] = Counter()
    for m in mount_lines(text):
        family = MOUNT_TYPES.get(m.type)
        (by_family.setdefault(family, Counter()) if family else other)[(m.by, m.writable)] += 1
    facts = {f: _facts(c) for f, c in by_family.items()}
    return facts, _facts(other)


def origin(resolved: str) -> str:
    """Where a found tool really lives: a filesystem bundle's own copy, or elsewhere."""
    m = re.search(r"/Library/Filesystems/([^/]+\.fs)/", resolved)
    return f"from {m[1]}" if m else resolved


def _facts(c: Counter[tuple[str, bool]]) -> tuple[MountFact, ...]:
    return tuple(MountFact(driver=d, writable=w, count=n) for (d, w), n in sorted(c.items()))


def _personality_creators(registry: Registry) -> dict[str, list[Creator]]:
    out: dict[str, list[Creator]] = {}
    for p in registry.personalities:
        if not p.formats:
            continue
        family = THIRD_PARTY_BUNDLES.get(p.bundle, p.bundle) if p.third_party else p.bundle
        where = f"third-party bundle {p.bundle_path}" if p.third_party else p.user_visible_name
        out.setdefault(family, []).append(Creator(f"diskutil {p.name}", LISTED, where))
    return out


def _tool_creators(spec: ToolSpec, tools: Mapping[str, ToolProbe]) -> list[Creator]:
    found = [(n, tools[n]) for n in spec.names if n in tools]
    out = []
    # A copy inside a filesystem bundle (Paragon's mke2fs) is shown, but it is not the
    # package: without the package's own tool, say how to get it.
    if not any(origin(t.resolved) == t.resolved for _, t in found):
        out.append(Creator(f"{spec.package} ({' / '.join(spec.names)})", ABSENT, "not found", install=spec.install))
    for name, t in found:
        first = next((ln.strip() for ln in t.output.splitlines() if ln.strip()), "")
        state = WORKS if t.code == 0 else FAILS
        detail = f"{first} — {t.path}" + ("" if t.resolved == t.path else f" → {origin(t.resolved)}")
        out.append(Creator(name, state, detail if state == WORKS else f"exit {t.code}: {first} — {t.path}"))
    return out


def _driver(d: DriverSpec, kexts: Mapping[str, str], fskit: Mapping[str, str], present: frozenset[str]) -> Driver:
    if d.kind == UNOBSERVABLE:
        return Driver(d.name, None, d.note)
    installed = d.installed is not None and d.installed in present
    where = f" (installed: {d.installed})" if installed else ""
    install = None if installed else d.install
    if d.kind == KEXT:
        hits = sorted(v for k, v in kexts.items() if k == d.ident or k.startswith(d.ident + "."))
        if hits:
            return Driver(d.name, True, f"loaded {', '.join(hits)}")
        return Driver(d.name, False, f"not loaded{where}", install=install)
    election = fskit.get(d.ident)
    if election is None:
        return Driver(d.name, False, f"not registered{where}", install=install)
    state = {"+": "enabled", "-": "disabled", " ": "default"}.get(election, f"election {election!r}")
    return Driver(d.name, election != "-", f"registered, {state}")


def working_tool(name: str, search_path: str, home: str) -> str | None:
    """Path of tool NAME (one of TOOLS' names) where doctor would report it as working,
    for the planner (T-042). None: absent, or its version check fails."""
    t = find_tools(search_path, home).get(name)
    return t.path if t is not None and t.code == 0 else None


def find_tools(search_path: str, home: str) -> dict[str, ToolProbe]:
    out = {}
    for spec in TOOLS.values():
        dirs = _dirs(spec, search_path, home)
        for name in spec.names:
            path = next(
                (os.path.join(d, name) for d in dirs if os.access(os.path.join(d, name), os.X_OK)),
                None,
            )
            if path is not None:
                code, output = run_version(path)
                out[name] = ToolProbe(path, os.path.realpath(path), code, output)
    return out


def mke2fs_path(search_path: str, home: str) -> str | None:
    """mke2fs for `ext4` (T-053): e2fsprogs' own when one is found and works, else a file
    system bundle's copy (Paragon's ufsd_ExtFS.fs ships mke2fs 1.46.4) — the plan names
    the path either way. None: no working mke2fs."""
    seen: set[str] = set()
    own, bundled = [], []
    for d in _dirs(TOOLS["ext"], search_path, home):
        path = os.path.join(d, "mke2fs")
        real = os.path.realpath(path)
        if real in seen or not os.access(path, os.X_OK):
            continue
        seen.add(real)
        if run_version(path)[0] == 0:
            (own if origin(real) == real else bundled).append(path)
    return next(iter(own + bundled), None)


def ext4_mounting(registry: Registry, kexts: Mapping[str, str]) -> Ext4Mounting:
    """Who would mount a new ext4 volume: the bundle diskutil lists for family "ext", and
    whether the driver it mounts with is loaded (T-053)."""
    bundles = sorted({p.bundle for p in registry.personalities if THIRD_PARTY_BUNDLES.get(p.bundle) == "ext"})
    if not bundles:
        return Ext4Mounting(None, None)
    if len(bundles) > 1:
        # Which one probes a volume first is the bundles' probe order, not known here.
        raise UnfamiliarInput(f"diskutil lists more than one bundle for ext: {bundles}")
    d = _driver(EXT_MOUNTED_WITH[bundles[0]], kexts, {}, frozenset())
    return Ext4Mounting(bundles[0], f"{d.name}, {d.detail}" if d.loaded else None)


def read_ext4_mounting() -> Ext4Mounting:
    return ext4_mounting(
        parse_filesystems(read_filesystems()), parse_kexts(_text(["/usr/bin/kmutil", "showloaded", "--list-only"]))
    )


def _dirs(spec: ToolSpec, search_path: str, home: str) -> list[str]:
    return [d for d in search_path.split(os.pathsep) if d] + [d.replace("~", home, 1) for d in spec.dirs]


def _text(argv: list[str]) -> str:
    return run_read_only(argv).decode(errors="replace")
