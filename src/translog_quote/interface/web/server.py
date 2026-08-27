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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from translog_quote.errors import TranslogError
from translog_quote.interface.web import serialize
from translog_quote.interface.web.session import DemoSequenceError, DemoSession

if TYPE_CHECKING:
    from collections.abc import Callable

    from translog_quote.config import Settings

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_STATIC_DIR = Path(__file__).resolve().parent / "static"

#: The whole static surface. A literal table, not a directory walk: a path is
#: served because it is named here, so traversal has nothing to traverse.
_STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
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
    """The HTTP server plus the single demonstration session it serves."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], settings: Settings | None = None) -> None:
        super().__init__(address, DemoRequestHandler)
        self._settings = settings
        self.lock = threading.Lock()
        self.session = DemoSession(settings)

    def reset_session(self) -> None:
        self.session = DemoSession(self._settings)


class DemoRequestHandler(BaseHTTPRequestHandler):
    server_version = "TranslogPOC/0.1"

    @property
    def _demo(self) -> DemoServer:
        assert isinstance(self.server, DemoServer)
        return self.server

    # ------------------------------------------------------------- routing --

    def do_GET(self) -> None:  # noqa: N802 - fixed by http.server
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            with self._demo.lock:
                self._send_json(serialize.snapshot(self._demo.session))
            return
        static = _STATIC_FILES.get(path)
        if static is None:
            self._send_json({"error": "not found"}, status=404)
            return
        filename, content_type = static
        self._send_bytes((_STATIC_DIR / filename).read_bytes(), content_type)

    def do_POST(self) -> None:  # noqa: N802 - fixed by http.server
        path = self.path.split("?", 1)[0]
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
            self._send_json({"error": type(exc).__name__}, status=500)

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
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, settings: Settings | None = None
) -> int:
    """Serve the demonstration until interrupted."""
    with DemoServer((host, port), settings) as server:
        print(f"Translog POC — http://{host}:{port}/")
        print("  Rates: demo data (no WebCargo request is made)")
        print("  Email: not connected (drafts only; nothing can send)")
        print("  Ctrl+C stops the server.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0
