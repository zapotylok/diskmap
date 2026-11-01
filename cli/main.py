"""diskmap commands: `show` (T-030), `doctor` (T-024; `--trial` writes to its own image only,
T-026), `plan` (T-045), `backup-map` and
`restore-map` (T-012), `journal` (T-013), `ui` (T-061: the same map and plans in a browser,
K14) — all read-only: plan, backup-map and restore-map show plans, they apply nothing — and
`apply` (T-050), the one that writes: the same plans, built from the same words, shown,
confirmed and run. `mount` and `unmount` (T-073, K16) run their one-step plan at once:
the map does not change and nothing is destroyed. `install-app` (T-079) copies the window,
diskmap.app, into Applications — files, not disks."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from cli import apply, journal, mount, plan, show, trial
from cli.doctor import render
from cli.preview import plan_json
from core.executor.image import ImageError
from core.executor.journal import JournalError, journal_path, read_journal
from core.executor.privilege import PrivilegeError, invoker
from core.executor.run import Refused
from core.probe import doctor
from core.probe.errors import ProbeError
from core.probe.show import Failed, read_map
from core.probe.trial import read_setup, read_trial
from core.safety.image import NotTrialImage
from core.version import VERSION

APPLY_KINDS = {
    "repartition": "the whole disk, destroying what is on it",
    "add": "partitions after the last one (or --after diskNsM, into a gap), touching nothing",
    "shrink": "a partition (diskNsM) in place, new partitions after it",
    "grow": "a partition (diskNsM) in place, into the free space right after it",
    "backup-map": "back up the partition map",
    "restore-map": "put a backed-up partition map back",
}


AFTER_HELP = (
    "add only: the partition (diskNsM) whose following free space to fill, when that is between two partitions; "
    "the ones after it are renumbered"
)


def _plan_args(p: argparse.ArgumentParser, kind: str) -> None:
    """The words that name a plan: KIND is a strategy, backup-map or restore-map."""
    if kind == "restore-map":
        p.add_argument("backup", help="the backup's folder, as a plan's backup steps made it")
    if kind == plan.SHRINK:
        p.add_argument("disk", metavar="diskNsM", help="the partition to shrink")
        p.add_argument("layout", nargs="?", help="layout JSON file: the partitions to add after it (optional)")
        p.add_argument("--to", metavar="SIZE", required=True, help="the new size of the partition, e.g. 900Mi")
        return
    if kind == plan.GROW:
        p.add_argument("disk", metavar="diskNsM", help="the partition to grow")
        p.add_argument("--to", metavar="SIZE", required=True, help="the new size of the partition, e.g. 3Gi")
        return
    p.add_argument("disk", metavar="diskN", help="whole disk, diskN or /dev/diskN")
    if kind in plan.STRATEGIES:
        p.add_argument("layout", help='layout JSON file, or - for stdin: {"partitions": [{"format", "label", "size"}]}')
    if kind == "add":
        p.add_argument(
            "--after",
            metavar="diskNsM",
            help=AFTER_HELP,
        )


def _build(kind: str, args: argparse.Namespace, width: int, home: str | None = None) -> plan.Built:
    """The plan KIND with ARGS names, as `plan` shows it and `apply` runs it; HOME holds
    the map backups (None: this user's). Raises plan.Refused, ProbeError or OSError."""
    home, at = home or str(Path.home()), datetime.now(UTC).astimezone()
    if getattr(args, "after", None) is not None and kind != "add":
        raise plan.Refused(f"--after is for add, not {kind}")
    if kind == "backup-map":
        return plan.build_backup(args.disk, home, width, at)
    if kind == "restore-map":
        return plan.build_restore(args.backup, args.disk, home, width, at)
    if kind == plan.GROW:
        return plan.build_grow(args.disk, args.to, home, width, at)
    layout = getattr(args, "layout", None)
    text = None
    if layout == "-":
        text = sys.stdin.read()
    elif layout is not None:
        with open(layout, encoding="utf-8") as f:
            text = f.read()
    doc = plan.load_layout(text, layout) if text is not None else None
    env = (os.environ.get("PATH", ""), home, width, at)
    if kind == plan.SHRINK:
        return plan.build_shrink(args.disk, args.to, doc, *env)
    return plan.build(kind, args.disk, doc, *env, after=getattr(args, "after", None))


def drop_dead_terminal() -> None:
    """A terminal gone mid-apply (T-066) leaves what failed to be written in the buffer of
    sys.stdout; at exit Python writes it again, fails, and turns the exit status into 120.
    Point stdout and stderr that cannot be flushed at /dev/null: the status stays apply's."""
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except OSError:
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, f.fileno())
            os.close(null)


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="diskmap")
    parser.add_argument("--version", action="version", version=f"diskmap {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    p_show = sub.add_parser("show", help="partition map of every physical disk, or of the ones named (read-only)")
    p_show.add_argument("disks", nargs="*", metavar="diskN", help="whole disk, diskN or /dev/diskN")
    p_show.add_argument("--json", action="store_true", help="machine-readable output")
    p_show.add_argument("--external", action="store_true", help="leave out protected disks (and say which)")
    p_doctor = sub.add_parser("doctor", help="file systems: how they are created, driven and mounted (read-only)")
    p_doctor.add_argument(
        "--trial",
        action="store_true",
        help="make and mount every format on a disk image of doctor's own, and keep how this Mac mounts each",
    )
    p_doctor.add_argument(
        "--only", metavar="FORMAT", action="append", default=[], help="with --trial: just this format (repeatable)"
    )
    p_plan = sub.add_parser("plan", help="show what laying out a disk would do (applies nothing)")
    p_plan.add_argument(
        "strategy",
        choices=sorted([*plan.STRATEGIES, plan.SHRINK, plan.GROW]),
        help="repartition: the whole disk, destroying what is on it; add: after the last partition, touching "
        "nothing; shrink: a partition (diskNsM) in place, new partitions after it; grow: a partition (diskNsM) "
        "in place, into the free space right after it",
    )
    p_plan.add_argument(
        "disk", metavar="diskN", help="whole disk, diskN or /dev/diskN; for shrink and grow the partition, diskNsM"
    )
    p_plan.add_argument(
        "layout",
        nargs="?",
        help='layout JSON file, or - for stdin: {"partitions": [{"format", "label", "size"}]}; '
        "for shrink optional: the partitions to add after it",
    )
    p_plan.add_argument("--to", metavar="SIZE", help="shrink and grow only: the new size of the partition, e.g. 900Mi")
    p_plan.add_argument(
        "--after",
        metavar="diskNsM",
        help=AFTER_HELP,
    )
    p_plan.add_argument("--json", action="store_true", help="machine-readable output, the preview text included")
    _plan_args(
        sub.add_parser("backup-map", help="show the plan that backs up a disk's partition map (applies nothing)"),
        "backup-map",
    )
    _plan_args(
        sub.add_parser("restore-map", help="show the plan that puts a backed-up partition map back (applies nothing)"),
        "restore-map",
    )
    sub.add_parser("journal", help="what earlier runs did: outcome, steps done, map backups (read-only)")
    p_ui = sub.add_parser("ui", help="the map and plans in a browser, on this Mac only (read-only)")
    p_ui.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: one the system picks)")
    p_ui.add_argument("--no-open", action="store_true", help="print the link, do not open a browser")
    p_ui.add_argument(
        "--exit-on-eof", action="store_true", help="stop when stdin closes (diskmap.app runs the server this way)"
    )
    p_install = sub.add_parser(
        "install-app", help="copy the window, diskmap.app, into Applications (/Applications, else ~/Applications)"
    )
    p_install.add_argument("--to", metavar="FOLDER", help="copy it into FOLDER instead")
    for verb in ("mount", "unmount"):
        p = sub.add_parser(verb, help=f"{verb} a partition of an external disk, as Finder would (the map stays)")
        p.add_argument("disk", metavar="diskNsM", help="the partition")
    p_apply = sub.add_parser(
        "apply", help="build a plan as `plan` does, show it, and run it once you type the disk's name"
    )
    kinds = p_apply.add_subparsers(dest="kind", required=True, metavar="{" + ",".join(APPLY_KINDS) + "}")
    for kind, what in APPLY_KINDS.items():
        _plan_args(kinds.add_parser(kind, help=what), kind)
    args = parser.parse_args(argv)
    width = shutil.get_terminal_size((80, 24)).columns

    if args.command == "journal":
        path = journal_path(str(Path.home()))
        try:
            runs, torn = read_journal(path)
        except JournalError as e:
            print(f"journal: {e}", file=sys.stderr)
            return 1
        print(journal.render(runs, torn, path))
        return 0

    if args.command == "ui":
        return _ui(args.port, not args.no_open, args.exit_on_eof)

    if args.command == "install-app":
        return _install_app(args.to)

    if args.command in ("mount", "unmount"):
        if os.geteuid() == 0:
            print(f"{args.command}: run it as yourself, not as root — a volume root mounts is root's", file=sys.stderr)
            return 1
        try:
            built = plan.build_mount(args.disk, args.command == "mount", width)
        except (plan.Refused, ProbeError, OSError) as e:
            print(f"{args.command}: {e}", file=sys.stderr)
            return 1
        return mount.run(built, str(Path.home()), os.environ.get("PATH", ""))

    if args.command == "apply":
        if getattr(args, "layout", None) == "-":
            parser.error("apply reads the confirmation from stdin: give the layout as a file")
        try:
            # Under sudo, home is the caller's, whatever sudo did with HOME (T-051).
            user = invoker(os.geteuid(), os.environ)
            home = user.home if user is not None else str(Path.home())
            built = _build(args.kind, args, width, home)
        except (PrivilegeError, plan.Refused, ProbeError, OSError) as e:
            print(f"apply: {e}", file=sys.stderr)
            return 1
        path = os.environ.get("PATH", "")
        code = apply.run(built, home, path, _ask, os.geteuid() == 0, as_user=user, width=width)
        drop_dead_terminal()
        return code

    if args.command in ("backup-map", "restore-map", "plan"):
        kind = args.command
        if kind == "plan":
            kind = args.strategy
            if (kind in (plan.SHRINK, plan.GROW)) != (args.to is not None):
                parser.error("--to is required for shrink and grow, and only for them")
            if args.layout is None and kind not in (plan.SHRINK, plan.GROW):
                parser.error(f"{kind} needs a layout")
            if args.layout is not None and kind == plan.GROW:
                parser.error("grow takes no layout: diskutil adds no partitions with a grow")
        try:
            built = _build(kind, args, width)
        except (plan.Refused, ProbeError, OSError) as e:
            print(f"{args.command}: {e}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            sys.stdout.write(plan_json(built.plan, built.preview, built.untested))
        else:
            print(built.preview)
        return 0

    if args.command == "show":
        try:
            m = read_map(args.disks, external=args.external)
        except ProbeError as e:
            print(f"show: {e}", file=sys.stderr)
            return 1
        if args.json:
            sys.stdout.write(show.to_json(m))
        else:
            print(show.render(m, shutil.get_terminal_size((80, 24)).columns))
        # A disk that could not be mapped is shown, and still fails the command.
        return 1 if any(isinstance(d, Failed) for d in m.disks) else 0

    if args.command == "doctor":
        if args.only and not args.trial:
            parser.error("--only goes with --trial")
        search_path, home = os.environ.get("PATH", ""), str(Path.home())
        try:
            if args.trial:
                at = datetime.now(UTC).astimezone()
                trial.run_trial(home, search_path, trial.trial_folder(home, at), at, only=tuple(args.only))
                print()
            report = doctor.build(doctor.read_raw(search_path, home))
            now = read_trial(home, read_setup(search_path, home))
        except KeyboardInterrupt:
            print("\ndoctor: trial stopped; the image was detached, nothing is kept", file=sys.stderr)
            return 130
        except (trial.TrialError, ImageError, NotTrialImage, JournalError, ProbeError, OSError, Refused) as e:
            print(f"doctor: {e}", file=sys.stderr)
            return 1
        print(render(report, now))
    return 0


def _ui(port: int, open_browser: bool, exit_on_eof: bool = False) -> int:
    """Serve the web UI until Ctrl-C. Never as root: the UI only reads, and what writes
    is `sudo diskmap apply` in a terminal (K14). EXIT_ON_EOF: also until stdin closes —
    diskmap.app (A′) holds the server's stdin, so the server ends with the app, however
    the app ends, and is never left serving with no window."""
    from ui.server import core_of, serve

    if os.geteuid() == 0:
        print(
            "ui: run it as yourself, not as root — it only reads; writing is sudo diskmap apply (K14)", file=sys.stderr
        )
        return 1
    try:
        server = serve(core_of(os.environ.get("PATH", ""), str(Path.home())), port)
    except OSError as e:
        print(f"ui: {e}", file=sys.stderr)
        return 1
    print(f"diskmap ui: {server.link}")
    print("Only this link lets a browser in. Ctrl-C stops the server.", flush=True)
    if open_browser:
        subprocess.run(["/usr/bin/open", server.link], check=False)
    if exit_on_eof:
        threading.Thread(target=_shutdown_at_eof, args=(server.shutdown,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


def _install_app(to: str | None) -> int:
    """Copy diskmap.app where Launchpad and Spotlight find it (T-079)."""
    from cli import install_app

    try:
        app = install_app.find_app()
        folder = Path(to).expanduser() if to else install_app.destination(str(Path.home()))
        target = install_app.install(app, folder)
    except (install_app.InstallError, OSError) as e:
        print(f"install-app: {e}", file=sys.stderr)
        return 1
    print(f"installed {target}")
    print("Open it from Launchpad or Spotlight, or: open -a diskmap")
    return 0


def _shutdown_at_eof(stop: Callable[[], None]) -> None:
    """Read stdin to its end — nothing is said on it — then STOP the server."""
    while sys.stdin.buffer.read(4096):
        pass
    stop()
