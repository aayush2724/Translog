"""Translog must never read its own outbound mail back as a client's.

Found on the deployed demo, and the cause of the "stuck at NEEDS_INFO" the
operator kept hitting. Translog reads and sends from one mailbox, so a
clarification it sends arrives in its own inbox labelled INBOX *and* SENT. The
sent-mail guard allows that combination on purpose — a self-addressed test
message is the Phase 10.3 shape — and the clarification carries no
`[TRANSLOG INTERNAL]` marker because it is client-facing. So it was ingested:

    clarification sent  ->  arrives in our own inbox
    next poll ingests it, correlates it by its own In-Reply-To (correctly!)
    it states no shipment fields, so the merge changes nothing
    validation still fails -> back to NEEDS_INFO with a fresh round-2 draft
    the client's real reply is now deferred behind that draft, forever

Keying on Gmail's own message id rather than the RFC `Message-ID` is not a
detail: Gmail *rewrites* the Message-ID on send — verified against the live
API — so an id we choose cannot be matched afterwards. The id in the send
response can.
"""

from __future__ import annotations

from typing import Any

import pytest

from translog_quote.adapters.email.gmail import GmailEmailSource
from translog_quote.adapters.email.gmail_send import GmailEmailSink
from translog_quote.domain.email import OutboundMessage

MAILBOX = "translog@example.com"


class FakeSendTransport:
    """Answers like Gmail: an id, a threadId, and labels."""

    def __init__(self) -> None:
        self.count = 0

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.count += 1
        return {"id": f"gmail-sent-{self.count}", "threadId": "t1", "labelIds": ["SENT", "INBOX"]}


class FakeReadTransport:
    """A mailbox listing whatever messages it was constructed with."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = {m["id"]: m for m in messages}

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if path == "profile":
            return {"emailAddress": MAILBOX}
        if path == "messages":
            return {"messages": [{"id": i} for i in self._messages]}
        return self._messages[path.removeprefix("messages/")]


def message(gmail_id: str, *, rfc_id: str, subject: str, labels: list[str]) -> dict[str, Any]:
    return {
        "id": gmail_id,
        "threadId": "t1",
        "labelIds": labels,
        "payload": {
            "headers": [
                {"name": "Message-ID", "value": rfc_id},
                {"name": "From", "value": "someone@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 31 Aug 2026 12:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": "aGVsbG8="},  # "hello"
        },
    }


def test_gmail_rewrites_our_message_id_so_the_provider_id_is_the_key() -> None:
    """Documents why this is keyed the way it is, against the real behaviour.

    Verified live: a Message-ID set in the MIME we submit is replaced by Gmail
    with one of its own. Matching on it afterwards is therefore impossible.
    """
    sink = GmailEmailSink(FakeSendTransport(), sender_address=MAILBOX)

    sink.send(OutboundMessage(to_address="client@example.com", subject="Re: x", body_text="y"))

    # What we can match on is the id the provider handed back, not any header.
    assert sink.sent_provider_ids == {"gmail-sent-1"}


def test_a_message_this_run_sent_is_not_ingested() -> None:
    """The regression. Before the fix this came back as a client reply."""
    sink = GmailEmailSink(FakeSendTransport(), sender_address=MAILBOX)
    sink.send(OutboundMessage(to_address="client@example.com", subject="Re: x", body_text="y"))

    inbox = FakeReadTransport(
        [
            message(
                "gmail-sent-1",
                rfc_id="<rewritten-by-gmail@mail.gmail.com>",
                subject="Re: Air Freight Quote",
                labels=["INBOX", "SENT", "UNREAD"],  # exactly what Gmail assigns
            )
        ]
    )
    source = GmailEmailSource(
        inbox, mailbox_address=MAILBOX, sent_by_us=lambda: sink.sent_provider_ids
    )

    assert source.fetch_new() == ()


def test_a_genuine_client_message_is_still_ingested() -> None:
    """The filter must be exact, not a blanket refusal of self-addressed mail."""
    sink = GmailEmailSink(FakeSendTransport(), sender_address=MAILBOX)
    sink.send(OutboundMessage(to_address="client@example.com", subject="Re: x", body_text="y"))

    inbox = FakeReadTransport(
        [
            message(
                "gmail-sent-1",
                rfc_id="<ours@mail.gmail.com>",
                subject="ours",
                labels=["INBOX", "SENT"],
            ),
            message(
                "gmail-client-9",
                rfc_id="<theirs@mail.gmail.com>",
                subject="theirs",
                labels=["INBOX", "UNREAD"],
            ),
        ]
    )
    source = GmailEmailSource(
        inbox,
        mailbox_address=MAILBOX,
        max_results=5,
        sent_by_us=lambda: sink.sent_provider_ids,
    )

    fetched = source.fetch_new()

    assert [e.message_id for e in fetched] == ["<theirs@mail.gmail.com>"]


def test_the_filter_sees_sends_that_happened_after_the_source_was_built() -> None:
    """A poll builds the source, then a send happens, then the next poll runs.

    Passing a snapshot instead of a callable would miss exactly that ordering.
    """
    sink = GmailEmailSink(FakeSendTransport(), sender_address=MAILBOX)
    inbox = FakeReadTransport(
        [
            message(
                "gmail-sent-1",
                rfc_id="<ours@mail.gmail.com>",
                subject="ours",
                labels=["INBOX", "SENT"],
            )
        ]
    )
    source = GmailEmailSource(
        inbox, mailbox_address=MAILBOX, sent_by_us=lambda: sink.sent_provider_ids
    )
    assert len(source.fetch_new()) == 1, "not sent yet, so it is just a message"

    sink.send(OutboundMessage(to_address="c@example.com", subject="Re: x", body_text="y"))

    assert source.fetch_new() == (), "now it is ours, and must be skipped"


def test_a_sink_without_the_filter_still_works() -> None:
    """No source is required to be told about sends."""
    inbox = FakeReadTransport(
        [message("g1", rfc_id="<a@mail.gmail.com>", subject="a", labels=["INBOX"])]
    )

    assert len(GmailEmailSource(inbox, mailbox_address=MAILBOX).fetch_new()) == 1


def test_a_failed_send_records_no_provider_id() -> None:
    """Only what the provider accepted may be remembered as ours."""
    from translog_quote.errors import PermanentFailure

    class Refusing:
        def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise PermanentFailure("rejected")

    sink = GmailEmailSink(Refusing(), sender_address=MAILBOX)

    with pytest.raises(PermanentFailure):
        sink.send(OutboundMessage(to_address="c@example.com", subject="s", body_text="b"))

    assert sink.sent_provider_ids == set()
    assert sink.sent == []
