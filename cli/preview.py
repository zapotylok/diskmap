"""Text preview of an ExecutionPlan (T-045, T-014): the map now, the map after, the
steps with their exact commands, and what loses its data.

A command line is printed whole, never wrapped, exactly as it is to be typed: the argv
joined with shell quoting, `sudo ` in front when the step needs root. Copied line by
line into a terminal, the commands do what the plan says (rules.md §S6) — the rig test
does exactly that. Everything else is prose and wraps to the width.

plan_json is the same plan for a program (`diskmap plan --json`, the web UI, K14): the
preview text itself, so what a screen shows is what a terminal shows, plus the map after
and the steps as data to draw from.
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any

from cli.show import FREE, NBSP, SLACK, SYMBOLS, _row, _sub, _wrap, allocate, render, size
from core.model import gpt_types
from core.model.layout import MBR, gaps
from core.model.mapdump import head_blocks, tail_blocks
from core.planner.plan import Answers, ExecutionPlan, Loss, PlannedPartition, Step
from core.planner.predict import ADDED_BOOTER, ADDED_EFI, Predicted
from core.probe.show import DiskMap

STRATEGIES = {
    "repartition": "full repartition",
    "add": "add partitions after the last one",
    "shrink": "shrink a partition in place",
    "grow": "grow a partition in place",
    "backup": "back up the partition map",
    "restore": "restore the partition map from a backup",
    "mount": "mount a partition",
    "unmount": "unmount a partition",
}
ADDED = {ADDED_EFI: "EFI", ADDED_BOOTER: "Apple_Boot booter"}
INDENT = "    "


def command_line(step: Step) -> str:
    """The step as it is typed into a terminal."""
    return ("sudo " if step.needs_root else "") + shlex.join(step.command)


def _strategy(plan: ExecutionPlan) -> str:
    # Added into a gap, a partition always renumbers the ones after it (D50); in the tail, never.
    if plan.strategy == "add" and any(k.was for k in plan.kept):
        return "add partitions into the free space between two"
    return STRATEGIES.get(plan.strategy, plan.strategy)


def render_plan(plan: ExecutionPlan, now: DiskMap, width: int = 80, untested: str | None = None) -> str:
    """UNTESTED: what to say first on a macOS diskmap was not tested on (K19)."""
    blocks = [
        _wrap(
            f"Plan for {plan.disk} — {_strategy(plan)}. Nothing has been changed: diskmap only shows plans.",
            width,
        ),
        *(["UNTESTED MACOS\n" + _wrap(f"- {untested}", width)] if untested else []),
        "NOW\n" + render(now, width),
        "AFTER\n" + _after(plan, width),
        "STEPS\n" + "\n".join(_step(n, s, width) for n, s in enumerate(plan.steps, 1)),
    ]
    lost = [(n, s.destroys) for n, s in enumerate(plan.steps, 1) if s.destroys]
    if lost:
        blocks.append("\n".join(_losses(n, ls, width) for n, ls in lost))
    if plan.mounting:
        blocks.append("MOUNTING ON THIS MAC\n" + "\n".join(_wrap(f"- {t}", width) for t in plan.mounting))
    if plan.notes:
        blocks.append("NOTES\n" + "\n".join(_wrap(f"- {t}", width) for t in plan.notes))
    return "\n\n".join(blocks)


def _after(plan: ExecutionPlan, width: int) -> str:
    bs = plan.block_size
    mine = {pp.predicted: pp for pp in plan.partitions}
    kept = {k.predicted: k for k in plan.kept}
    resized = {r.predicted: r for r in plan.resized}
    restored = {r.predicted: r for r in plan.restored}
    rows: list[tuple[int, int, Predicted | None]] = [(q.start_lba, q.blocks, q) for q in plan.after]
    taken = [(q.start_lba, q.end_lba) for q in plan.after]
    if plan.scheme == MBR:
        first, last = 1, plan.blocks - 1
    else:  # the standard GPT: 128 entries at both ends (K7)
        first, last = head_blocks(bs), plan.blocks - tail_blocks(bs) - 1
    free = [f for f in gaps(first, last, taken) if f.blocks * bs >= SLACK]
    rows += [(f.start_lba, f.blocks, None) for f in free]
    rows.sort(key=lambda r: r[0])

    lines = []
    cells = allocate([r[1] for r in rows], width - 2)
    if cells is not None:
        sym = {q: SYMBOLS[i] if i < len(SYMBOLS) else "+" for i, q in enumerate(plan.after)}
        lines.append("[" + "".join((sym[r[2]] if r[2] else FREE) * c for r, c in zip(rows, cells)) + "]")
    lines.append(_row("#", "DEVICE", "START LBA", "SIZE", "TYPE / FILE SYSTEM", width=width))
    for i, (start, nblocks, q) in enumerate(rows):
        if q is None:
            lines.append(_row(FREE, "free", str(start), size(nblocks * bs), width=width))
            continue
        n = plan.after.index(q)
        symbol = SYMBOLS[n] if n < len(SYMBOLS) else "+"
        pp = mine.get(q)
        if q in kept:
            k = kept[q]
            lines.append(_row(symbol, k.slice, str(start), size(nblocks * bs), *_type(q.type), width=width))
            how = f"kept in place, data kept, was {k.was}" if k.was else "kept as is"
            lines.append(_sub(f"{how}: {k.what}", width))
            continue
        if q in resized:
            r = resized[q]
            lines.append(_row(symbol, r.slice, str(start), size(nblocks * bs), *_type(q.type), width=width))
            how = "grown" if q.blocks > r.was.blocks else "shrunk"
            lines.append(_sub(f"{how} in place from {size(r.was.blocks * bs)}, data kept: {r.what}", width))
            continue
        if q in restored:
            r = restored[q]
            lines.append(_row(symbol, f"entry {r.entry}", str(start), size(nblocks * bs), *_type(q.type), width=width))
            lines.append(_sub(f"from the backup: {r.what}", width))
            continue
        if pp is None:
            lines.append(_row(symbol, "—", str(start), size(nblocks * bs), *_type(q.type), width=width))
            lines.append(_sub(f"added by diskutil: {ADDED.get(q.added or '', q.added)} — not asked for", width))
            continue
        lines.append(_row(symbol, pp.slice, str(start), size(nblocks * bs), *_type(q.type), width=width))
        lines.append(_sub(_made(pp), width))
        lines.append(_sub(f"asked {pp.size_text} → {nblocks * bs} bytes", width))
    total = sum(q.blocks for q in plan.after) * bs
    spare = sum(f.blocks for f in free) * bs
    lines.append(
        _wrap(
            f"{len(plan.after)} partition(s), {size(total)} · " + (f"free {size(spare)}" if spare else "no free space"),
            width,
        )
    )
    return _prose("\n".join(lines))


def _type(guid: str) -> list[str]:
    t = gpt_types.known(guid)
    return [t.name, guid] if t else [guid]


def _prose(text: str) -> str:
    """Sizes carry a no-break space (cli.show); in prose it becomes a plain one. Never
    applied to a command line: a label may hold any character and must reach the shell intact."""
    return "\n".join(ln.rstrip() for ln in text.replace(NBSP, " ").splitlines())


def _made(pp: PlannedPartition) -> str:
    label = "EFI" if pp.format.casefold() == "efi" else pp.label
    return pp.makes + (f' "{label}"' if label else ", no label")


def _step(n: int, s: Step, width: int) -> str:
    head = f"{n:>2}. {s.intent}"
    if not s.writes_disk:
        flags = ["changes nothing on the disk" if s.rollback is None else "can be undone"]
    else:
        flags = ["irreversible" if s.rollback is None else "can be undone"]
    if s.needs_root:
        flags.append("needs root")
    if s.destroys:
        flags.append(f"destroys {len(s.destroys)} volume(s) — see DATA LOST")
    if isinstance(s.stdin, Answers):
        flags += [f"diskmap answers {a!r} to {q!r} on stdin" for q, a in s.stdin.pairs]
    else:
        flags.append("stdin closed")
        tool = os.path.basename(s.command[0])
        flags += [f"{tool} asks {q!r} and answers {a!r} itself" for q, a in s.stdin.tool_answers]
    return "\n".join([_wrap(head, width), _wrap(" · ".join(flags), width, INDENT), INDENT + command_line(s)])


def _losses(n: int, losses: tuple[Loss, ...], width: int) -> str:
    lines = [_wrap(f"DATA LOST — step {n} destroys all data on:", width)]
    for x in losses:
        text = f"{x.device}  {x.what}"
        if x.used is not None:
            text += f" · {size(x.used)} used"
        if not x.certain:
            text += " · diskutil cannot tell what is here — assume data"
        lines.append(_wrap(text, width, INDENT))
    return _prose("\n".join(lines))


JSON_FORMAT = 1


def plan_json(plan: ExecutionPlan, preview: str, untested: str | None = None) -> str:
    """PLAN as JSON, with PREVIEW, the text `diskmap plan` prints for it."""
    bs = plan.block_size
    mine = {pp.predicted: pp for pp in plan.partitions}
    kept = {k.predicted: k for k in plan.kept}
    resized = {r.predicted: r for r in plan.resized}
    restored = {r.predicted: r for r in plan.restored}
    after = []
    for q in plan.after:
        t = gpt_types.known(q.type)
        out: dict[str, Any] = {
            "start_lba": q.start_lba,
            "blocks": q.blocks,
            "bytes": q.blocks * bs,
            "type": q.type,
            "type_name": t.name if t else None,
        }
        if q in kept:
            out |= {"role": "kept", "device": kept[q].slice, "what": kept[q].what}
            if kept[q].was:
                out["was"] = kept[q].was  # renumbered by a partition added before it (D50)
        elif q in resized:
            r = resized[q]
            out |= {"role": "resized", "device": r.slice, "what": r.what, "was_bytes": r.was.blocks * bs}
        elif q in restored:
            out |= {"role": "restored", "entry": restored[q].entry, "what": restored[q].what}
        elif q in mine:
            pp = mine[q]
            out |= {
                "role": "new",
                "device": pp.slice,
                "format": pp.format,
                "label": pp.label,
                "size_text": pp.size_text,
                "what": _made(pp),
            }
        else:
            out |= {"role": "added", "what": ADDED.get(q.added or "", q.added)}
        after.append(out)
    steps = [
        {
            "intent": s.intent,
            "command_line": command_line(s),
            "needs_root": s.needs_root,
            "writes_disk": s.writes_disk,
            "reversible": s.rollback is not None,
            "destroys": [
                {"device": x.device, "what": x.what, "used": x.used, "certain": x.certain} for x in s.destroys
            ],
        }
        for s in plan.steps
    ]
    doc = {
        "format": JSON_FORMAT,
        "strategy": plan.strategy,
        "disk": plan.disk,
        "block_size": bs,
        "blocks": plan.blocks,
        "scheme": plan.scheme,
        "after": after,
        "steps": steps,
        "needs_root": any(s.needs_root for s in plan.steps),
        "mounting": list(plan.mounting),
        "notes": list(plan.notes),
        "untested": untested,
        "preview": preview,
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
