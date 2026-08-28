"""Gmail response → RawEmail, and the safety rules around which messages the
source is allowed to touch.

Every Gmail response here is a canned dictionary shaped like the documented
`users.messages.get(format=full)` body. Nothing in this file opens a socket.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
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
