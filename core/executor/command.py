"""Running one step's command under its stdin contract (T-054, D27; rules.md §S10).

The rest of the executor — preflight, postflight, the journal, the order of steps — is
core.executor.run (T-050). This is the part that touches the process:

- argv as the step has it, never a shell (§S4); every disk identifier in argv belongs to
  the disk the guard issued, and a step that writes to the disk names it (a step on
  diskmap's own files — mkdir of a backup folder — names none);
- stdin is /dev/null for Closed, the declared answers for Answers — nothing implicit;
- no controlling terminal (a new session): a tool that would ask on /dev/tty gets an error
  at once instead of a prompt nobody sees, so a question ends in a failure, not a timeout;
- root only for a step that needs it: under sudo, any other step runs as the user who
  called sudo (core.executor.privilege, T-051);
- every question in the output is looked for, and one the step does not declare fails the
  step with the question's text. A tool that answers itself (diskutil for Paragon NTFS) has
  already acted by then: the failure says that what it did is unknown and the map must be
  read again — postflight's job.

A timeout is only the backstop for a tool that hangs for another reason.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from core.executor.privilege import Invoker, become
from core.planner.plan import Answers, Closed, Step
from core.safety.guard import AvailableDisk

# A question as command-line tools print them: "(y/n)?", "[y/N]", "(yes/no)?". The answer a
# tool gives itself follows on the same line ("Proceed with Format (y/n)?y").
QUESTION = re.compile(r"^(?P<q>.*?(?:\((?:y/n|yes/no)\)\s*\??|\[(?:y/n)\]\s*\??))(?P<a>.*)$", re.IGNORECASE)
DISK_ID = re.compile(r"(?:^|/dev/r?)(disk[0-9]+)(?:s[0-9]+)*$")


class StepFailed(Exception):
    """The command did not do what the step declared; the message is for the journal.
    STARTED: the process ran (a question, a timeout) and may have acted — the map must be
    read again; False: refused before anything ran. RC is None when there is no exit code
    (not started, or killed at the timeout); STDOUT and STDERR are what it printed."""

    def __init__(self, message: str, started: bool, rc: int | None = None, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.started = started
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class Asked:
    question: str
    answer: str  # what followed it on the line: the tool's own answer, or "" when it read one


@dataclass(frozen=True)
class Ran:
    returncode: int
    stdout: str
    stderr: str
    asked: tuple[Asked, ...]


def run_step_command(step: Step, disk: AvailableDisk, timeout: float, as_user: Invoker | None = None) -> Ran:
    """Run STEP's command on DISK; as AS_USER unless the step needs root (None: as this
    process). Raises StepFailed on an undeclared question, a timeout, argv naming another
    disk, or a command that could not be started. A non-zero exit is returned, not raised:
    what it means is the caller's (partitionDisk may fail after changing the map, D21)."""
    if not isinstance(disk, AvailableDisk):
        raise TypeError("a step runs only on a disk the guard issued (rules.md §S8)")
    named = {m[1] for a in step.command if (m := DISK_ID.search(a))}
    if named - {disk.bsd_name} or (step.writes_disk and not named):
        raise StepFailed(
            f"{step.intent}: argv names {sorted(named) or 'no disk'}, the guard issued {disk.bsd_name}", started=False
        )
    if isinstance(step.stdin, Answers):
        feed: bytes | None = "".join(f"{a}\n" for _, a in step.stdin.pairs).encode()
        declared = {q: None for q, _ in step.stdin.pairs}
    else:
        assert isinstance(step.stdin, Closed)
        feed = None
        declared = dict(step.stdin.tool_answers)
    drop = become(as_user) if as_user is not None and not step.needs_root else None
    try:
        p = subprocess.run(
            list(step.command),
            input=feed,
            stdin=subprocess.DEVNULL if feed is None else None,
            capture_output=True,
            start_new_session=True,
            timeout=timeout,
            check=False,
            preexec_fn=drop,
        )
    except subprocess.TimeoutExpired as e:
        so, se = (e.stdout or b"").decode(errors="replace"), (e.stderr or b"").decode(errors="replace")
        seen = [a.question for a in questions(so + "\n" + se)]
        raise StepFailed(
            f"{step.intent}: no exit within {timeout:g} s, killed"
            + (f"; it asked {seen[-1]!r}" if seen else "")
            + f"; last output: {(so + se).strip()[-300:]!r}",
            started=True,
            stdout=so,
            stderr=se,
        ) from None
    except (OSError, subprocess.SubprocessError) as e:
        # Not found, not executable, or the drop to the user failed (it runs in the child,
        # before exec): nothing ran.
        raise StepFailed(f"{step.intent}: {step.command[0]} could not be started: {e}", started=False) from None
    out, err = p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    asked = questions(out + "\n" + err)
    for a in asked:
        if a.question not in declared:
            raise StepFailed(
                f"{step.intent}: {step.command[0]} asked {a.question!r}, which the plan does not declare"
                + (f", and answered {a.answer!r} itself" if a.answer else "")
                + " — what it did after that is not known: read the map again",
                started=True,
                rc=p.returncode,
                stdout=out,
                stderr=err,
            )
        expected = declared[a.question]
        if expected is not None and a.answer != expected:
            raise StepFailed(
                f"{step.intent}: {step.command[0]} answered {a.answer!r} to {a.question!r} itself, "
                f"the plan declares {expected!r}",
                started=True,
                rc=p.returncode,
                stdout=out,
                stderr=err,
            )
    return Ran(p.returncode, out, err, tuple(asked))


def questions(text: str) -> list[Asked]:
    out = []
    for line in text.splitlines():
        if m := QUESTION.match(line.strip()):
            out.append(Asked(m["q"].strip(), m["a"].strip()))
    return out
