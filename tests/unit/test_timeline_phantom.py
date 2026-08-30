"""The timeline shows what happened, never what could have happened.

Found on a live screenshot: a complete enquiry — one that validated on first
sight and went straight to rate selection — displayed "Clarification awaiting
approval / Waiting for a person to approve and send" in its timeline. The
template carried a fixed clarification step, so a request that never entered
the clarification loop rendered the step as its *current* one, inventing a
pending human action nobody needed to take.

The rule pinned here: the clarification and client-reply steps appear exactly
when the request actually entered the clarification loop — evidenced by its
audit trail, a held draft, the NEEDS_INFO state, or a merged reply — and never
otherwise. History is shown; nothing is invented.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    ENQUIRY_EXTRACTION,
    REPLY,
    REPLY_EXTRACTION,
    ScriptedExtractor,
    StubSource,
)
from tests.unit.test_web_live import GrowingSource

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.interface.web.live_serialize import timeline_json
from translog_quote.interface.web.live_session import LiveSession

APPROVER = "ops.manager@translog.example"


@pytest.fixture
def settings() -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
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


COMPLETE_ENQUIRY = RawEmail(
    message_id="<complete-1@mail.example.com>",
    from_address="client@example.com",
    subject="Rate required - Mumbai to Dubai",
    body_text="Everything stated.",
    received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
)

COMPLETE_EXTRACTION = ExtractionResult(
    origin=ExtractedValue[str].stated("Mumbai"),
    destination=ExtractedValue[str].stated("Dubai"),
    weight_kg=ExtractedValue[float].stated(500.0),
    dimensions_in=ExtractedValue[CargoDimensions].stated(
        CargoDimensions(length=34, width=24, height=6)
    ),
    commodity=ExtractedValue[str].stated("Engineering components"),
    cargo_type=ExtractedValue[str].stated("Non-Haz"),
    is_chemical=ExtractedValue[bool].stated(value=False),
    pcs=ExtractedValue[int].stated(10),
    delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
)


def _timeline(session: LiveSession) -> list[dict[str, object]]:
    request = next(iter(session.requests.values()))
    return timeline_json(request, session.audit.events)  # type: ignore[arg-type]


def _keys(rows: list[dict[str, object]]) -> list[object]:
    return [row["key"] for row in rows]


def _row(rows: list[dict[str, object]], key: str) -> dict[str, object]:
    matching = [row for row in rows if row["key"] == key]
    assert len(matching) == 1, f"expected exactly one {key!r} row, got {len(matching)}"
    return matching[0]


# --- 1. a complete enquiry carries no clarification step ------------------------


def test_a_complete_enquiry_shows_no_clarification_step(
    settings: Settings,
) -> None:
    """The regression itself. These two rows were rendered for every request."""
    session = LiveSession(
        settings,
        source=StubSource(COMPLETE_ENQUIRY),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(COMPLETE_EXTRACTION),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )
    session.poll()

    rows = _timeline(session)
    keys = _keys(rows)

    assert "clarification_sent" not in keys
    assert "reply_received" not in keys
    labels = [row["label"] for row in rows]
    assert "Clarification awaiting approval" not in labels
    assert all(row["note"] != "Waiting for a person to approve and send" for row in rows)


def test_a_complete_enquiry_is_current_on_human_approval(settings: Settings) -> None:
    """With the phantom rows gone, the current step is the real next action."""
    session = LiveSession(
        settings,
        source=StubSource(COMPLETE_ENQUIRY),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(COMPLETE_EXTRACTION),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )
    session.poll()

    rows = _timeline(session)

    current = [row for row in rows if row["state"] == "current"]
    assert [row["key"] for row in current] == ["approval_decided"]
    assert current[0]["note"] == "Waiting for approval"
    assert _keys(rows) == [
        "enquiry_received",
        "extraction",
        "validation",
        "rate_search",
        "rate_selected",
        "approval_decided",
        "quotation_sent",
    ]


# --- 2. an incomplete enquiry shows the step it is actually sitting on ----------


def test_an_incomplete_enquiry_still_shows_the_clarification_step(
    settings: Settings,
) -> None:
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )
    session.poll()

    rows = _timeline(session)
    clarification = _row(rows, "clarification_sent")

    assert clarification["state"] == "current"
    assert clarification["label"] == "Clarification awaiting approval"
    assert clarification["note"] == "Waiting for a person to approve and send"
    assert _row(rows, "reply_received")["state"] == "pending"


# --- 3. history is shown once it exists, and only then --------------------------


def test_after_the_reply_the_clarification_steps_are_shown_as_history(
    settings: Settings,
) -> None:
    session = LiveSession(
        settings,
        source=GrowingSource((ENQUIRY,), (ENQUIRY, REPLY)),  # type: ignore[arg-type]
        sink=CollectingEmailSink(),
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()

    rows = _timeline(session)

    clarification = _row(rows, "clarification_sent")
    assert clarification["state"] == "done"
    assert clarification["label"] == "Clarification sent"
    reply = _row(rows, "reply_received")
    assert reply["state"] == "done"
    assert reply["at"] == (ENQUIRY.received_at + timedelta(hours=4)).isoformat()
