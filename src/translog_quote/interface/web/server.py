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

import hmac
import http.cookies
import json
import os
import threading
import time
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

#: The sign-in surface. Served WITHOUT authentication — it is how an operator
#: obtains the session in the first place — so it is a third table checked
#: before the auth gate, never merged into the gated ones. It carries no client
#: data: a static form, its stylesheets and script, and the shared brand icon.
#: `/app.css` is included because the login page reuses the dashboard's design
#: tokens and brand styles from it — WITHOUT it, every `var(--…)` on the page
#: falls back to nothing and the sign-in card renders unstyled. It is pure
#: public styling (committed source, no client data), so exposing it pre-auth is
#: safe. Every asset here is committed source; the CSP forbids inline
#: script/style, so the page's behaviour lives in `/login.js`, same origin.
_PUBLIC_FILES: dict[str, tuple[str, str]] = {
    "/login": ("login.html", "text/html; charset=utf-8"),
    "/login.html": ("login.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/login.css": ("login.css", "text/css; charset=utf-8"),
    "/login.js": ("login.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

#: The HTML pages a browser navigates to (as opposed to `fetch`es). An
#: unauthenticated navigation is answered with a redirect to the sign-in page;
#: an unauthenticated `fetch` (everything else) is answered with a 401 the
#: dashboard script turns into the same redirect. Both are the SAME table the
#: gated static views serve, named here so the two rejection styles stay in one
#: place and never diverge.
_PAGE_PATHS: frozenset[str] = frozenset({"/", "/index.html", "/live.html"})

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


def _live_decide_goods_type(session: LiveSession, body: dict[str, object]) -> None:
    """Record an operator's Goods Type pick for a held request.

    Names the request, the chosen catalog label, and — exactly like
    `quotation/decide` — the operator (`by`). The session refuses a label the
    catalog does not offer and an unnamed operator; nothing here defaults either.
    """
    request_id = _str_or_none(body.get("request_id"))
    if request_id is None:
        raise LiveSequenceError("A goods-type decision must name the request it applies to.")
    session.decide_goods_type(
        request_id,
        goods_type=str(body.get("goods_type", "")),
        by=str(body.get("by", "")),
    )


#: Every live action a browser may take. A literal table, like the static one.
_LIVE_ACTIONS: dict[str, Callable[[LiveSession, dict[str, object]], None]] = {
    "poll": _live_poll,
    "clarification/approve": _live_approve_clarification,
    "quotation/decide": _live_decide,
    "goods-type/decide": _live_decide_goods_type,
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


#: A shared secret that, when set, gates every dashboard route behind the
#: sign-in page. Read from the environment like the host allowlist above rather
#: than threaded through `Settings`, so the guard is decided in one place and
#: the value is never copied into a serialised snapshot. Empty or unset means
#: "no password" — which is refused for a non-loopback live bind (see `run`), so
#: an exposed dashboard cannot be left open by omission. The Host allowlist
#: stops a cross-origin browser; this stops anyone who simply knows the URL. The
#: token doubles as the HMAC key that signs session cookies (`_mint_session`),
#: so rotating it silently invalidates every session issued under the old one.
DASHBOARD_TOKEN_VAR = "TRANSLOG_DASHBOARD_TOKEN"  # noqa: S105 - env var name, not a secret

#: The session cookie's name and lifetime. A finite expiry is signed *into* the
#: cookie (see `_mint_session`), so an operator re-authenticates once a day
#: rather than holding an indefinite credential.
_SESSION_COOKIE = "translog_session"  # noqa: S105 - cookie name, not a secret
_SESSION_TTL_SECONDS = 12 * 3600


def _dashboard_token() -> str | None:
    """The configured dashboard password, or None when auth is disabled."""
    return os.environ.get(DASHBOARD_TOKEN_VAR, "").strip() or None


def _password_matches(submitted: str, token: str) -> bool:
    """Whether a submitted access key equals the token, in constant time.

    The username is cosmetic (the token is the whole secret), so only the key is
    checked, and `compare_digest` keeps a wrong one from leaking a timing signal.
    The credential is never logged. The submitted key is `.strip()`-ed to match
    the env token, which `_dashboard_token()` already strips — so a key pasted
    with a trailing newline or stray space (the common copy-paste footgun) still
    matches, and no legitimate key is rejected (the real secret can carry no
    surrounding whitespace, having been stripped on the env side).
    """
    return hmac.compare_digest(submitted.strip().encode("utf-8"), token.encode("utf-8"))


def _sign(payload: str, token: str) -> str:
    """The HMAC-SHA256 of `payload` under the dashboard token, hex-encoded."""
    return hmac.new(token.encode("utf-8"), payload.encode("utf-8"), "sha256").hexdigest()


def _mint_session(token: str, *, now: float | None = None) -> str:
    """A signed, stateless session value: `"<expiry-epoch>.<hmac>"`.

    No server-side store: the cookie carries its own expiry and a signature the
    server can verify but a client cannot forge without the token. Tampering
    with the expiry breaks the signature; the secret never leaves the process.
    """
    issued = int(now if now is not None else time.time())
    payload = str(issued + _SESSION_TTL_SECONDS)
    return f"{payload}.{_sign(payload, token)}"


def _session_is_valid(value: str | None, token: str, *, now: float | None = None) -> bool:
    """Whether a session cookie value is authentic and unexpired.

    Authentic: its signature recomputes under the current token (a rotated
    token, a tampered payload, or a forged signature all fail `compare_digest`).
    Unexpired: the signed expiry is still in the future. Nothing is trusted from
    the cookie until the signature has been verified.
    """
    if not value:
        return False
    payload, separator, signature = value.rpartition(".")
    if not separator or not hmac.compare_digest(signature, _sign(payload, token)):
        return False
    try:
        expiry = int(payload)
    except ValueError:
        return False
    return (now if now is not None else time.time()) < expiry


def _cookie_value(header: str | None, name: str) -> str | None:
    """The value of one cookie from a request `Cookie` header, or None.

    Parsed with the stdlib cookie jar rather than split by hand, and a
    malformed header yields None rather than raising into the request loop.
    """
    if not header:
        return None
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(header)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


def _set_session_header(value: str) -> str:
    """A `Set-Cookie` line establishing the session: HttpOnly, Secure,
    SameSite=Strict, Path=/, and the same finite Max-Age the value is signed
    with. `Secure` is kept even for local use — modern browsers treat
    http://localhost as a secure context, and local dev runs tokenless anyway."""
    return (
        f"{_SESSION_COOKIE}={value}; HttpOnly; Secure; SameSite=Strict; "
        f"Path=/; Max-Age={_SESSION_TTL_SECONDS}"
    )


def _clear_session_header() -> str:
    """A `Set-Cookie` line that expires the session immediately (logout)."""
    return f"{_SESSION_COOKIE}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"


def _binds_publicly(host: str) -> bool:
    """Whether this bind address is reachable from off the machine.

    Loopback is the only address that is not off-box. The all-interfaces
    wildcards (`0.0.0.0`, `::`) *include* loopback but also every other
    interface, so they count as public; a LAN or public IP likewise. Anything
    that is not plainly loopback must not be served without a password.
    """
    cleaned = host.strip().lower().strip("[]")
    is_loopback = cleaned in {"127.0.0.1", "localhost", "::1"} or cleaned.startswith("127.")
    return not is_loopback


class DemoRequestHandler(BaseHTTPRequestHandler):
    server_version = "TranslogPOC/0.1"

    @property
    def _demo(self) -> DemoServer:
        assert isinstance(self.server, DemoServer)
        return self.server

    def _authenticated(self) -> bool:
        """Whether this request carries a valid session.

        No password configured means auth is off — the local-development case,
        which a non-loopback live bind is separately refused (see `run`), so this
        can only be reached open on loopback. When a password is set, every route
        — data reads and state-changing actions alike — requires a signed,
        unexpired session cookie; there is no Basic-auth header path, so the
        browser's native username/password popup can no longer be provoked.
        """
        token = _dashboard_token()
        if token is None:
            return True
        return _session_is_valid(_cookie_value(self.headers.get("Cookie"), _SESSION_COOKIE), token)

    def _readiness(self) -> tuple[dict[str, object], int]:
        """Readiness: the dependencies this process needs are reachable.

        Demo/mock modes need nothing external, so ready is immediate. In browser
        mode the dashboard enqueues to Redis, so readiness pings it — a probe
        touches the shared queue, so keep the interval sane. Reports, never
        raises: an unreachable queue is a 503, not a traceback."""
        from translog_quote.config import WebCargoMode, load_settings

        settings = load_settings()
        mode = settings.webcargo.mode
        if mode is not WebCargoMode.BROWSER:
            return {"status": "ready", "mode": mode.value}, 200
        try:
            from redis import Redis

            Redis.from_url(
                settings.queue.redis_url, socket_connect_timeout=5, socket_timeout=5
            ).ping()
        except Exception as exc:  # noqa: BLE001 - readiness reports failure as 503
            from translog_quote.observability import get_logger

            get_logger("interface.web").warning(
                "readiness: redis unreachable (%s)", type(exc).__name__
            )
            return {
                "status": "not ready",
                "mode": mode.value,
                "redis": "unreachable",
            }, 503
        return {"status": "ready", "mode": mode.value, "redis": "ok"}, 200

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802 - http.server API
        """Route http.server's access log through the structured logger rather
        than stderr, so a deployed run's request lines land with everything
        else the app logs."""
        from translog_quote.observability import get_logger

        get_logger("interface.web").info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------- routing --

    def do_GET(self) -> None:  # noqa: N802 - fixed by http.server
        raw_path, _, query = self.path.partition("?")
        path = raw_path

        # Health probes are unauthenticated on purpose: a monitor carries no
        # dashboard password, and these expose no client data.
        if path == "/health":
            self._send_json({"status": "ok"})
            return
        if path == "/health/ready":
            payload, status = self._readiness()
            self._send_json(payload, status=status)
            return

        # The sign-in surface is served WITHOUT auth — it is how a session is
        # obtained. A closed table, exactly like the gated ones, so this opens
        # no path beyond the login page, its assets and the brand icon.
        public = _PUBLIC_FILES.get(path)
        if public is not None:
            filename, content_type = public
            self._send_bytes((_STATIC_DIR / filename).read_bytes(), content_type)
            return

        if not self._authenticated():
            self._reject_unauthenticated(path)
            return

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
        path = self.path.split("?", 1)[0]

        # Sign-in and sign-out come before the auth gate — one grants a session,
        # the other clears it — but still pass the same cross-site guard every
        # other POST does, so neither can be driven from a foreign origin.
        if path == "/login":
            self._do_login()
            return
        if path == "/logout":
            self._do_logout()
            return

        if not self._authenticated():
            # A 401 with NO `WWW-Authenticate` header: the dashboard's own
            # fetch layer redirects to /login on this, and the browser never
            # shows a native credential popup.
            self._send_json({"error": "unauthorized"}, status=401)
            return
        if self._rejects_cross_site():
            self._send_json({"error": "forbidden"}, status=403)
            return
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

    # --------------------------------------------------------------- sign-in --

    def _do_login(self) -> None:
        """Establish a session from a submitted password, or refuse it.

        Same-origin only (the cross-site guard), JSON body only. When no token
        is configured auth is off (loopback dev), so any sign-in succeeds
        without a cookie — there is nothing to protect. When a token is set, the
        password is compared in constant time and a match mints a signed
        session cookie; a mismatch is a plain 401 carrying NO `WWW-Authenticate`
        header, so the browser never shows its native popup. The username is
        read but cosmetic — the token is the whole secret — and neither field is
        ever logged.
        """
        if self._rejects_cross_site():
            self._send_json({"error": "forbidden"}, status=403)
            return
        try:
            body = self._read_json()
        except ValueError:
            self._send_json({"error": "request body must be a JSON object"}, status=400)
            return

        token = _dashboard_token()
        if token is None:
            self._send_json({"ok": True})
            return

        password = body.get("password")
        if not isinstance(password, str) or not _password_matches(password, token):
            self._send_json({"error": "invalid credentials"}, status=401)
            return
        self._send_json({"ok": True}, set_cookie=_set_session_header(_mint_session(token)))

    def _do_logout(self) -> None:
        """Clear the session cookie. Same-origin only; always succeeds, so a
        page can sign out without first proving it was signed in."""
        if self._rejects_cross_site():
            self._send_json({"error": "forbidden"}, status=403)
            return
        self._send_json({"ok": True}, set_cookie=_clear_session_header())

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

    def _send_json(
        self, payload: dict[str, object], *, status: int = 200, set_cookie: str | None = None
    ) -> None:
        self._send_bytes(
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
            set_cookie=set_cookie,
        )

    def _reject_unauthenticated(self, path: str) -> None:
        """Refuse an unauthenticated GET in the shape the caller understands.

        A browser navigating to a page is redirected to the sign-in page. Any
        other request — a `fetch` for JSON or a sub-resource — gets a 401 with
        NO `WWW-Authenticate` header, which the dashboard script turns into the
        same redirect. Neither response can raise the browser's native
        credential popup, because nothing here advertises Basic auth.
        """
        if path in _PAGE_PATHS:
            self._redirect("/login")
            return
        self._send_json({"error": "unauthorized"}, status=401)

    def _redirect(self, location: str) -> None:
        """A 302 to a same-origin path, carrying the standard security headers
        and no body."""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for header, value in _SECURITY_HEADERS:
            self.send_header(header, value)
        self.end_headers()

    def _send_bytes(
        self, body: bytes, content_type: str, *, status: int = 200, set_cookie: str | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
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

    A live dashboard reachable from off the machine must carry a password. If
    the bind is not loopback and no ``TRANSLOG_DASHBOARD_TOKEN`` is set, the
    process refuses to start rather than serve real client data and a working
    "approve and send" button to anyone who knows the URL — the Host allowlist
    stops a cross-origin browser, not a direct request. A loopback bind stays
    open, so local development is unchanged.
    """
    if live and _binds_publicly(host) and _dashboard_token() is None:
        print(
            f"Refusing to serve the live dashboard on a non-loopback address ({host}) "
            f"without a password. Set {DASHBOARD_TOKEN_VAR} to require one, or bind to "
            "127.0.0.1 for local use."
        )
        return 2

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
            print(
                "  Access:   sign-in required (session cookie)"
                if _dashboard_token() is not None
                else "  Access:   no password set — loopback only"
            )
            print("  Approval: human — nothing sends without an explicit click")
            print(f"  Mailbox:  read automatically every {interval:g}s — no button to press")
            print("  Scope:    mail that arrives from now on; the inbox's history is ignored")
        print("  Ctrl+C stops the server.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0
