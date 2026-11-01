"""Operation journal (T-013): append-only ~/.diskmap/journal.jsonl, one JSON object a line.

Written by the executor (core.executor.run, T-050) through Journal.run(). A run holds
journal.lock (flock) for its whole length: two plans never run at once, and a run left
open in the journal is known to be dead once the lock is free. Each record is one write(), then fsync.
Under sudo the folder, the lock and the journal are given to the user who called it (OWNER,
T-051): the journal is theirs to read and to append to without root.

Records; each has format, run and t (local time, ISO 8601):
  run-started    the plan whole (every step: argv, flags, assertions, by type), the
                 environment (§S9, core.probe.environment), the T-003 snapshot before
  step-started   the step's number, written once its preflight holds, right before its
                 command runs; a step its preflight refused has step-finished alone
  step-finished  exit code, full stdout and stderr, the questions answered (T-054), and
                 the error that failed it, if any (a preflight refused, a postflight not met)
  run-finished   outcome, steps done, the step in flight, why it stopped, the snapshot
                 after; closed_by — the later run that found this one left open

The outcome is one function of the records, whoever closes the run:
  applied            every step finished
  not applied        no step that writes to the disk (Step.writes_disk) started: only
                     reads, the map backup, unmounts ran — the map is the one before
  partially applied  a step that writes started and the plan did not finish: the map is
                     somewhere between before and after, and the steps done say where

A run whose process died (kill -9, power) has no run-finished. The next Journal.run()
appends one for it — outcome from its records, closed_by set — so the mark is in the
journal itself, not only in the way it is read. A last line cut off mid-write (power
during the write) stops the next run: the journal is not appended to after a torn line.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.planner.plan import ExecutionPlan, MapBackedUp
from core.probe.errors import ProbeError

FORMAT = 1
APPLIED = "applied"
NOT_APPLIED = "not applied"
PARTIAL = "partially applied"
EVENTS = ("run-started", "step-started", "step-finished", "run-finished")
LEFT_OPEN = "the process ended without closing the run (killed, crashed, or the power went)"


class JournalError(Exception):
    """The journal cannot be read or appended to; the message says why."""


class JournalBusy(JournalError):
    """Another run holds the journal: a plan is being applied now."""


def journal_path(home: str) -> str:
    return os.path.join(home, ".diskmap", "journal.jsonl")


def encode(value: Any) -> Any:
    """VALUE as JSON data. A dataclass carries its type: MapBackedUp and MapIs have the
    same fields, and the journal must tell them apart."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"type": type(value).__name__} | {
            f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, tuple | list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise TypeError(f"cannot journal {type(value).__name__}: {value!r}")


@dataclass(frozen=True)
class RunState:
    """A run as its records show it."""

    run: str
    started: str  # time of run-started
    strategy: str
    disk: str
    steps: int
    done: tuple[int, ...]  # finished without an error, in order
    failed: int | None  # finished with an error
    in_flight: int | None  # started, never finished
    outcome: str
    closed: bool  # a run-finished exists
    reason: str | None  # why it stopped; None: it ran to the end
    closed_by: str | None
    backups: tuple[str, ...]  # map backup folders the plan makes (K11)


def outcome(writes: list[bool], started: set[int], done: list[int]) -> str:
    """WRITES: Step.writes_disk of steps 1..N; STARTED, DONE: step numbers."""
    if len(done) == len(writes) and done == list(range(1, len(writes) + 1)):
        return APPLIED
    if any(writes[n - 1] for n in started):
        return PARTIAL
    return NOT_APPLIED


class _Records:
    """The records of one run, as they are read or written."""

    def __init__(self, started: dict[str, Any]) -> None:
        self.started = started
        self.writes = [bool(s["writes_disk"]) for s in started["plan"]["steps"]]
        self.begun: set[int] = set()
        self.done: list[int] = []
        self.failed: int | None = None
        self.finished: dict[str, Any] | None = None

    def add(self, r: dict[str, Any], where: str) -> None:
        event = r["event"]
        if self.finished is not None:
            raise JournalError(f"{where}: {event} after the run was finished")
        if event in ("step-started", "step-finished"):
            n = r.get("step")
            if not isinstance(n, int) or not 1 <= n <= len(self.writes):
                raise JournalError(f"{where}: step {n!r} is not one of the plan's 1..{len(self.writes)}")
            if event == "step-started":
                self.begun.add(n)
            elif r.get("error") is None and r.get("rc") == 0:
                self.done.append(n)
            else:
                self.failed = n
        elif event == "run-finished":
            self.finished = r
        else:
            raise JournalError(f"{where}: second run-started for run {r['run']}")

    def in_flight(self) -> int | None:
        ended = set(self.done) | ({self.failed} if self.failed is not None else set())
        left = sorted(self.begun - ended)
        return left[0] if left else None

    def outcome(self) -> str:
        return outcome(self.writes, self.begun, self.done)

    def state(self) -> RunState:
        plan = self.started["plan"]
        f = self.finished
        backups = sorted(
            {
                a["folder"]
                for s in plan["steps"]
                for a in (*s["preflight"], *s["postflight"])
                if a.get("type") == MapBackedUp.__name__
            }
        )
        return RunState(
            run=self.started["run"],
            started=self.started["t"],
            strategy=plan["strategy"],
            disk=plan["disk"],
            steps=len(self.writes),
            done=tuple(self.done),
            failed=self.failed,
            in_flight=self.in_flight(),
            outcome=f["outcome"] if f else self.outcome(),
            closed=f is not None,
            reason=f["reason"] if f else None,
            closed_by=f.get("closed_by") if f else None,
            backups=tuple(backups),
        )


def read_journal(path: str) -> tuple[list[RunState], bool]:
    """Every run in the journal at PATH, in order, and whether its last line is torn
    (cut off mid-write: ignored here, refused by the next run). No file: no runs."""
    runs, torn = _read(path)
    return [r.state() for r in runs.values()], torn


def _read(path: str) -> tuple[dict[str, _Records], bool]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return {}, False
    lines = data.split(b"\n")
    torn = lines[-1] != b""
    runs: dict[str, _Records] = {}
    for i, raw in enumerate(lines[:-1], 1):
        where = f"{path}:{i}"
        try:
            r = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise JournalError(f"{where}: not a JSON record — {e}") from None
        if not isinstance(r, dict) or r.get("format") != FORMAT or r.get("event") not in EVENTS:
            raise JournalError(f"{where}: unfamiliar record (format {FORMAT}, events {', '.join(EVENTS)})")
        run = r.get("run")
        if not isinstance(run, str):
            raise JournalError(f"{where}: no run id")
        if r["event"] == "run-started":
            if run in runs:
                raise JournalError(f"{where}: second run-started for run {run}")
            runs[run] = _Records(r)
        elif run not in runs:
            raise JournalError(f"{where}: {r['event']} of run {run}, which never started")
        else:
            runs[run].add(r, where)
    return runs, torn


class Run:
    """One plan being applied. Made by Journal.run(); the caller records the steps."""

    def __init__(self, journal: Journal, records: _Records) -> None:
        self._journal = journal
        self._records = records
        self.id: str = records.started["run"]

    def step_started(self, n: int) -> None:
        self._write({"event": "step-started", "step": n})

    def step_finished(
        self,
        n: int,
        rc: int | None,
        stdout: str,
        stderr: str,
        asked: tuple[tuple[str, str], ...] = (),
        error: str | None = None,
    ) -> None:
        """RC None: the command never ran (its preflight refused it). ERROR: what failed
        the step — a refused preflight, an unmet postflight, an unexpected question."""
        self._write(
            {
                "event": "step-finished",
                "step": n,
                "rc": rc,
                "stdout": stdout,
                "stderr": stderr,
                "asked": [list(qa) for qa in asked],
                "error": error,
            }
        )

    def _write(self, record: dict[str, Any]) -> None:
        r = self._journal._append(self.id, record)
        self._records.add(r, "this run")


class Journal:
    def __init__(self, path: str, owner: tuple[int, int] | None = None) -> None:
        """OWNER: (uid, gid) to give the files to; None: they stay this process's."""
        self.path = path
        self.owner = owner
        self._fd: int | None = None

    @contextmanager
    def run(
        self,
        plan: ExecutionPlan,
        environment: dict[str, Any],
        before: Callable[[], Any],
        after: Callable[[], Any],
    ) -> Iterator[Run]:
        """Record applying PLAN. BEFORE and AFTER give the T-003 snapshots; one that
        fails is recorded as its error. The run is finished on leaving: why it stopped is
        the exception that left (re-raised), or the step that failed, or nothing."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        lock = os.open(self.path.removesuffix(".jsonl") + ".lock", os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            self._own(os.path.dirname(self.path), lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JournalBusy(f"{self.path}: another diskmap run is applying a plan now") from None
            runs, torn = _read(self.path)
            if torn:
                raise JournalError(
                    f"{self.path}: the last line is cut off (written when the power went?) — move the file "
                    "aside and look at it before the next run"
                )
            self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            self._own(self._fd)
            run_id = f"{datetime.now(UTC).astimezone():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
            for left in runs.values():
                if left.finished is None:  # the lock is ours: its process is gone
                    self._close(left, LEFT_OPEN, None, closed_by=run_id)
            started = self._append(
                run_id,
                {
                    "event": "run-started",
                    "plan": encode(plan),
                    "environment": environment,
                    "before": _snapshot(before),
                },
            )
            r = Run(self, _Records(started))
            try:
                yield r
            except BaseException as e:
                self._close(r._records, f"interrupted: {type(e).__name__}: {e}".rstrip(": "), after)
                raise
            failed = r._records.failed
            done = len(r._records.done) == len(r._records.writes)
            self._close(r._records, None if done else _stopped(failed), after)
        finally:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            os.close(lock)

    def _own(self, *what: str | int) -> None:
        """Give WHAT (paths, open descriptors) to the owner, if there is one."""
        if self.owner is not None:
            for w in what:
                os.chown(w, *self.owner)

    def _close(
        self, rec: _Records, reason: str | None, after: Callable[[], Any] | None, closed_by: str | None = None
    ) -> None:
        record: dict[str, Any] = {
            "event": "run-finished",
            "outcome": rec.outcome(),
            "done": rec.done,
            "failed": rec.failed,
            "in_flight": rec.in_flight(),
            "reason": reason,
            "after": _snapshot(after) if after is not None else None,
            "closed_by": closed_by,
        }
        rec.add(self._append(rec.started["run"], record), "closing")

    def _append(self, run_id: str, record: dict[str, Any]) -> dict[str, Any]:
        assert self._fd is not None, "the journal is written only inside run()"
        full = {"format": FORMAT, "run": run_id, "t": datetime.now(UTC).astimezone().isoformat(), **record}
        data = (json.dumps(full, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view) :]
        os.fsync(self._fd)
        return full


def _stopped(failed: int | None) -> str:
    return f"step {failed} failed" if failed is not None else "stopped before the last step"


def _snapshot(take: Callable[[], Any]) -> Any:
    try:
        return take()
    except (ProbeError, OSError) as e:  # a snapshot that cannot be taken is recorded, not raised
        return {"error": f"{type(e).__name__}: {e}"}
