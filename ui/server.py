"""`diskmap ui` (T-061, K14): a local web UI over the same core. Read-only.

The server runs as the user who started it, never as root, and does what `show` and
`plan` do: it maps the disks and builds plans, and applies nothing. Writing stays
`sudo diskmap apply …` in a terminal, where the plan is built again, shown and confirmed
by the process that runs it (K14). One exception, K16: it mounts and unmounts a partition
by running `diskmap mount|unmount` — a one-step plan that changes no map and destroys
nothing, journaled like any other.

Who may talk to it:
- it listens on 127.0.0.1 only, on a port the system picks;
- every request must name that very host and port in `Host`, so a page whose name was
  rebound to 127.0.0.1 is turned away (DNS rebinding);
- the page is served only to a browser holding the session cookie, which the one-time
  link `diskmap ui` opens sets (SameSite=Strict: no other site's page sends it);
- every /api/ call carries the token in X-Diskmap-Token, a header no other origin can
  set without a CORS preflight this server never answers.
The token is made per run and lives as long as the process.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from core.version import VERSION

STATIC = Path(__file__).parent / "static"
FILES = {
    "/static/app.js": "text/javascript; charset=utf-8",
    "/static/editor.js": "text/javascript; charset=utf-8",
    "/static/style.css": "text/css; charset=utf-8",
    "/static/icon.svg": "image/svg+xml",
}
COOKIE = "diskmap"
HEADER = "X-Diskmap-Token"
TOKEN_SLOT = "{{TOKEN}}"
VERSION_SLOT = "{{VERSION}}"
MAX_BODY = 64 * 1024
MOUNT_TIMEOUT = 120.0  # diskutil mount waits for fsck of a dirty volume; longer is a hang
KINDS = ("repartition", "add", "shrink", "grow", "backup-map")
RESIZE = ("shrink", "grow")  # take a partition and a new size
WHOLE = re.compile(r"disk[0-9]+")
SLICE = re.compile(r"disk[0-9]+s[0-9]+")
SECURITY = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class BadRequest(Exception):
    """The request names no plan this server builds; the message says why."""


@dataclass(frozen=True)
class MountRequest:
    slice: str  # diskNsM
    mount: bool  # False: unmount


class Declined(Exception):
    """The core built no plan or map; the message is the core's own, for the page."""


@dataclass(frozen=True)
class PlanRequest:
    kind: str  # one of KINDS
    disk: str  # diskN; for shrink and grow the partition, diskNsM
    layout: Any | None  # the layout document, as `diskmap plan` reads it from a file
    to: str | None  # shrink and grow only: the new size
    after: str | None = None  # add only: the partition before the gap to fill (T-076, D50); None: the tail


@dataclass(frozen=True)
class Core:
    """What the server asks of diskmap: the map as `show --json` writes it, the formats a
    layout can name (cli.formats), and a plan as `plan --json` writes it. Each raises
    Declined with the reason, for the page."""

    disk_map: Callable[[], str]
    formats: Callable[[], str]
    plan: Callable[[PlanRequest], str]
    # Hand a plan to `sudo diskmap apply` (T-062, ui/terminal.py): write its files and, when
    # asked, open them in a new Terminal window. JSON: the handoff's id, the command line, the
    # script, and whether Terminal opened it.
    terminal: Callable[[PlanRequest, bool], str]
    # How a handoff ended (T-065), by its id. JSON: done, and once it is, the exit status and
    # the run it journaled (T-068): outcome, what it did as `diskmap journal` says it, the
    # restore commands when it was partially applied — null when nothing was run;
    # journal_error instead when the journal cannot be read.
    terminal_result: Callable[[str], str]
    # Whether a partition (diskNsM) can be shrunk, before a size is named (T-069): JSON with its
    # size and the least it can be shrunk to, as diskutil's resize limits say; Declined with
    # the planner's reason when it cannot.
    limits: Callable[[str], str]
    # Mount or unmount a partition (T-073, K16): `diskmap mount|unmount` in a process of its
    # own. JSON with what it printed; Declined with that when it failed.
    mount: Callable[[MountRequest], str]


def parse_mount_request(body: Any) -> MountRequest:
    """BODY, the JSON of a mount request: {"slice": "diskNsM", "mount": true|false}."""
    if not isinstance(body, dict) or set(body) != {"slice", "mount"}:
        raise BadRequest('expected {"slice": "diskNsM", "mount": true or false}')
    if not isinstance(body["slice"], str) or not SLICE.fullmatch(body["slice"]):
        raise BadRequest("slice: expected diskNsM")
    if not isinstance(body["mount"], bool):
        raise BadRequest("mount: expected true or false")
    return MountRequest(body["slice"], body["mount"])


def parse_plan_request(body: Any) -> PlanRequest:
    """BODY, the JSON of a plan request, checked: what it does not say plainly is refused,
    never guessed (§S9)."""
    if not isinstance(body, dict):
        raise BadRequest("expected a JSON object")
    unknown = set(body) - {"kind", "disk", "layout", "to", "after"}
    if unknown:
        raise BadRequest(f"unknown field(s): {', '.join(sorted(unknown))}")
    kind, disk, layout, to, after = (body.get(k) for k in ("kind", "disk", "layout", "to", "after"))
    if kind not in KINDS:
        raise BadRequest(f"kind: expected one of {', '.join(KINDS)}")
    if not isinstance(disk, str) or not (SLICE if kind in RESIZE else WHOLE).fullmatch(disk):
        raise BadRequest("disk: expected diskNsM" if kind in RESIZE else "disk: expected diskN")
    if kind in RESIZE:
        if not isinstance(to, str) or not to.strip():
            raise BadRequest(f"to: {kind} needs the new size")
    elif to is not None:
        raise BadRequest("to: only shrink and grow take a size")
    if kind in ("repartition", "add") and layout is None:
        raise BadRequest(f"layout: {kind} needs a layout")
    if kind in ("backup-map", "grow") and layout is not None:
        raise BadRequest(f"layout: {kind} takes none")
    if after is not None:
        if kind != "add":
            raise BadRequest("after: only add takes a partition to follow")
        if not isinstance(after, str) or not SLICE.fullmatch(after) or not after.startswith(f"{disk}s"):
            raise BadRequest(f"after: expected a partition of {disk}, {disk}sM")
    return PlanRequest(kind, disk, layout, to, after)


def core_of(search_path: str, home: str, width: int = 100) -> Core:
    """The real core: probes the system on every call, as `show` and `plan` do."""
    from cli import plan as cli_plan
    from cli import show
    from cli.formats import read_formats
    from cli.preview import plan_json
    from core.probe.errors import ProbeError
    from core.probe.show import read_map

    def disk_map() -> str:
        try:
            return show.to_json(read_map())
        except ProbeError as e:
            raise Declined(str(e)) from None

    def formats() -> str:
        try:
            return read_formats(search_path, home)
        except (ProbeError, OSError) as e:
            raise Declined(str(e)) from None

    def plan(r: PlanRequest) -> str:
        at = datetime.now(UTC).astimezone()
        try:
            if r.kind == "backup-map":
                built = cli_plan.build_backup(r.disk, home, width, at)
            elif r.kind == "shrink":
                assert r.to is not None
                built = cli_plan.build_shrink(r.disk, r.to, r.layout, search_path, home, width, at)
            elif r.kind == "grow":
                assert r.to is not None
                built = cli_plan.build_grow(r.disk, r.to, home, width, at)
            else:
                built = cli_plan.build(r.kind, r.disk, r.layout, search_path, home, width, at, after=r.after)
        except (cli_plan.Refused, ProbeError, OSError) as e:
            raise Declined(str(e)) from None
        return plan_json(built.plan, built.preview, built.untested)

    def limits(slice_name: str) -> str:
        try:
            s = cli_plan.resize_limits(slice_name, width)
        except (cli_plan.Refused, ProbeError, OSError) as e:
            raise Declined(str(e)) from None
        doc = {
            "slice": s.slice,
            "disk": s.disk,
            "apfs": s.apfs,
            "current": s.current,
            "minimum": s.minimum,
            "maximum": s.maximum,
        }
        return json.dumps(doc)

    def terminal(r: PlanRequest, open_it: bool) -> str:
        from ui import terminal as T

        if r.kind not in T.KINDS:
            raise Declined(f"{r.kind} is applied from the command line only")
        try:
            h = T.prepare(r.kind, r.disk, r.layout, home, datetime.now(UTC).astimezone(), to=r.to, after=r.after)
        except (T.HandoffError, OSError) as e:
            raise Declined(str(e)) from None
        error = None
        if open_it:
            try:
                T.open_terminal(h.script)
            except T.HandoffError as e:
                error = str(e)
        doc = {"id": h.id, "command_line": h.command_line, "script": h.script, "layout": h.layout}
        return json.dumps(doc | {"opened": open_it and error is None, "error": error}, ensure_ascii=False)

    def terminal_result(handoff: str) -> str:
        from cli.journal import details
        from core.executor.journal import PARTIAL
        from ui import terminal as T

        try:
            status = T.result(home, handoff)
        except T.HandoffError as e:
            raise Declined(str(e)) from None
        if status is None:
            return json.dumps({"done": False, "status": None})
        try:
            r = T.journaled(home, handoff)
        except T.HandoffError as e:
            return json.dumps({"done": True, "status": status, "run": None, "journal_error": str(e)})
        run = None
        if r is not None:
            run = {
                "id": r.run,
                "outcome": r.outcome,
                "details": details(r),
                # Not applied: the map is the one before, there is nothing to put back.
                "restore": [f"diskmap restore-map {b} {r.disk}" for b in r.backups] if r.outcome == PARTIAL else [],
            }
        return json.dumps({"done": True, "status": status, "run": run, "journal_error": None}, ensure_ascii=False)

    def mount(r: MountRequest) -> str:
        from ui.terminal import ROOT

        verb = "mount" if r.mount else "unmount"
        try:
            # Its own session: a signal to the server's group is not the step's (D44).
            done = subprocess.run(
                [sys.executable, "-m", "cli", verb, r.slice],
                cwd=ROOT,
                env=os.environ | {"PATH": search_path},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=MOUNT_TIMEOUT,
                start_new_session=True,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Declined(f"{verb}: {e}") from None
        output = (done.stdout + done.stderr).strip()
        if done.returncode != 0:
            raise Declined(output or f"diskmap {verb} exited {done.returncode}")
        return json.dumps({"output": output}, ensure_ascii=False)

    return Core(disk_map, formats, plan, terminal, terminal_result, limits, mount)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, core: Core, token: str, port: int = 0) -> None:
        self.core = core
        self.token = token
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def origin(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"

    @property
    def link(self) -> str:
        """The one link that lets a browser in: it sets the cookie and goes to /."""
        return f"http://{self.origin}/?t={self.token}"


def serve(core: Core, port: int = 0) -> Server:
    return Server(core, secrets.token_urlsafe(32), port)


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "diskmap"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        pass  # the terminal belongs to the person who started the server

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        url = urlsplit(self.path)
        if url.path == "/" and "t" in parse_qs(url.query):
            given = parse_qs(url.query)["t"][0]
            if not hmac.compare_digest(given.encode(), self.server.token.encode()):
                return self._text(HTTPStatus.FORBIDDEN, "This link is not from the running diskmap ui.")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Strict")
            self._security()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if url.path == "/":
            if not self._cookie_ok():
                return self._text(HTTPStatus.FORBIDDEN, "Open the link `diskmap ui` printed in the terminal.")
            page = (STATIC / "index.html").read_text(encoding="utf-8")
            page = page.replace(TOKEN_SLOT, self.server.token).replace(VERSION_SLOT, VERSION)
            return self._send(HTTPStatus.OK, "text/html; charset=utf-8", page.encode())
        if url.path in FILES:
            return self._send(HTTPStatus.OK, FILES[url.path], (STATIC / url.path.removeprefix("/static/")).read_bytes())
        if url.path == "/api/map":
            if not self._token_ok():
                return
            return self._call(self.server.core.disk_map)
        if url.path == "/api/formats":
            if not self._token_ok():
                return
            return self._call(self.server.core.formats)
        if url.path == "/api/terminal/result":
            if not self._token_ok():
                return
            ids = parse_qs(url.query).get("id", [])
            if len(ids) != 1:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "id: expected one"})
            return self._call(lambda: self.server.core.terminal_result(ids[0]))
        if url.path == "/api/limits":
            if not self._token_ok():
                return
            slices = parse_qs(url.query).get("slice", [])
            if len(slices) != 1 or not SLICE.fullmatch(slices[0]):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "slice: expected one diskNsM"})
            return self._call(lambda: self.server.core.limits(slices[0]))
        self._text(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self) -> None:
        if not self._host_ok():
            return
        path = urlsplit(self.path).path
        if path not in ("/api/plan", "/api/terminal", "/api/handoff", "/api/mount"):
            return self._text(HTTPStatus.NOT_FOUND, "Not found.")
        if not self._token_ok():
            return
        if self.headers.get_content_type() != "application/json":
            return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "expected application/json"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length required"})
        if not 0 <= length <= MAX_BODY:
            return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
        parse = parse_mount_request if path == "/api/mount" else parse_plan_request
        try:
            request = parse(json.loads(self.rfile.read(length)))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "not JSON"})
        except BadRequest as e:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
        core = self.server.core
        if isinstance(request, MountRequest):
            self._call(lambda: core.mount(request))
        elif path == "/api/plan":
            self._call(lambda: core.plan(request))
        else:  # /api/handoff writes the files only: the command is copied and run by hand
            self._call(lambda: core.terminal(request, path == "/api/terminal"))

    def do_OPTIONS(self) -> None:  # a CORS preflight is never answered yes
        self._text(HTTPStatus.FORBIDDEN, "No cross-origin access.")

    def _host_ok(self) -> bool:
        if self.headers.get("Host") == self.server.origin:
            return True
        self._text(HTTPStatus.MISDIRECTED_REQUEST, "Wrong host.")
        return False

    def _cookie_ok(self) -> bool:
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        given = jar[COOKIE].value if COOKIE in jar else ""
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def _token_ok(self) -> bool:
        given = self.headers.get(HEADER, "")
        if hmac.compare_digest(given.encode(), self.server.token.encode()):
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong token"})
        return False

    def _call(self, fn: Callable[[], str]) -> None:
        try:
            body = fn()
        except Declined as e:
            return self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(e)})
        self._send(HTTPStatus.OK, "application/json; charset=utf-8", body.encode())

    def _json(self, status: HTTPStatus, doc: dict[str, Any]) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(doc, ensure_ascii=False).encode())

    def _text(self, status: HTTPStatus, text: str) -> None:
        self._send(status, "text/plain; charset=utf-8", (text + "\n").encode())

    def _security(self) -> None:
        for name, value in SECURITY.items():
            self.send_header(name, value)

    def _send(self, status: HTTPStatus, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security()
        self.end_headers()
        self.wfile.write(body)
