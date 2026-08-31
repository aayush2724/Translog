"""A reply whose answer cannot be used goes to a person, not back into the loop.

Reproduced from a live production test. An enquiry was missing dimensions; the
clarification asked for them; the client answered — completely and truthfully:

    Dimensions:
    - 8 packages: 48 x 32 x 28 inches each
    - 6 packages: 36 x 24 x 20 inches each

The canonical record holds exactly one L x W x H, so extraction correctly
returned AMBIGUOUS (BR-7: never guess) with the note "The email provides two
different sets of dimensions for different groups of packages". Ambiguous maps
to None, the merge had nothing to fill, and the workflow drafted the identical
question again — a dead loop, because the client cannot answer differently:
their shipment genuinely has two package sizes.

The corrected behaviour pinned here: when a client has answered a clarification
and a required field still cannot be filled because their answer was ambiguous,
the request escalates to MANUAL_REVIEW — carrying the model's own note so the
operator can see why — instead of re-asking. First-contact ambiguity still
clarifies (that is what clarification is for), and a reply that answers cleanly
still proceeds to rate search untouched.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.test_gmail_thread import ScriptedExtractor, StubSource

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import StatedLocationResolver
from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.web.live_serialize import request_detail, request_summary
from translog_quote.interface.web.live_session import LiveSession

APPROVER = "A. Operator"

TWO_SIZES_NOTE = (
    "The email provides two different sets of dimensions for different groups of packages."
)

ENQUIRY = RawEmail(
    message_id="<milan-enquiry@mail.example.com>",
    from_address="client@example.com",
    subject="Air Freight Quote - Bengaluru to Milan",
    body_text="685 kg, Bengaluru to Milan, hazardous, MSDS available, 14 pieces.",
    received_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
)

REPLY = RawEmail(
    message_id="<milan-reply@mail.example.com>",
    from_address="client@example.com",
    subject="Re: Air Freight Quote - Bengaluru to Milan",
    body_text=(
        "Dimensions:\n"
        "- 8 packages: 48 x 32 x 28 inches each\n"
        "- 6 packages: 36 x 24 x 20 inches each\n"
    ),
    received_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    in_reply_to=ENQUIRY.message_id,
    references=(ENQUIRY.message_id,),
)


def enquiry_extraction() -> ExtractionResult:
    """Everything except dimensions, so exactly that field is asked for."""
    return ExtractionResult(
        origin=ExtractedValue[str].stated("Bengaluru, India"),
        destination=ExtractedValue[str].stated("Milan, Italy"),
        weight_kg=ExtractedValue[float].stated(685.0),
        commodity=ExtractedValue[str].stated("Testing equipment"),
        cargo_type=ExtractedValue[str].stated("Hazardous"),
        is_chemical=ExtractedValue[bool].stated(value=False),
        msds_attached=ExtractedValue[bool].stated(value=True),
        pcs=ExtractedValue[int].stated(14),
        delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
    )


def ambiguous_dimensions_reply() -> ExtractionResult:
    """What the real model returned for the two-sizes answer."""
    return ExtractionResult(
        dimensions_in=ExtractedValue[CargoDimensions].ambiguous(note=TWO_SIZES_NOTE),
    )


def clean_dimensions_reply() -> ExtractionResult:
    return ExtractionResult(
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=48, width=32, height=28)
        ),
    )


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


def session_after_reply(
    settings: Settings, sink: CollectingEmailSink, reply_extraction: ExtractionResult
) -> LiveSession:
    """The full journey: enquiry -> clarification approved -> client reply."""
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(enquiry_extraction(), reply_extraction),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )
    session.poll()
    request = next(iter(session.requests.values()))
    assert request.state is RequestState.NEEDS_INFO
    session.approve_clarification(by=APPROVER, request_id=request.request_id)
    session._source = StubSource(ENQUIRY, REPLY)  # type: ignore[assignment,arg-type]
    session.poll()
    return session


def only(session: LiveSession) -> object:
    return next(iter(session.requests.values()))


# --- the regression -------------------------------------------------------------


def test_an_unusable_answer_escalates_instead_of_re_asking(settings: Settings) -> None:
    sink = CollectingEmailSink()
    session = session_after_reply(settings, sink, ambiguous_dimensions_reply())

    request = only(session)
    assert request.state is RequestState.MANUAL_REVIEW  # type: ignore[attr-defined]
    assert request.clarification is None, "no round-2 draft may exist"  # type: ignore[attr-defined]
    assert request.awaiting_clarification_approval is False  # type: ignore[attr-defined]
    assert len(sink.sent) == 1, "only the original clarification ever went out"


def test_the_operator_sees_why(settings: Settings) -> None:
    sink = CollectingEmailSink()
    session = session_after_reply(settings, sink, ambiguous_dimensions_reply())
    request = only(session)

    assert request.manual_review_notes == (TWO_SIZES_NOTE,)  # type: ignore[attr-defined]
    detail = request_detail(session, request)  # type: ignore[arg-type]
    assert detail["manual_review_notes"] == [TWO_SIZES_NOTE]
    summary = request_summary(request)  # type: ignore[arg-type]
    assert summary["status"]["label"] == "MANUAL REVIEW"  # type: ignore[index]


def test_the_escalation_is_audited_with_the_note(settings: Settings) -> None:
    sink = CollectingEmailSink()
    session = session_after_reply(settings, sink, ambiguous_dimensions_reply())

    escalations = [e for e in session.audit.events if e.event.value == "manual_review_escalated"]
    assert len(escalations) == 1
    assert escalations[0].detail == {
        "fields": ["dimensions_in"],
        "notes": [TWO_SIZES_NOTE],
    }


def test_the_escalated_state_is_persisted(settings: Settings) -> None:
    """A restart must not resurrect the dead loop."""
    sink = CollectingEmailSink()
    session = session_after_reply(settings, sink, ambiguous_dimensions_reply())
    request_id = only(session).request_id  # type: ignore[attr-defined]

    stored = session._durable.get_request(request_id)
    assert stored is not None
    assert stored.state is RequestState.MANUAL_REVIEW


def test_a_further_poll_does_not_revive_the_request(settings: Settings) -> None:
    sink = CollectingEmailSink()
    session = session_after_reply(settings, sink, ambiguous_dimensions_reply())

    session.poll()
    session.poll()

    assert only(session).state is RequestState.MANUAL_REVIEW  # type: ignore[attr-defined]
    assert len(sink.sent) == 1


# --- what must NOT change -------------------------------------------------------


def test_first_contact_ambiguity_still_clarifies(settings: Settings) -> None:
    """An ambiguous field in the FIRST email is what clarification exists for."""
    sink = CollectingEmailSink()
    first = enquiry_extraction().model_copy(
        update={
            "dimensions_in": ExtractedValue[CargoDimensions].ambiguous(note=TWO_SIZES_NOTE),
        }
    )
    session = LiveSession(
        settings,
        source=StubSource(ENQUIRY),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(first),  # type: ignore[arg-type]
        resolver=StatedLocationResolver(),
    )
    session.poll()

    request = only(session)
    assert request.state is RequestState.NEEDS_INFO  # type: ignore[attr-defined]
    assert request.awaiting_clarification_approval is True  # type: ignore[attr-defined]


def test_a_clean_answer_still_proceeds_to_rate_search(settings: Settings) -> None:
    sink = CollectingEmailSink()
    session = session_after_reply(settings, sink, clean_dimensions_reply())

    request = only(session)
    assert request.state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    assert request.manual_review_notes == ()  # type: ignore[attr-defined]
    assert request.awaiting_quotation_decision is True  # type: ignore[attr-defined]
