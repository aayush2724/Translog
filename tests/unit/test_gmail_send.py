"""The outbound Gmail sink: MIME framing, the send transport, and the scopes.

No network anywhere. The transport is exercised against a stub HTTP client and
the sink against a stub transport, which is what the ``GmailSendTransport``
seam exists for.
"""

from __future__ import annotations

import base64
import json
from email import message_from_bytes
from email.policy import default as default_policy
from pathlib import Path
from typing import Any

import httpx
import pytest

from translog_quote.adapters.email.gmail import GMAIL_READONLY_SCOPE
from translog_quote.adapters.email.gmail_send import (
    GMAIL_SEND_SCOPE,
    SEND_PATH,
    GmailEmailSink,
    HttpxGmailSendTransport,
    build_mime,
)
from translog_quote.domain.email import OutboundMessage
from translog_quote.errors import ContractViolation, PermanentFailure, TransientFailure

MESSAGE = OutboundMessage(
    to_address="client@example.com",
    subject="Air freight quotation R-1 — Ahmedabad to Bahrain",
    body_text="Dear Sir/Madam,\n\nPlease find our quotation below.\n",
)

SENDER = "translog@example.com"


class StubTransport:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, payload))
        if self._fail is not None:
            raise self._fail
        return {"id": "1900", "threadId": "1900"}


def decoded(payload: dict[str, Any]) -> Any:
    """The sent bytes, read back as a message.

    ``policy=default`` on the way in, matching ``EmailMessage``'s own policy on
    the way out: headers come back as decoded unicode rather than as RFC 2047
    encoded words, so an assertion compares what a recipient sees.
    """
    raw = payload["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw), policy=default_policy)


# --- scopes: the whole separation argument --------------------------------------


def test_the_send_scope_is_send_only_and_is_not_the_read_scope() -> None:
    """The two halves of the Gmail integration hold different grants.

    This is the assertion behind "inbound processing is separate from the
    outbound sink": not a comment, but two different strings requested in two
    different consent runs.
    """
    assert GMAIL_SEND_SCOPE == "https://www.googleapis.com/auth/gmail.send"
    assert GMAIL_SEND_SCOPE != GMAIL_READONLY_SCOPE


# --- MIME framing ---------------------------------------------------------------


def test_the_mime_message_carries_the_configured_sender() -> None:
    mime = decoded({"raw": build_mime(MESSAGE, sender_address=SENDER)})

    assert mime["From"] == SENDER
    assert mime["To"] == "client@example.com"
    assert mime["Subject"] == MESSAGE.subject


def test_the_body_survives_the_round_trip_unchanged() -> None:
    mime = decoded({"raw": build_mime(MESSAGE, sender_address=SENDER)})

    assert mime.get_content().strip() == MESSAGE.body_text.strip()


def test_a_reply_carries_both_threading_headers() -> None:
    """In-Reply-To alone threads inconsistently across clients; References is
    what actually places the quotation in the client's existing conversation."""
    reply = MESSAGE.model_copy(update={"in_reply_to": "<enq-1@client.example>"})

    mime = decoded({"raw": build_mime(reply, sender_address=SENDER)})

    assert mime["In-Reply-To"] == "<enq-1@client.example>"
    assert mime["References"] == "<enq-1@client.example>"


def test_a_first_message_carries_no_threading_headers() -> None:
    mime = decoded({"raw": build_mime(MESSAGE, sender_address=SENDER)})

    assert mime["In-Reply-To"] is None
    assert mime["References"] is None


@pytest.mark.parametrize(
    "field, value",
    [
        ("subject", "Quotation\r\nBcc: attacker@example.com"),
        ("to_address", "client@example.com\nBcc: attacker@example.com"),
    ],
)
def test_a_header_carrying_a_line_break_is_refused(field: str, value: str) -> None:
    """Header injection. A reply subject is the client's own text with "Re: "
    prefixed, so this is untrusted input reaching a header position. The
    correct answer is to refuse, not to strip characters and send something
    the caller did not write."""
    hostile = MESSAGE.model_copy(update={field: value})

    with pytest.raises(ContractViolation, match="line break"):
        build_mime(hostile, sender_address=SENDER)


# --- the sink -------------------------------------------------------------------


def test_the_sink_posts_to_the_send_endpoint_and_nowhere_else() -> None:
    transport = StubTransport()

    GmailEmailSink(transport, sender_address=SENDER).send(MESSAGE)

    assert [path for path, _ in transport.calls] == [SEND_PATH]


def test_the_sink_records_what_it_sent() -> None:
    transport = StubTransport()
    sink = GmailEmailSink(transport, sender_address=SENDER)

    sink.send(MESSAGE)

    assert sink.sent == [MESSAGE]


def test_a_failed_send_is_not_recorded_as_delivered() -> None:
    """A run's own record of what went out must not include a message the
    provider rejected — that is the record a person checks after a demo."""
    sink = GmailEmailSink(StubTransport(fail=TransientFailure("boom")), sender_address=SENDER)

    with pytest.raises(TransientFailure):
        sink.send(MESSAGE)

    assert sink.sent == []


def test_the_sink_refuses_to_build_without_a_sender_address() -> None:
    with pytest.raises(PermanentFailure, match="sender address"):
        GmailEmailSink(StubTransport(), sender_address="")


# --- the transport --------------------------------------------------------------


def write_token(tmp_path: Path) -> Path:
    path = tmp_path / "send_token.json"
    path.write_text(
        json.dumps(
            {
                "refresh_token": "not-a-real-refresh-token",
                "client_id": "not-a-real-client-id",
                "client_secret": "not-a-real-client-secret",
            }
        ),
        encoding="utf-8",
    )
    return path


class RecordingClient:
    """Answers the token endpoint, then the send endpoint, from a script."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((url, kwargs))
        return self._responses.pop(0)


def ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def transport_for(tmp_path: Path, client: RecordingClient) -> HttpxGmailSendTransport:
    return HttpxGmailSendTransport(
        token_path=write_token(tmp_path),
        timeout_seconds=5,
        max_retries=0,
        client=client,  # type: ignore[arg-type]
        sleep=lambda _: None,
        jitter=lambda: 0.0,
    )


def test_the_transport_refreshes_then_posts_the_message(tmp_path: Path) -> None:
    client = RecordingClient(ok({"access_token": "at"}), ok({"id": "abc"}))

    body = transport_for(tmp_path, client).post_json(SEND_PATH, {"raw": "eA=="})

    assert body == {"id": "abc"}
    token_url, send_url = (url for url, _ in client.requests)
    assert token_url == "https://oauth2.googleapis.com/token"
    assert send_url.endswith("/users/me/messages/send")


def test_the_transport_refuses_any_path_but_the_send_endpoint(tmp_path: Path) -> None:
    """One endpoint, enforced. A send transport that can be pointed at an
    arbitrary path is a send transport that can be pointed at a read path."""
    client = RecordingClient(ok({"access_token": "at"}), ok({}))

    with pytest.raises(ContractViolation, match="may only post"):
        transport_for(tmp_path, client).post_json("messages", {"raw": "eA=="})

    assert client.requests == []


def test_a_rejected_send_credential_says_which_command_fixes_it(tmp_path: Path) -> None:
    client = RecordingClient(
        ok({"access_token": "at"}),
        httpx.Response(401, json={"error": {"message": "Invalid Credentials"}}),
        ok({"access_token": "at2"}),
        httpx.Response(401, json={"error": {"message": "Invalid Credentials"}}),
    )

    with pytest.raises(PermanentFailure, match="gmail-auth-send"):
        transport_for(tmp_path, client).post_json(SEND_PATH, {"raw": "eA=="})


def test_a_permanent_send_failure_is_never_retried(tmp_path: Path) -> None:
    """Mailing a client twice is worse than not mailing them at all, so only
    transient statuses are retried and a 400 stops immediately."""
    client = RecordingClient(
        ok({"access_token": "at"}),
        httpx.Response(400, json={"error": {"message": "Invalid to header"}}),
    )
    transport = HttpxGmailSendTransport(
        token_path=write_token(tmp_path),
        timeout_seconds=5,
        max_retries=3,
        client=client,  # type: ignore[arg-type]
        sleep=lambda _: None,
        jitter=lambda: 0.0,
    )

    with pytest.raises(ContractViolation):
        transport.post_json(SEND_PATH, {"raw": "eA=="})

    assert len(client.requests) == 2  # one refresh, one send, no retry


def test_the_transport_refuses_to_build_without_a_token_file(tmp_path: Path) -> None:
    with pytest.raises(PermanentFailure, match="gmail-auth"):
        HttpxGmailSendTransport(
            token_path=tmp_path / "absent.json", timeout_seconds=5, max_retries=0
        )


def test_no_error_message_repeats_a_credential(tmp_path: Path) -> None:
    client = RecordingClient(
        httpx.Response(400, json={"error": "invalid_grant"}),
    )

    with pytest.raises(PermanentFailure) as excinfo:
        transport_for(tmp_path, client).post_json(SEND_PATH, {"raw": "eA=="})

    message = str(excinfo.value)
    for secret in ("not-a-real-refresh-token", "not-a-real-client-secret", "not-a-real-client-id"):
        assert secret not in message
