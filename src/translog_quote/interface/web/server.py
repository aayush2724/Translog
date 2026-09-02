"""The POC web server. Standard library only, local by default.

One `DemoSession` per process, three whitelisted static files, and a fixed set
of demonstration actions. There is deliberately nothing else: no upload, no
redirect, no proxying, no path arithmetic on request URLs — a request either
matches an entry in a literal table or it is a 404.

Credentials cannot reach the browser through this module. Handlers only ever
serialise pipeline outcomes via `serialize`, which cannot see `Settings`, and
the static files are committed source with no templating step to leak into.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from translog_quote.domain.quotation import NotADecision
from translog_quote.errors import TranslogError
from translog_quote.interface.web import live_serialize, serialize
from translog_quote.interface.web.live_poller import LivePoller
from translog_quote.interface.web.live_session import (
    LiveSequenceError,
    LiveSession,
    build_live_session,
)
from translog_quote.interface.web.session import DemoSequenceError, DemoSession
from translog_quote.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from translog_quote.config import Settings

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_log = get_logger("interface.web.server")

_STATIC_DIR = Path(__file__).resolve().parent / "static"

#: The whole static surface. A literal table, not a directory walk: a path is
#: served because it is named here, so traversal has nothing to traverse.
_STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

#: The live view's own files. A second table rather than entries in the first,
#: so the scripted POC's static surface is unchanged and the "closed whitelist"
#: test keeps meaning exactly what it meant.
_LIVE_FILES: dict[str, tuple[str, str]] = {
    "/": ("live.html", "text/html; charset=utf-8"),
    "/live.html": ("live.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/live.css": ("live.css", "text/css; charset=utf-8"),
    "/live.js": ("live.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

#: Every action a browser may take. Each advances the session through the same
#: methods the tests drive; the approval boundary lives in the session and the
#: workflow behind it, never in this table.
_ACTIONS: dict[str, Callable[[DemoSession], None]] = {
    "approve-clarification": DemoSession.approve_clarification,
    "receive-reply": DemoSession.receive_reply,
    "search-rates": DemoSession.search_rates,
    "approve-quotation": DemoSession.acknowledge_quotation,
}

#: The largest body a live action may send. A decision is a few dozen bytes; a
#: ceiling keeps a stray request from being read into memory unbounded.
_MAX_BODY_BYTES = 4096


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _selected(query: str) -> str | None:
    """The `request_id` from a query string, if there is exactly one.

    Parsed rather than pattern-matched, and never used to build a path: it is
    a dictionary key on the session and nothing else.
    """
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == "request_id" and value:
            return unquote(value)
    return None


def _live_poll(session: LiveSession, body: dict[str, object]) -> None:
    """Read the mailbox once. Reads and processes; sends nothing.

    The same call `LivePoller` makes on its timer, which is what actually
    drives the demonstration — no page offers this and nobody has to invoke it.
    It stays reachable because it is the one live action with no side effect
    outside the session, and it is how the flow is driven end to end over real
    HTTP in the tests.
    """
    session.poll()


def _live_approve_clarification(session: LiveSession, body: dict[str, object]) -> None:
    """Release a held clarification. Requires a named person; no default.

    `request_id` says which draft. The page has always sent it and this handler
    used to drop it on the floor, which is how approving the request on screen
    could mail a different client about a different shipment.
    """
    session.approve_clarification(
        by=str(body.get("by", "")),
        request_id=_str_or_none(body.get("request_id")),
    )


def _live_decide(session: LiveSession, body: dict[str, object]) -> None:
    """Apply one human decision to the quotation gate.

    Every value comes from the request body and none has a default. An absent
    or unrecognised `decision` reaches `decision_from_choice`, which raises
    rather than resolving to either outcome — so a malformed request can no
    more approve a quotation than it can decline one.
    """
    request_id = _str_or_none(body.get("request_id"))
    if request_id is None:
        raise LiveSequenceError("A decision must name the request it applies to.")
    session.decide(
        request_id,
        choice=str(body.get("decision", "")),
        by=str(body.get("by", "")),
        reason=str(body.get("reason", "")),
    )


#: Every live action a browser may take. A literal table, like the static one.
_LIVE_ACTIONS: dict[str, Callable[[LiveSession, dict[str, object]], None]] = {
    "poll": _live_poll,
    "clarification/approve": _live_approve_clarification,
    "quotation/decide": _live_decide,
}


_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)


class DemoServer(ThreadingHTTPServer):
    """The HTTP server plus the single demonstration session it serves.

    Two modes, one server. The scripted POC (`live_session=None`) is unchanged.
    In live mode the same process additionally serves the real-Gmail view; the
    session it drives is built by the caller, so a misconfigured credential
    fails before anything binds a port rather than as a 500 in front of a room.
    """

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        settings: Settings | None = None,
        *,
        live_session: LiveSession | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        super().__init__(address, DemoRequestHandler)
        self._settings = settings
        self.lock = threading.Lock()
        self.session = DemoSession(settings)
        self.live = live_session
        # The live view has no "check mail": the mailbox is read here, on a
        # timer, under the same lock the request handlers take. Started only
        # when an interval is given, so a test that wants to drive `poll()`
        # itself gets a server that reads nothing behind its back.
        self.poller: LivePoller | None = None
        if live_session is not None and poll_interval_seconds is not None:
            self.poller = LivePoller(
                live_session, lock=self.lock, interval_seconds=poll_interval_seconds
            )
            self.poller.start()

    @property
    def is_live(self) -> bool:
        return self.live is not None

    def reset_session(self) -> None:
        self.session = DemoSession(self._settings)

    def server_close(self) -> None:
        """Stop polling and release the mailbox connections, then close.

        Ordered: the poller first, so nothing is mid-request when the clients
        it uses are closed underneath it.
        """
        if self.poller is not None:
            self.poller.stop()
        if self.live is not None:
            self.live.close()
        super().server_close()


#: Host header values a browser may legitimately send to a loopback server.
#: Anything else is a DNS-rebinding attempt: an attacker domain resolved to
#: 127.0.0.1 so a page it serves can reach this server from the victim's
#: browser. The port is appended at check time.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")

#: Hostnames this server is additionally allowed to be reached under, as a
#: comma-separated list. Empty by default, which leaves the guard exactly as it
#: has always been for a local run.
#:
#: It exists because the guard above is written for a loopback-only server, and
#: a hosted one is reached by name: every state-changing POST to a deployed
#: hostname is refused until that name is declared here. Declaring it is
#: deliberately an explicit act — an allowlist, never a wildcard and never
#: "trust whatever Host arrives", because the check would then defend nothing.
ALLOWED_HOSTS_VAR = "TRANSLOG_ALLOWED_HOSTS"


def _allowed_hosts() -> frozenset[str]:
    """Loopback, plus whatever names the operator has declared."""
    declared = os.environ.get(ALLOWED_HOSTS_VAR, "")
    return frozenset(_LOOPBACK_HOSTS) | {
        name.strip().lower() for name in declared.split(",") if name.strip()
    }


class DemoRequestHandler(BaseHTTPRequestHandler):
    server_version = "TranslogPOC/0.1"

    @property
    def _demo(self) -> DemoServer:
        assert isinstance(self.server, DemoServer)
        return self.server

    # ------------------------------------------------------------- routing --

    def do_GET(self) -> None:  # noqa: N802 - fixed by http.server
        raw_path, _, query = self.path.partition("?")
        path = raw_path

        if path == "/api/state":
            with self._demo.lock:
                self._send_json(serialize.snapshot(self._demo.session))
            return

        if path == "/api/live/state":
            live = self._demo.live
            if live is None:
                self._send_json({"error": "not found"}, status=404)
                return
            with self._demo.lock:
                self._send_json(live_serialize.snapshot(live, selected=_selected(query)))
            return

        table = _LIVE_FILES if self._demo.is_live else _STATIC_FILES
        static = table.get(path)
        if static is None:
            self._send_json({"error": "not found"}, status=404)
            return
        filename, content_type = static
        self._send_bytes((_STATIC_DIR / filename).read_bytes(), content_type)

    def _rejects_cross_site(self) -> bool:
        """Whether this request must be refused as not same-origin.

        Two cheap, standard defences for a localhost-only server, and nothing
        that a legitimate same-origin `fetch` from the served page ever trips:

        - **Host must be loopback, or a name the operator declared** in
          `TRANSLOG_ALLOWED_HOSTS`. Defeats DNS rebinding, where an attacker
          domain resolves to 127.0.0.1 so its page can reach this server.
        - **State-changing POSTs must be `application/json`.** A cross-site HTML
          form can only send `text/plain`, form-encoded or multipart bodies
          without provoking a CORS preflight; this server sends no CORS headers,
          so a preflight fails and the browser never sends the real request.
          Requiring JSON therefore turns away the one CSRF shape that needs no
          preflight. The page's own `fetch` sets this header already.

        Refused requests get a 403 with a fixed body — the reason is not worth
        teaching an attacker to satisfy.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip().lower()
        if host not in _allowed_hosts():
            return True
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return content_type != "application/json"

    def do_POST(self) -> None:  # noqa: N802 - fixed by http.server
        if self._rejects_cross_site():
            self._send_json({"error": "forbidden"}, status=403)
            return
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/live/"):
            self._do_live_post(path.removeprefix("/api/live/"))
            return

        prefix = "/api/action/"
        if not path.startswith(prefix):
            self._send_json({"error": "not found"}, status=404)
            return

        name = path.removeprefix(prefix)
        try:
            with self._demo.lock:
                if name == "reset":
                    self._demo.reset_session()
                elif name in _ACTIONS:
                    _ACTIONS[name](self._demo.session)
                else:
                    self._send_json({"error": f"unknown action: {name}"}, status=404)
                    return
                self._send_json(serialize.snapshot(self._demo.session))
        except DemoSequenceError as exc:
            self._send_json({"error": str(exc)}, status=409)
        except TranslogError as exc:
            # The class of failure, never its contents: adapter messages can
            # carry provider detail that does not belong in a browser.
            #
            # Logged in full on the way past, though. Redacting the browser's
            # copy is the point; redacting the operator's too left a 500 whose
            # only trace was the access-log line, and the sentence naming what
            # actually broke was discarded at exactly the moment somebody
            # needed it.
            _log.warning("Live action %s failed: %s", name, exc)
            self._send_json({"error": type(exc).__name__}, status=500)

    # ---------------------------------------------------------------- live --

    def _do_live_post(self, name: str) -> None:
        """The three live actions. Each is a person doing something.

        `poll` reads the mailbox and sends nothing. `clarification/approve` and
        `quotation/decide` are the two human gates, and neither has a default:
        both require a named person in the request body, and the decision is
        applied by the existing pipeline, not by this handler.
        """
        live = self._demo.live
        if live is None or name not in _LIVE_ACTIONS:
            self._send_json({"error": "not found"}, status=404)
            return

        try:
            body = self._read_json()
        except ValueError:
            self._send_json({"error": "request body must be a JSON object"}, status=400)
            return

        try:
            with self._demo.lock:
                _LIVE_ACTIONS[name](live, body)
                self._send_json(
                    live_serialize.snapshot(live, selected=_str_or_none(body.get("request_id")))
                )
        except (LiveSequenceError, NotADecision) as exc:
            # A client error: the operator asked for something out of order, or
            # sent something that is not a decision. Its text is written for a
            # person and carries no provider detail.
            self._send_json({"error": str(exc)}, status=409)
        except TranslogError as exc:
            # The class of failure, never its contents: adapter messages can
            # carry provider detail that does not belong in a browser.
            self._send_json({"error": type(exc).__name__}, status=500)
        except Exception as exc:  # noqa: BLE001 - the boundary of the process
            # Not every failure below is a TranslogError — `NotADecision` and
            # the pydantic validation errors raised by domain types are plain
            # ValueErrors. Left uncaught one escapes as an empty 500 with
            # no JSON body, the browser's `response.json()` throws, and the
            # page reports that the server did not respond — which is both
            # wrong and unactionable. The class name is safe and says enough.
            _log.exception("Unhandled failure in live action %s", name)
            self._send_json({"error": type(exc).__name__}, status=500)

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _MAX_BODY_BYTES:
            raise ValueError("body too large")
        loaded = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("body is not an object")
        return loaded

    # ----------------------------------------------------------- responses --

    def _send_json(self, payload: dict[str, object], *, status: int = 200) -> None:
        self._send_bytes(
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def _send_bytes(self, body: bytes, content_type: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for header, value in _SECURITY_HEADERS:
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)


def run(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    settings: Settings | None = None,
    *,
    live: bool = False,
) -> int:
    """Serve the demonstration until interrupted.

    `live` swaps the scripted scenario for the real Gmail workflow. The session
    is built *before* the port is bound, so a missing credential or approver
    address stops the process with a readable sentence rather than serving a
    page that fails on its first click.

    Building it also fixes the demonstration's cutoff at this moment and starts
    the background poller. Between them that is the whole of "open the
    dashboard and send an enquiry": the mailbox's history is out of scope
    before the first read, and every read after that happens on its own.
    """
    live_session = None
    interval: float | None = None
    if live:
        from translog_quote.config import load_settings

        live_settings = settings or load_settings()
        try:
            live_session = build_live_session(live_settings)
        except TranslogError as exc:
            print(f"Cannot start the live demo: {exc}")
            return 2
        interval = live_settings.demo.poll_interval_seconds

    with DemoServer(
        (host, port), settings, live_session=live_session, poll_interval_seconds=interval
    ) as server:
        if live_session is None or interval is None:
            # The scripted POC. Both are set together or not at all, so this
            # reads as one condition rather than two: there is no live session
            # without the poll that drives it.
            print(f"Translog POC — http://{host}:{port}/")
            print("  Rates: demo data (no WebCargo request is made)")
            print("  Email: not connected (drafts only; nothing can send)")
        else:
            print(f"Translog LIVE — http://{host}:{port}/")
            print("  Inbound:  real Gmail (read-only credential)")
            print("  Outbound: real Gmail (separate send-only credential)")
            print("  Rates:    SIMULATED WEBCARGO DATA — DEMO ONLY")
            print(f"  Approver: {live_session.approver_address}")
            print("  Approval: human — nothing sends without an explicit click")
            print(f"  Mailbox:  read automatically every {interval:g}s — no button to press")
            print("  Scope:    mail that arrives from now on; the inbox's history is ignored")
        print("  Ctrl+C stops the server.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0
