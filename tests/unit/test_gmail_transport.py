"""The Gmail HTTP transport, exercised against a mock server, never Gmail.

`httpx.MockTransport` intercepts requests inside httpx itself, so these tests
assert real request construction, real token handling and real status mapping
without a socket, a mailbox, or a credential.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from translog_quote.adapters.email import HttpxGmailTransport
from translog_quote.adapters.email.gmail import (
    GMAIL_API_BASE,
    GOOGLE_TOKEN_URL,
    _NotFound,
)
from translog_quote.errors import ContractViolation, PermanentFailure, TransientFailure

if TYPE_CHECKING:
    from pathlib import Path

#: Stand-in credentials. Deliberately not shaped like real ones, so secret
#: scanners do not raise an incident on obviously fake values.
REFRESH_TOKEN = "rt-test-not-a-real-credential"
ACCESS_TOKEN = "at-test-not-a-real-credential"
CLIENT_SECRET = "cs-test-not-a-real-credential"

SLEPT: list[float] = []


def write_token_file(tmp_path: Path) -> Path:
    path = tmp_path / "gmail_token.json"
    path.write_text(
        json.dumps(
            {
                "refresh_token": REFRESH_TOKEN,
                "client_id": "test-client-id.apps.googleusercontent.com",
                "client_secret": CLIENT_SECRET,
            }
        ),
        encoding="utf-8",
    )
    return path


def token_ok() -> httpx.Response:
    return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599})


def transport_with(tmp_path: Path, handler: Any, *, max_retries: int = 0) -> HttpxGmailTransport:
    SLEPT.clear()
    return HttpxGmailTransport(
        token_path=write_token_file(tmp_path),
        timeout_seconds=5,
        max_retries=max_retries,
        backoff_seconds=1.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=SLEPT.append,
        jitter=lambda: 0.5,
    )


def scripted(gmail_responses: list[httpx.Response]) -> tuple[Any, list[httpx.Request]]:
    """A handler that answers the token endpoint and scripts Gmail replies."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == GOOGLE_TOKEN_URL:
            return token_ok()
        return gmail_responses.pop(0) if gmail_responses else httpx.Response(200, json={})

    return handler, seen


# --- token file -----------------------------------------------------------------


def test_a_missing_token_file_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(PermanentFailure, match="gmail-auth"):
        HttpxGmailTransport(token_path=tmp_path / "absent.json", timeout_seconds=5, max_retries=0)


def test_a_token_file_missing_keys_names_the_keys_not_the_values(tmp_path: Path) -> None:
    path = tmp_path / "gmail_token.json"
    path.write_text(json.dumps({"refresh_token": REFRESH_TOKEN}), encoding="utf-8")

    with pytest.raises(PermanentFailure, match="client_id") as excinfo:
        HttpxGmailTransport(token_path=path, timeout_seconds=5, max_retries=0)
    assert REFRESH_TOKEN not in str(excinfo.value)


def test_a_non_json_token_file_is_refused_without_echoing_content(tmp_path: Path) -> None:
    path = tmp_path / "gmail_token.json"
    path.write_text("secret-garbage-not-json", encoding="utf-8")

    with pytest.raises(PermanentFailure) as excinfo:
        HttpxGmailTransport(token_path=path, timeout_seconds=5, max_retries=0)
    assert "secret-garbage" not in str(excinfo.value)


# --- authentication -------------------------------------------------------------


def test_it_refreshes_once_then_gets_with_a_bearer_header(tmp_path: Path) -> None:
    handler, seen = scripted([httpx.Response(200, json={"emailAddress": "t@example.com"})])

    transport_with(tmp_path, handler).get_json("profile")

    assert seen[0].method == "POST"
    assert str(seen[0].url) == GOOGLE_TOKEN_URL
    assert seen[1].method == "GET"
    assert str(seen[1].url) == f"{GMAIL_API_BASE}/profile"
    assert seen[1].headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    # Never in the URL, where it would land in logs and proxy records.
    assert ACCESS_TOKEN not in str(seen[1].url)
    assert REFRESH_TOKEN not in str(seen[1].url)


def test_a_rejected_refresh_is_permanent_and_leaks_no_credential(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(PermanentFailure, match="invalid_grant") as excinfo:
        transport_with(tmp_path, handler).get_json("profile")

    text = str(excinfo.value)
    assert REFRESH_TOKEN not in text
    assert CLIENT_SECRET not in text


def test_a_401_buys_exactly_one_refresh_and_one_retry(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == GOOGLE_TOKEN_URL:
            return token_ok()
        return httpx.Response(401, json={})

    with pytest.raises(PermanentFailure, match="401"):
        transport_with(tmp_path, handler).get_json("profile")

    posts = [r for r in seen if r.method == "POST"]
    gets = [r for r in seen if r.method == "GET"]
    assert len(posts) == 2  # initial token + exactly one re-refresh, never a loop
    assert len(gets) == 2


# --- status mapping -------------------------------------------------------------


def test_a_403_is_permanent_and_mentions_the_scope(tmp_path: Path) -> None:
    handler, _ = scripted([httpx.Response(403, json={"error": {"message": "forbidden"}})])

    with pytest.raises(PermanentFailure, match="gmail.readonly"):
        transport_with(tmp_path, handler).get_json("profile")


def test_a_404_is_the_internal_not_found_and_is_not_retried(tmp_path: Path) -> None:
    handler, _ = scripted([httpx.Response(404, json={})])

    with pytest.raises(_NotFound):
        transport_with(tmp_path, handler, max_retries=2).get_json("messages/x")
    assert SLEPT == []


def test_a_400_is_a_contract_violation(tmp_path: Path) -> None:
    handler, _ = scripted([httpx.Response(400, json={"error": {"message": "bad request"}})])

    with pytest.raises(ContractViolation, match="bad request"):
        transport_with(tmp_path, handler).get_json("messages")


def test_a_429_waits_the_advertised_retry_after_then_succeeds(tmp_path: Path) -> None:
    handler, _ = scripted(
        [
            httpx.Response(429, json={}, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"messages": []}),
        ]
    )

    body = transport_with(tmp_path, handler, max_retries=1).get_json("messages")

    assert body == {"messages": []}
    assert SLEPT == [7.0]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_retry_then_surface_as_transient(tmp_path: Path, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOGLE_TOKEN_URL:
            return token_ok()
        return httpx.Response(status, json={})

    with pytest.raises(TransientFailure, match=str(status)):
        transport_with(tmp_path, handler, max_retries=2).get_json("messages")
    assert len(SLEPT) == 2


def test_a_non_json_success_body_is_a_contract_violation(tmp_path: Path) -> None:
    handler, _ = scripted([httpx.Response(200, text="<html>not json</html>")])

    with pytest.raises(ContractViolation, match="non-JSON"):
        transport_with(tmp_path, handler).get_json("profile")


# --- no outbound path -----------------------------------------------------------


def test_every_gmail_request_is_a_get_and_only_the_token_endpoint_sees_posts(
    tmp_path: Path,
) -> None:
    """The structural no-send guarantee: whatever paths are asked for, the only
    POST this transport can emit goes to Google's fixed token endpoint."""
    handler, seen = scripted(
        [
            httpx.Response(200, json={}),
            httpx.Response(200, json={}),
            httpx.Response(200, json={}),
        ]
    )
    transport = transport_with(tmp_path, handler)

    transport.get_json("profile")
    transport.get_json("messages", {"q": "in:inbox", "maxResults": "1"})
    transport.get_json("messages/abc123", {"format": "full"})

    for request in seen:
        if request.method != "GET":
            assert str(request.url) == GOOGLE_TOKEN_URL
        assert "send" not in str(request.url)
        assert "drafts" not in str(request.url)
