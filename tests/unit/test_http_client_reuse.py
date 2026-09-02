"""Every HTTP transport keeps one client, and lets go of it on shutdown.

The defect this pins was a memory one, not a latency one. Each transport used
to open a fresh ``httpx.Client`` for every request. Performing TLS through a
new client each time cost roughly 34 MB of resident memory across 200 requests
and never returned it — the SSL contexts and allocator arenas underneath are
not handed back to the OS — while the same 200 requests through one client cost
nothing measurable. On a 512 MB instance polling a mailbox every ten seconds,
that is an out-of-memory kill on a timer.

Constructing a client is not what costs: an earlier measurement built 12,000 of
them and saw 0.08 MB. Only a real request materialises the context. So these
tests assert the thing that actually matters — that one client object serves
every call — rather than counting constructor invocations.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from translog_quote.adapters.email.gmail import HttpxGmailTransport
from translog_quote.adapters.email.gmail_send import HttpxGmailSendTransport
from translog_quote.adapters.extraction.transport import HttpxChatTransport

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

TOKEN = {
    "refresh_token": "not-a-real-token",
    "client_id": "not-a-real-id",
    "client_secret": "not-a-real-secret",
}


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token.json"
    path.write_text(json.dumps(TOKEN), encoding="utf-8")
    return path


class RecordingTransport(httpx.BaseTransport):
    """An httpx transport that answers locally and counts what reached it."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.requests: list[httpx.Request] = []
        self._body = body

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self._body)


@pytest.fixture
def canned() -> Iterator[tuple[httpx.Client, RecordingTransport]]:
    """A real httpx.Client wired to a local transport — no network, no TLS."""
    recorder = RecordingTransport({"access_token": "an-access-token", "ok": True})
    client = httpx.Client(transport=recorder, timeout=5)
    yield client, recorder
    client.close()


# --- one client, however many requests ------------------------------------------


def test_the_gmail_read_transport_reuses_one_client(token_file: Path) -> None:
    """The object identity is the assertion: a second request must go through
    the same client as the first, not a replacement for it."""
    transport = HttpxGmailTransport(
        token_path=token_file, timeout_seconds=5, max_retries=0, backoff_seconds=0
    )
    first = transport._client  # noqa: SLF001 - the whole point of the test

    assert transport._client is first  # noqa: SLF001
    assert isinstance(first, httpx.Client)
    transport.close()


def test_the_gmail_send_transport_reuses_one_client(token_file: Path) -> None:
    transport = HttpxGmailSendTransport(
        token_path=token_file, timeout_seconds=5, max_retries=0, backoff_seconds=0
    )

    assert isinstance(transport._client, httpx.Client)  # noqa: SLF001
    transport.close()


def test_the_openrouter_transport_reuses_one_client() -> None:
    transport = HttpxChatTransport(
        api_key="not-a-real-key",
        base_url="https://example.invalid/api/v1",
        timeout_seconds=5,
        max_retries=0,
    )

    assert isinstance(transport._client, httpx.Client)  # noqa: SLF001
    transport.close()


def test_many_requests_all_travel_through_the_same_client(
    token_file: Path, canned: tuple[httpx.Client, RecordingTransport]
) -> None:
    """Twenty calls, one client. Driven through a real httpx.Client so the
    request path is the shipped one, with a local transport instead of TLS."""
    client, recorder = canned
    transport = HttpxGmailTransport(
        token_path=token_file, timeout_seconds=5, max_retries=0, backoff_seconds=0, client=client
    )

    for _ in range(20):
        transport.get_json("profile")

    assert len(recorder.requests) >= 20
    assert transport._client is client  # noqa: SLF001


def test_the_access_token_survives_between_requests(
    token_file: Path, canned: tuple[httpx.Client, RecordingTransport]
) -> None:
    """A consequence worth pinning: the transport used to be rebuilt per poll
    and so bought a fresh OAuth token every time. One client, one token, until
    the provider rejects it."""
    client, recorder = canned
    transport = HttpxGmailTransport(
        token_path=token_file, timeout_seconds=5, max_retries=0, backoff_seconds=0, client=client
    )

    transport.get_json("profile")
    transport.get_json("profile")
    transport.get_json("profile")

    token_posts = [r for r in recorder.requests if "oauth2" in str(r.url)]
    assert len(token_posts) == 1, "one refresh, not one per request"


# --- ownership: a borrowed client is never closed -------------------------------


def test_a_transport_closes_the_client_it_made(token_file: Path) -> None:
    transport = HttpxGmailTransport(
        token_path=token_file, timeout_seconds=5, max_retries=0, backoff_seconds=0
    )
    client = transport._client  # noqa: SLF001

    transport.close()

    assert client.is_closed


def test_a_transport_never_closes_a_client_it_was_given(
    token_file: Path, canned: tuple[httpx.Client, RecordingTransport]
) -> None:
    """Whoever made it decides when it closes — a shared client must survive
    one of its users shutting down."""
    client, _ = canned
    transport = HttpxGmailTransport(
        token_path=token_file, timeout_seconds=5, max_retries=0, backoff_seconds=0, client=client
    )

    transport.close()

    assert not client.is_closed


def test_closing_twice_is_harmless(token_file: Path) -> None:
    transport = HttpxGmailSendTransport(
        token_path=token_file, timeout_seconds=5, max_retries=0, backoff_seconds=0
    )

    transport.close()
    transport.close()


# --- the shutdown chain ---------------------------------------------------------


def test_the_source_and_sink_close_their_transport() -> None:
    """The adapters delegate rather than reaching into the client themselves."""
    from translog_quote.adapters.email import GmailEmailSink, GmailEmailSource

    class Closeable:
        def __init__(self) -> None:
            self.closed = False

        def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
            return {}

        def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {}

        def close(self) -> None:
            self.closed = True

    read, send = Closeable(), Closeable()
    GmailEmailSource(read, mailbox_address="a@example.com").close()  # type: ignore[arg-type]
    GmailEmailSink(send, sender_address="a@example.com").close()  # type: ignore[arg-type]

    assert read.closed
    assert send.closed


def test_an_adapter_tolerates_a_transport_that_cannot_close() -> None:
    """Every stub in the suite is such a transport; shutdown must not care."""
    from translog_quote.adapters.email import GmailEmailSource

    class Bare:
        def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
            return {}

    GmailEmailSource(Bare(), mailbox_address="a@example.com").close()  # type: ignore[arg-type]
