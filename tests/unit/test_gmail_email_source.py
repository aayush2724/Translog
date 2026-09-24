"""Gmail response → RawEmail, and the safety rules around which messages the
source is allowed to touch.

Every Gmail response here is a canned dictionary shaped like the documented
`users.messages.get(format=full)` body. Nothing in this file opens a socket.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from translog_quote.adapters.email import GmailEmailSource, parse_gmail_message
from translog_quote.errors import ContractViolation, PermanentFailure

MAILBOX = "translog.test@example.com"


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def headers(**overrides: str | None) -> list[dict[str, str]]:
    values: dict[str, str | None] = {
        "From": "Client Name <client@example.com>",
        "To": MAILBOX,
        "Subject": "Air freight enquiry BOM to JFK",
        "Date": "Tue, 26 Aug 2026 10:15:00 +0530",
        "Message-ID": "<enquiry-1@mail.example.com>",
    }
    values.update(overrides)
    return [{"name": k, "value": v} for k, v in values.items() if v is not None]


def message(
    *,
    payload: dict[str, Any] | None = None,
    labels: list[str] | None = None,
    gmail_id: str = "18f0a1b2c3d4e5f6",
    thread_id: str | None = "18f0a1b2c3d4e5f6",
    internal_date: str = "1756185900000",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": gmail_id,
        "internalDate": internal_date,
        "labelIds": labels if labels is not None else ["INBOX", "UNREAD"],
        "payload": payload
        if payload is not None
        else {
            "mimeType": "text/plain",
            "headers": headers(),
            "body": {"data": b64("500 kg general cargo, BOM to JFK, 2 pcs.")},
        },
    }
    if thread_id is not None:
        body["threadId"] = thread_id
    return body


class FakeTransport:
    """Scripted Gmail responses, keyed by request path. Records every call, so
    a test can assert that nothing beyond the expected reads happened."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append((path, params))
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, dict)
        return value


def source_over(
    responses: dict[str, Any], *, query: str = "in:inbox", max_results: int = 1
) -> tuple[GmailEmailSource, FakeTransport]:
    transport = FakeTransport(responses)
    return (
        GmailEmailSource(transport, mailbox_address=MAILBOX, query=query, max_results=max_results),
        transport,
    )


def inbox_with(*messages: dict[str, Any]) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "profile": {"emailAddress": MAILBOX},
        "messages": {"messages": [{"id": m["id"]} for m in messages]},
    }
    for m in messages:
        responses[f"messages/{m['id']}"] = m
    return responses


# --- mapping --------------------------------------------------------------------


def test_a_plain_text_message_maps_onto_raw_email() -> None:
    email = parse_gmail_message(message())

    assert email.message_id == "<enquiry-1@mail.example.com>"
    assert email.from_address == "client@example.com"  # display name stripped
    assert email.subject == "Air freight enquiry BOM to JFK"
    assert email.body_text == "500 kg general cargo, BOM to JFK, 2 pcs."
    assert email.received_at == datetime(2026, 8, 26, 10, 15, tzinfo=email.received_at.tzinfo)
    assert email.in_reply_to is None
    assert email.references == ()


def test_reply_headers_are_preserved_for_correlation() -> None:
    email = parse_gmail_message(
        message(
            payload={
                "mimeType": "text/plain",
                "headers": headers(
                    **{
                        "In-Reply-To": "<enquiry-1@mail.example.com>",
                        "References": "<root@mail.example.com> <enquiry-1@mail.example.com>",
                    }
                ),
                "body": {"data": b64("Yes, 2 pieces.")},
            }
        )
    )

    assert email.in_reply_to == "<enquiry-1@mail.example.com>"
    assert email.references == (
        "<root@mail.example.com>",
        "<enquiry-1@mail.example.com>",
    )


def test_the_plain_part_wins_in_a_multipart_alternative() -> None:
    email = parse_gmail_message(
        message(
            payload={
                "mimeType": "multipart/alternative",
                "headers": headers(),
                "body": {},
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("plain body")}},
                    {
                        "mimeType": "text/html",
                        "body": {"data": b64("<p>html body</p>")},
                    },
                ],
            }
        )
    )

    assert email.body_text == "plain body"


def test_a_nested_multipart_tree_is_walked_depth_first() -> None:
    email = parse_gmail_message(
        message(
            payload={
                "mimeType": "multipart/mixed",
                "headers": headers(),
                "body": {},
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "body": {},
                        "parts": [{"mimeType": "text/plain", "body": {"data": b64("nested body")}}],
                    },
                    {
                        "mimeType": "application/pdf",
                        "filename": "msds.pdf",
                        "body": {"attachmentId": "att-1", "size": 1024},
                    },
                ],
            }
        )
    )

    # The attachment is not downloaded and not described — Phase 10.3 reads text.
    assert email.body_text == "nested body"


def test_an_html_only_message_is_stripped_to_text_never_rendered() -> None:
    email = parse_gmail_message(
        message(
            payload={
                "mimeType": "text/html",
                "headers": headers(),
                "body": {
                    "data": b64(
                        "<html><head><style>p{color:red}</style></head><body>"
                        "<script>alert(1)</script><p>500&nbsp;kg BOM&rarr;JFK</p>"
                        "<div>2 pcs</div></body></html>"
                    )
                },
            }
        )
    )

    assert "<script>" not in email.body_text
    assert "alert(1)" not in email.body_text
    assert "color:red" not in email.body_text
    assert "500" in email.body_text
    assert "2 pcs" in email.body_text


def test_internal_date_is_the_fallback_when_the_date_header_is_unparseable() -> None:
    email = parse_gmail_message(
        message(
            payload={
                "mimeType": "text/plain",
                "headers": headers(Date="not a date"),
                "body": {"data": b64("body")},
            },
            internal_date="1756185900000",
        )
    )

    assert email.received_at == datetime.fromtimestamp(1756185900, tz=UTC)


@pytest.mark.parametrize(
    "date_header",
    [
        "Tue, 26 Aug 2026 10:15:00 -0000",  # RFC 5322: UTC, local zone unknown
        "Tue, 26 Aug 2026 10:15:00",  # no zone at all
    ],
)
def test_a_date_header_without_a_usable_zone_is_read_as_utc_never_naive(
    date_header: str,
) -> None:
    """`parsedate_to_datetime` returns a *naive* datetime for these; one naive
    `received_at` among aware ones made the poll's sort raise every cycle."""
    email = parse_gmail_message(
        message(
            payload={
                "mimeType": "text/plain",
                "headers": headers(Date=date_header),
                "body": {"data": b64("body")},
            }
        )
    )

    assert email.received_at.tzinfo is not None
    assert email.received_at == datetime(2026, 8, 26, 10, 15, tzinfo=UTC)


def test_a_date_header_with_a_real_zone_is_unchanged() -> None:
    email = parse_gmail_message(message())  # the default header: +0530

    assert email.received_at == datetime(2026, 8, 26, 4, 45, tzinfo=UTC)
    assert email.received_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_gmails_own_id_stands_in_when_a_message_id_header_is_absent() -> None:
    email = parse_gmail_message(
        message(
            payload={
                "mimeType": "text/plain",
                "headers": headers(**{"Message-ID": None}),
                "body": {"data": b64("body")},
            }
        )
    )

    assert email.message_id == "18f0a1b2c3d4e5f6"


# --- malformed responses --------------------------------------------------------


def test_a_message_without_a_payload_is_a_contract_violation() -> None:
    with pytest.raises(ContractViolation, match="no payload"):
        parse_gmail_message({"id": "x", "internalDate": "1756185900000"})


def test_a_payload_without_headers_is_a_contract_violation() -> None:
    with pytest.raises(ContractViolation, match="headers"):
        parse_gmail_message({"id": "x", "payload": {"body": {"data": b64("hi")}}})


def test_a_message_with_no_readable_text_part_is_a_contract_violation() -> None:
    with pytest.raises(ContractViolation, match="no readable text"):
        parse_gmail_message(
            message(
                payload={
                    "mimeType": "application/pdf",
                    "headers": headers(),
                    "body": {"attachmentId": "att-1"},
                }
            )
        )


def test_body_data_that_is_not_base64url_is_a_contract_violation() -> None:
    with pytest.raises(ContractViolation, match="base64url"):
        parse_gmail_message(
            message(
                payload={
                    "mimeType": "text/plain",
                    "headers": headers(),
                    "body": {"data": "!!!not base64!!!"},
                }
            )
        )


def test_an_unparseable_from_address_is_a_contract_violation() -> None:
    with pytest.raises(ContractViolation, match="From"):
        parse_gmail_message(
            message(
                payload={
                    "mimeType": "text/plain",
                    "headers": headers(From="not-an-address"),
                    "body": {"data": b64("body")},
                }
            )
        )


def test_a_malformed_list_response_is_a_contract_violation() -> None:
    source, _ = source_over({"profile": {"emailAddress": MAILBOX}, "messages": {"messages": {}}})

    with pytest.raises(ContractViolation, match="malformed"):
        source.fetch_new()


def test_a_list_entry_without_a_usable_id_is_refused() -> None:
    source, _ = source_over(
        {"profile": {"emailAddress": MAILBOX}, "messages": {"messages": [{"id": "../../evil"}]}}
    )

    with pytest.raises(ContractViolation, match="usable id"):
        source.fetch_new()


# --- selection and safety -------------------------------------------------------


def test_it_fetches_one_message_from_the_configured_query_only() -> None:
    source, transport = source_over(inbox_with(message()))

    emails = source.fetch_new()

    assert len(emails) == 1
    assert transport.calls[0] == ("profile", None)
    assert transport.calls[1] == ("messages", {"q": "in:inbox", "maxResults": "1"})
    assert transport.calls[2] == ("messages/18f0a1b2c3d4e5f6", {"format": "full"})
    assert len(transport.calls) == 3  # nothing else in the mailbox is touched


def test_it_never_reads_more_than_max_results_even_if_gmail_returns_more() -> None:
    first = message(gmail_id="aaa1")
    second = message(gmail_id="bbb2")
    responses = inbox_with(first, second)
    source, transport = source_over(responses, max_results=1)

    emails = source.fetch_new()

    assert len(emails) == 1
    assert not any(path == "messages/bbb2" for path, _ in transport.calls)


def test_an_empty_mailbox_slice_yields_nothing() -> None:
    source, _ = source_over({"profile": {"emailAddress": MAILBOX}, "messages": {}})

    assert source.fetch_new() == ()


def test_sent_only_mail_is_skipped() -> None:
    source, _ = source_over(inbox_with(message(labels=["SENT"])))

    assert source.fetch_new() == ()


def test_drafts_are_skipped() -> None:
    source, _ = source_over(inbox_with(message(labels=["DRAFT", "INBOX"])))

    assert source.fetch_new() == ()


def test_a_self_addressed_test_email_in_the_inbox_is_still_ingested() -> None:
    """The Phase 10.3 test shape: sent from the test account to itself."""
    source, _ = source_over(inbox_with(message(labels=["INBOX", "SENT"])))

    assert len(source.fetch_new()) == 1


def test_a_message_that_vanishes_between_list_and_get_is_skipped() -> None:
    from translog_quote.adapters.email.gmail import _NotFound

    responses = inbox_with(message())
    responses["messages/18f0a1b2c3d4e5f6"] = _NotFound("gone")
    source, _ = source_over(responses)

    assert source.fetch_new() == ()


def test_it_refuses_a_mailbox_that_is_not_the_configured_test_address() -> None:
    responses = inbox_with(message())
    responses["profile"] = {"emailAddress": "someone.else@example.com"}
    source, transport = source_over(responses)

    with pytest.raises(PermanentFailure, match="TRANSLOG_GMAIL__TEST_ADDRESS"):
        source.fetch_new()

    # It stopped at the profile check: no message was listed or read.
    assert transport.calls == [("profile", None)]


def test_the_mismatched_address_is_not_echoed_in_the_refusal() -> None:
    responses = inbox_with(message())
    responses["profile"] = {"emailAddress": "someone.else@example.com"}
    source, _ = source_over(responses)

    with pytest.raises(PermanentFailure) as excinfo:
        source.fetch_new()
    assert "someone.else@example.com" not in str(excinfo.value)


def test_the_address_check_is_case_insensitive() -> None:
    responses = inbox_with(message())
    responses["profile"] = {"emailAddress": MAILBOX.upper()}
    source, _ = source_over(responses)

    assert len(source.fetch_new()) == 1


def test_a_source_without_a_configured_mailbox_refuses_to_build() -> None:
    with pytest.raises(PermanentFailure, match="TRANSLOG_GMAIL__TEST_ADDRESS"):
        GmailEmailSource(FakeTransport({}), mailbox_address="")


def test_gmail_ids_are_kept_as_adapter_metadata_not_domain_fields() -> None:
    source, _ = source_over(inbox_with(message()))

    email = source.fetch_new()[0]
    metadata = source.provider_metadata(email.message_id)

    assert metadata is not None
    assert metadata.gmail_id == "18f0a1b2c3d4e5f6"
    assert metadata.thread_id == "18f0a1b2c3d4e5f6"
    # RawEmail itself stays provider-agnostic.
    assert "gmail" not in email.model_dump_json().lower()


def test_the_source_exposes_no_way_to_send_modify_or_delete() -> None:
    """The port it satisfies is EmailSource; there is no outbound surface."""
    source, _ = source_over(inbox_with(message()))

    public_names = [name for name in dir(source) if not name.startswith("_")]
    for forbidden in ("send", "modify", "delete", "trash", "draft", "reply"):
        assert not any(forbidden in name for name in public_names)


# --- operations mode: the date-bounded, paginated, oldest-first fetch (Req A) ---


def dated(gmail_id: str, date_str: str, mid: str, *, subject: str | None = None) -> dict[str, Any]:
    """A message with a specific Date header and Message-ID, for ordering tests."""
    override: dict[str, str | None] = {"Date": date_str, "Message-ID": mid}
    if subject is not None:
        override["Subject"] = subject
    return message(
        gmail_id=gmail_id,
        payload={
            "mimeType": "text/plain",
            "headers": headers(**override),
            "body": {"data": b64("500 kg, BOM to JFK, 2 pcs.")},
        },
    )


def malformed(gmail_id: str, date_str: str, mid: str) -> dict[str, Any]:
    """A well-formed envelope with **no readable text part** — the exact
    production failure. From/Date/Message-ID headers are valid, so it fails at
    the body stage inside ``parse_gmail_message`` (ContractViolation, "no
    readable text part") rather than earlier, mirroring the Account A message."""
    return message(
        gmail_id=gmail_id,
        payload={
            "mimeType": "application/pdf",
            "headers": headers(**{"Date": date_str, "Message-ID": mid}),
            "body": {"attachmentId": "att-1"},
        },
    )


class PagingTransport:
    """A Gmail transport that honours pageToken, so ``messages.list`` pagination
    can be exercised. Pages are given newest-first, as Gmail returns them."""

    def __init__(self, pages: list[tuple[list[str], str | None]], bodies: dict[str, Any]) -> None:
        self.pages = pages
        self.bodies = bodies
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self._by_token: dict[str | None, int] = {None: 0}
        for index, (_ids, token) in enumerate(pages):
            if token is not None:
                self._by_token[token] = index + 1

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append((path, params))
        if path == "profile":
            return {"emailAddress": MAILBOX}
        if path == "messages":
            index = self._by_token[(params or {}).get("pageToken")]
            ids, token = self.pages[index]
            response: dict[str, Any] = {"messages": [{"id": i} for i in ids]}
            if token is not None:
                response["nextPageToken"] = token
            return response
        return self.bodies[path]


def _ops_source(
    transport: PagingTransport,
    *,
    max_results: int,
    is_internal: Any = None,
) -> GmailEmailSource:
    return GmailEmailSource(
        transport,
        mailbox_address=MAILBOX,
        max_results=max_results,
        overlap_seconds=0.0,
        is_internal=is_internal,
    )


def test_operations_fetch_is_date_bounded_paginated_and_oldest_first() -> None:
    """`since` builds an `after:` query, pages the id list to the end, and
    returns messages oldest-first — the order a backlog must be drained in."""
    bodies = {
        "messages/m1": dated("m1", "Mon, 24 Aug 2026 09:00:00 +0000", "<m1@x>"),
        "messages/m2": dated("m2", "Tue, 25 Aug 2026 09:00:00 +0000", "<m2@x>"),
        "messages/m3": dated("m3", "Wed, 26 Aug 2026 09:00:00 +0000", "<m3@x>"),
    }
    transport = PagingTransport([(["m3", "m2"], "t1"), (["m1"], None)], bodies)
    source = _ops_source(transport, max_results=10)

    since = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    emails = source.fetch_new(since=since)

    assert [e.message_id for e in emails] == ["<m1@x>", "<m2@x>", "<m3@x>"]
    # The listing query is date-bounded and it paged to the end (two list calls).
    list_calls = [p for path, p in transport.calls if path == "messages"]
    assert all("after:" in (p or {})["q"] for p in list_calls)
    assert len(list_calls) == 2


def test_operations_fetch_budget_takes_the_oldest_and_leaves_the_rest() -> None:
    """A window larger than the per-poll budget is drained oldest-first; the
    newest are left for the next poll rather than the oldest being starved."""
    bodies = {
        "messages/m1": dated("m1", "Mon, 24 Aug 2026 09:00:00 +0000", "<m1@x>"),
        "messages/m2": dated("m2", "Tue, 25 Aug 2026 09:00:00 +0000", "<m2@x>"),
        "messages/m3": dated("m3", "Wed, 26 Aug 2026 09:00:00 +0000", "<m3@x>"),
    }
    transport = PagingTransport([(["m3", "m2", "m1"], None)], bodies)
    source = _ops_source(transport, max_results=2)

    emails = source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert [e.message_id for e in emails] == ["<m1@x>", "<m2@x>"]
    # m3 (the newest) was never fetched — the budget stopped first.
    assert "messages/m3" not in [path for path, _ in transport.calls]


def test_internal_mail_does_not_consume_the_operations_budget() -> None:
    """An internal/approval message is skipped without spending a client slot, so
    the budget still yields the intended number of client messages."""
    bodies = {
        "messages/m1": dated("m1", "Mon, 24 Aug 2026 09:00:00 +0000", "<m1@x>"),
        "messages/m2": dated(
            "m2", "Tue, 25 Aug 2026 09:00:00 +0000", "<m2@x>", subject="[TRANSLOG INTERNAL] review"
        ),
        "messages/m3": dated("m3", "Wed, 26 Aug 2026 09:00:00 +0000", "<m3@x>"),
    }
    transport = PagingTransport([(["m3", "m2", "m1"], None)], bodies)
    source = _ops_source(
        transport,
        max_results=2,
        is_internal=lambda e: e.subject.strip().startswith("[TRANSLOG INTERNAL]"),
    )

    emails = source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert [e.message_id for e in emails] == ["<m1@x>", "<m3@x>"]


# --- operations mode: an already-handled message is skipped, not starving (Req A) ---


def test_a_seen_message_does_not_consume_the_operations_budget() -> None:
    """A message already handled this run is skipped without spending a client
    slot, exactly like internal mail — so the budget still yields the intended
    number of *unhandled* client messages rather than being burned re-reading
    settled ones."""
    bodies = {
        "messages/m1": dated("m1", "Mon, 24 Aug 2026 09:00:00 +0000", "<m1@x>"),
        "messages/m2": dated("m2", "Tue, 25 Aug 2026 09:00:00 +0000", "<m2@x>"),
        "messages/m3": dated("m3", "Wed, 26 Aug 2026 09:00:00 +0000", "<m3@x>"),
    }
    transport = PagingTransport([(["m3", "m2", "m1"], None)], bodies)
    seen = {"<m1@x>"}
    source = GmailEmailSource(
        transport,
        mailbox_address=MAILBOX,
        max_results=2,
        overlap_seconds=0.0,
        seen=lambda mid: mid in seen,
    )

    emails = source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    # m1 was skipped as already-handled; the budget of 2 reached m2 and m3.
    assert [e.message_id for e in emails] == ["<m2@x>", "<m3@x>"]


def test_a_backlog_of_seen_drafts_does_not_starve_a_newer_message() -> None:
    """The starvation fix, at the fetch. Several old unresolved drafts sit at the
    front of the oldest-first window; once handled they are ``seen``, so a small
    per-poll budget is no longer spent re-reading them and reaches the newer
    first-contact message that would otherwise never be fetched."""
    bodies = {
        "messages/d1": dated("d1", "Mon, 24 Aug 2026 09:00:00 +0000", "<d1@x>"),
        "messages/d2": dated("d2", "Tue, 25 Aug 2026 09:00:00 +0000", "<d2@x>"),
        "messages/d3": dated("d3", "Wed, 26 Aug 2026 09:00:00 +0000", "<d3@x>"),
        "messages/new": dated("new", "Thu, 27 Aug 2026 09:00:00 +0000", "<new@x>"),
    }
    # Gmail lists newest-first; reversed to oldest-first the drafts come before
    # the newer message, and a budget of 2 would stop before it.
    transport = PagingTransport([(["new", "d3", "d2", "d1"], None)], bodies)
    seen = {"<d1@x>", "<d2@x>", "<d3@x>"}
    source = GmailEmailSource(
        transport,
        mailbox_address=MAILBOX,
        max_results=2,
        overlap_seconds=0.0,
        seen=lambda mid: mid in seen,
    )

    emails = source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert [e.message_id for e in emails] == ["<new@x>"]


def test_a_reply_to_a_seen_thread_is_still_fetched() -> None:
    """The reply carries a new Message-ID, so it is not ``seen`` even though the
    enquiry it answers is — a clarification reply to an old draft is fetched and
    processed, never mistaken for the settled enquiry beneath it."""
    bodies = {
        "messages/enq": dated("enq", "Mon, 24 Aug 2026 09:00:00 +0000", "<enq@x>"),
        "messages/rep": dated("rep", "Tue, 25 Aug 2026 09:00:00 +0000", "<rep@x>"),
    }
    transport = PagingTransport([(["rep", "enq"], None)], bodies)
    seen = {"<enq@x>"}  # the enquiry is handled; the reply is not
    source = GmailEmailSource(
        transport,
        mailbox_address=MAILBOX,
        max_results=1,
        overlap_seconds=0.0,
        seen=lambda mid: mid in seen,
    )

    emails = source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert [e.message_id for e in emails] == ["<rep@x>"]


def test_the_seen_filter_does_not_apply_to_the_demonstration_newest_slice() -> None:
    """``seen`` is an operations-path skip only. The demonstration newest-first
    read (``since is None``) ignores it, so nothing about the button-driven demo
    changes."""
    seen = {"<enquiry-1@mail.example.com>"}
    source, _ = source_over(inbox_with(message()), max_results=1)
    source._seen = lambda mid: mid in seen  # type: ignore[attr-defined]

    emails = source.fetch_new()  # since is None -> newest slice

    assert [e.message_id for e in emails] == ["<enquiry-1@mail.example.com>"]


# --- operations mode: one malformed message must not abort the whole poll -------


def test_a_malformed_message_does_not_abort_the_operations_fetch() -> None:
    """The production incident, at the fetch. A message with no readable text part
    used to raise a ContractViolation out of ``_ingest`` that aborted the entire
    account poll. Now it is caught and the poll completes, returning the valid
    mail around it."""
    bodies = {
        "messages/bad": malformed("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad@x>"),
        "messages/m2": dated("m2", "Tue, 25 Aug 2026 09:00:00 +0000", "<m2@x>"),
        "messages/m3": dated("m3", "Wed, 26 Aug 2026 09:00:00 +0000", "<m3@x>"),
    }
    transport = PagingTransport([(["m3", "m2", "bad"], None)], bodies)
    source = _ops_source(transport, max_results=10)

    emails = source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert [e.message_id for e in emails] == ["<m2@x>", "<m3@x>"]


def test_a_malformed_message_is_quarantined_by_its_gmail_id() -> None:
    """Not silently dropped: its Gmail id is recorded in the source's quarantine
    set so the poll can move past it and later skip it before re-fetching."""
    bodies = {
        "messages/bad": malformed("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad@x>"),
        "messages/m2": dated("m2", "Tue, 25 Aug 2026 09:00:00 +0000", "<m2@x>"),
    }
    transport = PagingTransport([(["m2", "bad"], None)], bodies)
    source = _ops_source(transport, max_results=10)

    source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert source._unparseable == {"bad"}  # type: ignore[attr-defined]


def test_a_malformed_message_does_not_spend_the_budget_so_newer_mail_is_reached() -> None:
    """Like the internal/seen skips: the malformed message is quarantined without
    consuming a client slot, so a tight per-poll budget still reaches the newer
    valid message behind it instead of being burned on the unparseable one."""
    bodies = {
        "messages/bad": malformed("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad@x>"),
        "messages/good": dated("good", "Tue, 25 Aug 2026 09:00:00 +0000", "<good@x>"),
    }
    transport = PagingTransport([(["good", "bad"], None)], bodies)
    source = _ops_source(transport, max_results=1)

    emails = source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert [e.message_id for e in emails] == ["<good@x>"]


def test_a_quarantined_message_is_not_re_fetched_on_a_later_poll() -> None:
    """Once quarantined it is skipped *before* the re-fetch, so a message the
    overlap window keeps re-listing neither re-spends the budget nor re-raises —
    the fix that stops the quota re-burn seen in production."""
    bodies = {
        "messages/bad": malformed("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad@x>"),
        "messages/good": dated("good", "Tue, 25 Aug 2026 09:00:00 +0000", "<good@x>"),
    }
    transport = PagingTransport([(["good", "bad"], None)], bodies)
    source = _ops_source(transport, max_results=10)

    source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))
    after_first = len(transport.calls)
    source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    second_poll_gets = [
        path for path, _ in transport.calls[after_first:] if path.startswith("messages/")
    ]
    assert "messages/bad" not in second_poll_gets  # skipped before the fetch
    assert "messages/good" in second_poll_gets  # a normal message is still read


def test_a_malformed_message_never_becomes_a_raw_email() -> None:
    """It cannot create a request downstream because it is never returned as a
    RawEmail at all, and leaves no provider metadata to resurrect it by."""
    bodies = {"messages/bad": malformed("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad@x>")}
    transport = PagingTransport([(["bad"], None)], bodies)
    source = _ops_source(transport, max_results=10)

    emails = source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert emails == ()
    assert source.provider_metadata("<bad@x>") is None


def test_a_malformed_and_a_seen_message_are_both_skipped_to_reach_newer_mail() -> None:
    """The quarantine composes with the seen-skip starvation fix: an unparseable
    message and an already-handled one are both stepped over without spending the
    budget, so a tight budget still reaches the fresh client message behind them.
    Existing seen-skip behaviour is unchanged."""
    bodies = {
        "messages/bad": malformed("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad@x>"),
        "messages/seen": dated("seen", "Tue, 25 Aug 2026 09:00:00 +0000", "<seen@x>"),
        "messages/new": dated("new", "Wed, 26 Aug 2026 09:00:00 +0000", "<new@x>"),
    }
    transport = PagingTransport([(["new", "seen", "bad"], None)], bodies)
    seen = {"<seen@x>"}
    source = GmailEmailSource(
        transport,
        mailbox_address=MAILBOX,
        max_results=1,
        overlap_seconds=0.0,
        seen=lambda mid: mid in seen,
    )

    emails = source.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert [e.message_id for e in emails] == ["<new@x>"]
    assert source._unparseable == {"bad"}  # type: ignore[attr-defined]


def test_quarantine_state_is_isolated_per_source_instance() -> None:
    """Each account has its own GmailEmailSource, so one mailbox quarantining a
    malformed message never causes another account's source to skip its own — even
    when the two happen to share a Gmail id."""
    source_a = _ops_source(
        PagingTransport(
            [(["bad"], None)],
            {"messages/bad": malformed("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad-a@x>")},
        ),
        max_results=10,
    )
    source_b = _ops_source(
        PagingTransport(
            [(["bad"], None)],
            {"messages/bad": dated("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad-b@x>")},
        ),
        max_results=10,
    )

    source_a.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert source_a._unparseable == {"bad"}  # type: ignore[attr-defined]
    assert source_b._unparseable == set()  # type: ignore[attr-defined]
    # Account B still reads its own message with the same Gmail id normally.
    emails_b = source_b.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))
    assert [e.message_id for e in emails_b] == ["<bad-b@x>"]


def test_a_fresh_source_re_parses_and_re_quarantines_after_a_restart() -> None:
    """The quarantine is in-memory and per-session by design. A restart builds a
    fresh source with an empty set, so it re-fetches and re-parses the malformed
    message once, then re-quarantines it — safe, exactly as a restart re-derives
    an uncommitted draft. It does not skip the newer valid mail."""
    bodies = {
        "messages/bad": malformed("bad", "Mon, 24 Aug 2026 09:00:00 +0000", "<bad@x>"),
        "messages/good": dated("good", "Tue, 25 Aug 2026 09:00:00 +0000", "<good@x>"),
    }
    first = _ops_source(PagingTransport([(["good", "bad"], None)], bodies), max_results=10)
    first.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))
    assert first._unparseable == {"bad"}  # type: ignore[attr-defined]

    restart_transport = PagingTransport([(["good", "bad"], None)], bodies)
    second = _ops_source(restart_transport, max_results=10)
    assert second._unparseable == set()  # type: ignore[attr-defined]

    emails = second.fetch_new(since=datetime(2026, 8, 20, tzinfo=UTC))

    assert "messages/bad" in [path for path, _ in restart_transport.calls]  # re-parsed once
    assert second._unparseable == {"bad"}  # type: ignore[attr-defined]
    assert [e.message_id for e in emails] == ["<good@x>"]
