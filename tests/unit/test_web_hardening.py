"""Production hardening of the dashboard's HTTP surface.

What a browser, a crawler or a script sees on every response — headers that
close clickjacking and feature abuse, a 404 that is a page for a person and
JSON for a fetch, a robots file that keeps an operator desk out of search
results, revalidatable static assets, and a sign-in that cannot be guessed at
machine speed. All over real HTTP against the real server; only the mailbox
and the model are stubbed.
"""

from __future__ import annotations

import http.client
import json

import pytest
from tests.unit.test_hosted_deployment import live_server, login  # noqa: F401 - fixture
from tests.unit.test_web_poc import server, settings  # noqa: F401 - fixtures

from translog_quote.interface.web import server as server_module
from translog_quote.interface.web.server import (
    _NOT_FOUND_PAGE,
    _STATIC_DIR,
    DASHBOARD_TOKEN_VAR,
    DemoServer,
    LoginThrottle,
)

TOKEN = "test-not-a-real-credential"


def fetch(
    srv: DemoServer,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    try:
        sent = {"Host": "127.0.0.1", **(headers or {})}
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


BROWSER = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "x-robots-tag": "noindex, nofollow",
}


def assert_hardened(headers: dict[str, str]) -> None:
    for name, value in EXPECTED_HEADERS.items():
        assert headers.get(name) == value, name
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "camera=()" in headers["permissions-policy"]
    assert "strict-transport-security" not in headers, "HSTS is for public binds only"


# --- headers on every kind of response ---------------------------------------------


def test_the_page_carries_the_full_header_set(server: DemoServer) -> None:  # noqa: F811
    status, headers, _ = fetch(server, "/", headers=BROWSER)

    assert status == 200
    assert_hardened(headers)


def test_json_responses_are_never_cached(server: DemoServer) -> None:  # noqa: F811
    status, headers, _ = fetch(server, "/api/state")

    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert_hardened(headers)


def test_a_redirect_carries_the_headers_too(
    live_server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, TOKEN)

    status, headers, _ = fetch(live_server, "/", headers=BROWSER)

    assert status == 302
    assert headers["location"] == "/login"
    assert headers["cache-control"] == "no-store"
    assert_hardened(headers)


def test_hsts_is_sent_only_when_bound_publicly(
    server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "_hsts_enabled", True)

    _, headers, _ = fetch(server, "/")

    assert headers["strict-transport-security"].startswith("max-age=31536000")


# --- robots and 404 ---------------------------------------------------------------------


def test_robots_txt_disallows_everything_without_a_session(
    live_server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, TOKEN)

    status, headers, body = fetch(live_server, "/robots.txt")

    assert status == 200
    assert headers["content-type"].startswith("text/plain")
    assert body == b"User-agent: *\nDisallow: /\n"
    assert_hardened(headers)


def test_a_navigation_to_an_unknown_path_gets_a_page(server: DemoServer) -> None:  # noqa: F811
    status, headers, body = fetch(server, "/no-such-page", headers=BROWSER)

    assert status == 404
    assert headers["content-type"].startswith("text/html")
    assert b"Page not found" in body
    assert b'href="/"' in body, "a way back to the desk"
    assert b'name="robots" content="noindex' in body
    assert b"<script" not in body, "static: nothing for the CSP to block"
    assert_hardened(headers)


def test_a_fetch_to_an_unknown_path_still_gets_json(server: DemoServer) -> None:  # noqa: F811
    status, headers, body = fetch(server, "/no-such-page")

    assert status == 404
    assert headers["content-type"].startswith("application/json")
    assert json.loads(body) == {"error": "not found"}


def test_an_unknown_path_is_404_even_without_a_session(
    live_server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mistyped URL gets the page, not a sign-in redirect and not a 401."""
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, TOKEN)

    status, _, body = fetch(live_server, "/requests/R-1", headers=BROWSER)

    assert status == 404
    assert b"Page not found" in body


def test_a_protected_api_path_is_still_401_not_404_without_a_session(
    live_server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, TOKEN)

    status, _, _ = fetch(live_server, "/api/live/state")

    assert status == 401


def test_the_not_found_page_is_committed_source_with_no_inline_code() -> None:
    page = (_STATIC_DIR / _NOT_FOUND_PAGE).read_text(encoding="utf-8")

    assert "<script" not in page
    assert "onclick=" not in page
    assert 'href="/app.css"' in page and 'href="/login.css"' in page, "public assets only"


# --- static assets revalidate -------------------------------------------------------------


def test_static_assets_carry_an_etag_and_revalidate(server: DemoServer) -> None:  # noqa: F811
    status, headers, body = fetch(server, "/app.css")

    assert status == 200
    assert headers["cache-control"] == "no-cache"
    etag = headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')
    assert body

    status, headers, body = fetch(server, "/app.css", headers={"If-None-Match": etag})

    assert status == 304
    assert body == b""
    assert headers["etag"] == etag
    assert_hardened(headers)


def test_a_stale_etag_gets_the_file_again(server: DemoServer) -> None:  # noqa: F811
    status, _, body = fetch(server, "/app.css", headers={"If-None-Match": '"stale"'})

    assert status == 200
    assert body


def test_the_sign_in_assets_revalidate_without_a_session(
    live_server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, TOKEN)

    _, headers, _ = fetch(live_server, "/login.js")

    assert headers["cache-control"] == "no-cache"
    assert "etag" in headers


# --- sign-in throttle ---------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_throttle_counts_failures_per_client_over_a_window() -> None:
    clock = Clock()
    throttle = LoginThrottle(window_seconds=300, per_client=3, global_limit=100, clock=clock)

    for _ in range(3):
        assert throttle.retry_after("a") is None
        throttle.record_failure("a")

    assert throttle.retry_after("a") == 300
    assert throttle.retry_after("b") is None, "another client is unaffected"
    clock.now += 299
    assert throttle.retry_after("a") == 1
    clock.now += 2
    assert throttle.retry_after("a") is None, "the window slid past the failures"


def test_a_success_clears_that_clients_failures_only() -> None:
    throttle = LoginThrottle(window_seconds=300, per_client=2, global_limit=10, clock=Clock())
    throttle.record_failure("a")
    throttle.record_failure("a")
    throttle.record_failure("b")
    assert throttle.retry_after("a") == 300

    throttle.record_success("a")

    assert throttle.retry_after("a") is None
    throttle.record_failure("b")
    assert throttle.retry_after("b") == 300, "another client's failures are untouched"


def test_the_global_budget_bounds_a_distributed_guess() -> None:
    throttle = LoginThrottle(window_seconds=60, per_client=5, global_limit=4, clock=Clock())
    for client in ("w", "x", "y", "z"):
        throttle.record_failure(client)

    assert throttle.retry_after("fresh-client") == 60


def test_repeated_wrong_keys_are_throttled_with_a_retry_after(
    live_server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, TOKEN)
    monkeypatch.setattr(
        server_module,
        "_LOGIN_THROTTLE",
        LoginThrottle(window_seconds=300, per_client=3, global_limit=50, clock=Clock()),
    )

    for _ in range(3):
        assert login(live_server, password="wrong")[0] == 401

    status, headers = login(live_server, password="wrong")
    assert status == 429
    assert headers["retry-after"] == "300"

    status, _ = login(live_server, password=TOKEN)
    assert status == 429, "a throttled client is refused even with the right key"


def test_the_throttle_lifts_when_the_window_passes(
    live_server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, TOKEN)
    clock = Clock()
    monkeypatch.setattr(
        server_module,
        "_LOGIN_THROTTLE",
        LoginThrottle(window_seconds=300, per_client=2, global_limit=50, clock=clock),
    )
    login(live_server, password="wrong")
    login(live_server, password="wrong")
    assert login(live_server, password=TOKEN)[0] == 429

    clock.now += 301

    status, headers = login(live_server, password=TOKEN)
    assert status == 200
    assert "set-cookie" in headers


def test_the_throttle_body_names_no_key_and_the_wait(
    live_server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DASHBOARD_TOKEN_VAR, TOKEN)
    monkeypatch.setattr(
        server_module,
        "_LOGIN_THROTTLE",
        LoginThrottle(window_seconds=120, per_client=1, global_limit=50, clock=Clock()),
    )
    login(live_server, password="wrong")

    status, _, body = fetch(
        live_server,
        "/login",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"password": TOKEN}).encode("utf-8"),
    )

    assert status == 429
    payload = json.loads(body)
    assert payload == {"error": "too many attempts", "retry_after": 120}
    assert TOKEN not in body.decode("utf-8")


def test_without_a_token_the_throttle_never_engages(
    live_server: DemoServer,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DASHBOARD_TOKEN_VAR, raising=False)
    monkeypatch.setattr(
        server_module,
        "_LOGIN_THROTTLE",
        LoginThrottle(window_seconds=300, per_client=1, global_limit=1, clock=Clock()),
    )

    for _ in range(3):
        assert login(live_server, password="anything")[0] == 200


def test_the_client_key_is_the_proxys_appended_address() -> None:
    class Handler:
        client_address = ("10.0.0.9", 1234)

        def __init__(self, forwarded: str | None) -> None:
            self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}

    key_of = server_module._client_key
    assert key_of(Handler(None)) == "10.0.0.9"  # type: ignore[arg-type]
    assert key_of(Handler("203.0.113.5")) == "203.0.113.5"  # type: ignore[arg-type]
    spoofed = Handler("spoofed, 203.0.113.5")
    key = key_of(spoofed)  # type: ignore[arg-type]
    assert key == "203.0.113.5", "the client-controlled prefix is ignored"


# --- the pages themselves ----------------------------------------------------------------


@pytest.mark.parametrize("page", ["live.html", "login.html", "index.html", "not_found.html"])
def test_every_page_asks_not_to_be_indexed(page: str) -> None:
    html = (_STATIC_DIR / page).read_text(encoding="utf-8")

    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert '<meta name="description" content="' in html
    assert "<title>" in html


def test_the_pages_have_distinct_titles() -> None:
    def title_of(page: str) -> str:
        html = (_STATIC_DIR / page).read_text(encoding="utf-8")
        return html.split("<title>")[1].split("</title>")[0]

    pages = ("live.html", "login.html", "index.html", "not_found.html")
    titles = {page: title_of(page) for page in pages}

    assert len(set(titles.values())) == 4, titles


@pytest.mark.parametrize("page", ["live.html", "index.html"])
def test_the_desk_pages_offer_a_skip_link_to_a_focusable_main(page: str) -> None:
    html = (_STATIC_DIR / page).read_text(encoding="utf-8")

    assert '<a class="skip-link" href="#main">Skip to content</a>' in html
    assert '<main id="main" class="container" tabindex="-1">' in html


def test_the_sign_in_field_is_described_by_its_hint() -> None:
    html = (_STATIC_DIR / "login.html").read_text(encoding="utf-8")

    assert 'aria-describedby="login-hint"' in html
    assert 'id="login-hint"' in html
    assert 'autocomplete="current-password"' in html
    assert 'autocomplete="username"' in html, "password managers pair the key with a username"


def test_the_sign_in_script_handles_the_throttle_and_empty_input() -> None:
    js = (_STATIC_DIR / "login.js").read_text(encoding="utf-8")

    assert "429" in js
    assert "Enter the dashboard access key." in js
    assert 'setAttribute("aria-invalid", "true")' in js
    assert ".innerHTML" not in js and "eval(" not in js


def test_the_dashboard_script_bounds_its_reads_and_pauses_hidden_tabs() -> None:
    js = (_STATIC_DIR / "live.js").read_text(encoding="utf-8")

    assert "STATE_TIMEOUT_MS" in js
    assert "document.hidden" in js
    assert '"visibilitychange"' in js
    assert "document.title" in js
