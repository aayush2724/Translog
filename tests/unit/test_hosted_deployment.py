"""Running the existing server on a host that assigns the port and the name.

Two things a platform like Render changes about how this process is reached,
and nothing else:

- the port is handed over in ``PORT`` rather than chosen locally;
- the browser reaches the server by hostname, not by loopback address.

The second is the one with teeth. The cross-site guard was written for a
loopback-only server and refuses every state-changing POST whose ``Host`` is a
name — so on a deployed URL the page would load and then silently 403 on Check
mail, on approving a clarification and on approving a quotation. The allowlist
is what makes the deployed name legitimate, and these tests pin that it stays
an allowlist: loopback always, declared names as well, everything else refused.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.unit.test_gmail_thread import ENQUIRY, ENQUIRY_EXTRACTION, ScriptedExtractor, StubSource

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.config import Settings
from translog_quote.interface.web import __main__ as web_main
from translog_quote.interface.web.live_session import LiveSession
from translog_quote.interface.web.server import (
    _SESSION_COOKIE,
    _SESSION_TTL_SECONDS,
    ALLOWED_HOSTS_VAR,
    DASHBOARD_TOKEN_VAR,
    DEFAULT_PORT,
    DemoServer,
    _binds_publicly,
    _mint_session,
    run,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def live_server() -> Iterator[DemoServer]:
    """A real server on an ephemeral port. Declared here rather than imported,
    so this module owns its own fixture instead of shadowing another's."""
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    settings = base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": "test-not-a-credential"}),
            "demo": base.demo.model_copy(update={"state_dir": Path(tempfile.mkdtemp())}),
            "gmail": base.gmail.model_copy(
                update={
                    "test_address": "translog@example.com",
                    "sender_address": "translog@example.com",
                    "approver_address": "approvals@translog.example",
                    "send_enabled": True,
                }
            ),
        }
    )
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION),  # type: ignore[arg-type]
    )
    instance = DemoServer(("127.0.0.1", 0), settings, live_session=session)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()


# --- the port the platform assigns ----------------------------------------------


def test_the_port_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "10000")
    seen: dict[str, object] = {}

    def fake_run(*, host: str, port: int, settings: object, live: bool) -> int:
        seen["host"], seen["port"] = host, port
        return 0

    monkeypatch.setattr(web_main, "run", fake_run)
    web_main.main([])

    assert seen["port"] == 10000


def test_an_explicit_port_flag_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "10000")
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        web_main,
        "run",
        lambda **kw: (seen.update(kw), 0)[1],  # type: ignore[arg-type,return-value]
    )

    web_main.main(["--port", "9999"])

    assert seen["port"] == 9999


def test_the_bind_address_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform routes to the container's external interface, not loopback."""
    monkeypatch.setenv("HOST", "0.0.0.0")  # noqa: S104 - the platform requires it
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        web_main,
        "run",
        lambda **kw: (seen.update(kw), 0)[1],  # type: ignore[arg-type,return-value]
    )

    web_main.main([])

    assert seen["host"] == "0.0.0.0"  # noqa: S104


def test_with_nothing_set_the_local_defaults_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of reading these as flag defaults rather than instead."""
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        web_main,
        "run",
        lambda **kw: (seen.update(kw), 0)[1],  # type: ignore[arg-type,return-value]
    )

    web_main.main([])

    assert seen["port"] == DEFAULT_PORT
    assert seen["host"] == "127.0.0.1"


@pytest.mark.parametrize("bad", ["", "   ", "not-a-port", "80a"])
def test_an_unusable_port_falls_back_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A boot crash on a restarting host is a loop, not a failure anyone sees."""
    monkeypatch.setenv("PORT", bad)

    assert web_main._env_port() == DEFAULT_PORT


# --- the Host allowlist ----------------------------------------------------------


def post(server: DemoServer, host_header: str) -> int:
    """A state-changing POST carrying the Host a deployed browser would send."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(
            "POST",
            "/api/live/poll",
            body=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Host": host_header},
        )
        return connection.getresponse().status
    finally:
        connection.close()


def test_a_deployed_hostname_is_refused_until_it_is_declared(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocker, stated as a test: without this the deployed UI is read-only."""
    monkeypatch.delenv(ALLOWED_HOSTS_VAR, raising=False)

    assert post(live_server, "translog.onrender.com") == 403


def test_a_declared_hostname_is_accepted(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "translog.onrender.com")

    assert post(live_server, "translog.onrender.com") == 200


def test_several_names_may_be_declared_at_once(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "one.example.com, two.example.com")

    assert post(live_server, "two.example.com") == 200


def test_an_undeclared_name_is_still_refused_while_others_are_declared(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allowlist, not a switch that turns the guard off."""
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "translog.onrender.com")

    assert post(live_server, "attacker.example.com") == 403


def test_loopback_never_needs_declaring(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "translog.onrender.com")

    assert post(live_server, "127.0.0.1") == 200
    assert post(live_server, "localhost") == 200


def test_a_wildcard_is_not_a_supported_value(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`*` is a literal name here, so it cannot silently disable the check."""
    monkeypatch.setenv(ALLOWED_HOSTS_VAR, "*")

    assert post(live_server, "attacker.example.com") == 403


# --- the dashboard sign-in (signed session cookie) -------------------------------
#
# The Host allowlist above stops a cross-origin browser; it does not stop anyone
# who simply knows the URL. On a deployed --live instance that is real client
# data and a working "approve and send" button reachable by that person. A
# sign-in page closes that gap: an operator posts the shared password once, the
# server sets a signed, HttpOnly session cookie, and every route requires it.
# There is no HTTP Basic anywhere, so the browser's native credential popup can
# no longer appear; a non-loopback live bind without a token still refuses to
# start, and loopback development stays open.


def _cookie_from(headers: dict[str, str]) -> str | None:
    """The session cookie value out of a `Set-Cookie` response header, if any."""
    raw = headers.get("set-cookie")
    if not raw:
        return None
    name, _, value = raw.split(";", 1)[0].partition("=")
    return value if name == _SESSION_COOKIE else None


def get(
    server: DemoServer, *, path: str = "/api/live/state", cookie: str | None = None
) -> tuple[int, dict[str, str]]:
    """A GET, optionally carrying a session cookie. Returns (status, headers)."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    headers = {"Host": "127.0.0.1"}
    if cookie is not None:
        headers["Cookie"] = f"{_SESSION_COOKIE}={cookie}"
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}
    finally:
        connection.close()


def post_authed(server: DemoServer, *, cookie: str | None = None) -> tuple[int, dict[str, str]]:
    """A state-changing POST from loopback, optionally carrying a session cookie."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    headers = {"Content-Type": "application/json", "Host": "127.0.0.1"}
    if cookie is not None:
        headers["Cookie"] = f"{_SESSION_COOKIE}={cookie}"
    try:
        connection.request("POST", "/api/live/poll", body=b"{}", headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}
    finally:
        connection.close()


def login(
    server: DemoServer, *, password: str, username: str = "operator"
) -> tuple[int, dict[str, str]]:
    """POST the sign-in form. Returns (status, headers) so a test can read the
    Set-Cookie the server issues."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    try:
        connection.request(
            "POST", "/login", body=body,
            headers={"Content-Type": "application/json", "Host": "127.0.0.1"},
        )
        response = connection.getresponse()
        response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}
    finally:
        connection.close()


def logout(server: DemoServer) -> tuple[int, dict[str, str]]:
    """POST sign-out. Returns (status, headers) so a test can read the clearing
    Set-Cookie."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(
            "POST", "/logout", body=b"{}",
            headers={"Content-Type": "application/json", "Host": "127.0.0.1"},
        )
        response = connection.getresponse()
        response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}
    finally:
        connection.close()


def test_without_a_token_the_dashboard_is_open_on_loopback(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local development is unchanged: no password set, loopback bind, no auth."""
    monkeypatch.delenv(DASHBOARD_TOKEN_VAR, raising=False)

    assert get(live_server)[0] == 200
    assert post_authed(live_server)[0] == 200


def test_the_login_page_is_reachable_without_a_session(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /login is how a session is obtained, so it is never gated — and it
    never advertises Basic auth."""
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    status, headers = get(live_server, path="/login")

    assert status == 200
    assert "text/html" in headers.get("content-type", "")
    assert "www-authenticate" not in headers


def test_an_unauthenticated_page_navigation_redirects_to_login(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser opening the dashboard with no session is sent to the sign-in
    page — not challenged with a popup."""
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    status, headers = get(live_server, path="/")

    assert status == 302
    assert headers.get("location") == "/login"
    assert "www-authenticate" not in headers


def test_an_unauthenticated_api_fetch_is_401_json_without_www_authenticate(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    status, headers = get(live_server, path="/api/live/state")

    assert status == 401
    assert "www-authenticate" not in headers
    assert "application/json" in headers.get("content-type", "")


def test_state_changing_actions_require_a_session(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approve/decide/poll endpoints — the ones that send real email."""
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    status, headers = post_authed(live_server)  # no cookie

    assert status == 401
    assert "www-authenticate" not in headers


def test_valid_login_sets_a_signed_session_cookie(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    status, headers = login(live_server, password="s3cret")

    assert status == 200
    set_cookie = headers.get("set-cookie", "")
    assert set_cookie.startswith(f"{_SESSION_COOKIE}=")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=" in set_cookie  # a finite expiry
    assert "www-authenticate" not in headers


def test_a_valid_cookie_authenticates_subsequent_requests(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    cookie = _cookie_from(login(live_server, password="s3cret")[1])
    assert cookie is not None

    assert get(live_server, cookie=cookie)[0] == 200
    assert post_authed(live_server, cookie=cookie)[0] == 200


def test_the_username_is_cosmetic_only_the_password_matters(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    cookie = _cookie_from(login(live_server, password="s3cret", username="anyone-at-all")[1])
    assert cookie is not None
    assert get(live_server, cookie=cookie)[0] == 200


def test_a_wrong_password_is_refused_without_www_authenticate(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    status, headers = login(live_server, password="wrong")

    assert status == 401
    assert "www-authenticate" not in headers  # no popup, ever
    assert _cookie_from(headers) is None  # and no session handed out


def test_a_tampered_cookie_is_rejected(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    cookie = _cookie_from(login(live_server, password="s3cret")[1])
    assert cookie is not None
    payload, _, signature = cookie.rpartition(".")

    # A blanked signature and a forged (later) expiry keeping the old signature
    # both fail: nothing is trusted from the cookie until the HMAC recomputes.
    assert get(live_server, cookie=f"{payload}.{'0' * len(signature)}")[0] == 401
    assert get(live_server, cookie=f"{int(payload) + 999999}.{signature}")[0] == 401


def test_an_expired_cookie_is_rejected(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correctly-signed cookie whose signed expiry is already in the past."""
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    expired = _mint_session("s3cret", now=time.time() - _SESSION_TTL_SECONDS - 10)

    assert get(live_server, cookie=expired)[0] == 401


def test_logout_clears_the_session(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    cookie = _cookie_from(login(live_server, password="s3cret")[1])
    assert get(live_server, cookie=cookie)[0] == 200

    status, headers = logout(live_server)

    assert status == 200
    cleared = headers.get("set-cookie", "")
    assert cleared.startswith(f"{_SESSION_COOKIE}=")
    assert "Max-Age=0" in cleared  # the browser drops it at once
    # The cleared (empty) cookie no longer authenticates a page navigation.
    assert get(live_server, path="/", cookie="")[0] == 302


def test_no_route_in_the_auth_flow_advertises_basic_auth(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The popup guarantee, stated directly: the page, the API and a failed
    sign-in all answer without `WWW-Authenticate`, so no browser can raise its
    native username/password dialog from the dashboard auth flow."""
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    assert "www-authenticate" not in get(live_server, path="/")[1]
    assert "www-authenticate" not in get(live_server, path="/api/live/state")[1]
    assert "www-authenticate" not in get(live_server, path="/login")[1]
    assert "www-authenticate" not in login(live_server, password="wrong")[1]


def test_health_stays_reachable_without_a_session(
    live_server: DemoServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monitor carries no session; /health must answer it, and without a
    popup challenge."""
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, "s3cret")

    status, headers = get(live_server, path="/health")

    assert status == 200
    assert "www-authenticate" not in headers


# --- fail closed: a public live bind must carry a password -----------------------


def test_a_live_public_bind_without_a_token_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The B1 fix: `run` returns non-zero before binding rather than serving open."""
    monkeypatch.delenv(DASHBOARD_TOKEN_VAR, raising=False)

    assert run(host="0.0.0.0", port=0, live=True) == 2  # noqa: S104 - the refused case


def test_a_specific_public_address_without_a_token_also_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DASHBOARD_TOKEN_VAR, raising=False)

    assert run(host="10.0.0.5", port=0, live=True) == 2


@pytest.mark.parametrize("loopback", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_loopback_addresses_are_not_treated_as_public(loopback: str) -> None:
    assert _binds_publicly(loopback) is False


@pytest.mark.parametrize("public", ["0.0.0.0", "::", "10.0.0.5", "translog.onrender.com"])  # noqa: S104
def test_off_box_addresses_are_treated_as_public(public: str) -> None:
    assert _binds_publicly(public) is True
