"""The `gmail-thread` command, driven offline.

The mailbox and the model are stubbed; the router, policy, workflow, merge and
validator are all real. What these tests check is the command's own behaviour:
the order it processes a conversation in, where it stops, and what it refuses.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest

from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.errors import PermanentFailure
from translog_quote.interface.demo import gmail_thread
from translog_quote.interface.demo.gmail_thread import (
    EXIT_AWAITING_APPROVAL,
    EXIT_CONFIG,
    EXIT_GMAIL,
    EXIT_NO_MESSAGE,
    EXIT_OK,
    _request_id_for,
    run_gmail_thread,
)

FAKE_KEY = "test-not-a-real-credential"
APPROVER = "ops.manager@translog.example"

ENQUIRY_ID = "<enquiry-1@mail.example.com>"
REPLY_ID = "<reply-1@mail.example.com>"

ENQUIRY = RawEmail(
    message_id=ENQUIRY_ID,
    from_address="client@example.com",
    subject="Rate required - Ahmedabad to Bahrain",
    body_text="500 KG, Ahmedabad to Bahrain, 24 x 34 x 6 inches, Non-Haz.",
    received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
)

REPLY = RawEmail(
    message_id=REPLY_ID,
    from_address="client@example.com",
    subject="Re: Rate required - Ahmedabad to Bahrain",
    body_text=(
        "Commodity: Engineering components\n"
        "Chemical: No\n"
        "Pieces: 10 cartons\n"
        "Delivery: Airport to airport"
    ),
    received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC) + timedelta(hours=4),
    in_reply_to=ENQUIRY_ID,
    references=(ENQUIRY_ID,),
)

ENQUIRY_EXTRACTION = ExtractionResult(
    origin=ExtractedValue[str].stated("Ahmedabad"),
    destination=ExtractedValue[str].stated("Bahrain"),
    weight_kg=ExtractedValue[float].stated(500.0),
    dimensions_in=ExtractedValue[CargoDimensions].stated(
        CargoDimensions(length=34, width=24, height=6)
    ),
    cargo_type=ExtractedValue[str].stated("Non-Haz"),
)

REPLY_EXTRACTION = ExtractionResult(
    commodity=ExtractedValue[str].stated("Engineering components"),
    is_chemical=ExtractedValue[bool].stated(value=False),
    pcs=ExtractedValue[int].stated(10),
    delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
)


class StubSource:
    def __init__(self, *emails: RawEmail) -> None:
        self._emails = emails

    def fetch_new(self) -> tuple[RawEmail, ...]:
        return self._emails


class ScriptedExtractor:
    def __init__(self, *results: ExtractionResult) -> None:
        self._results = list(results)
        self.calls: list[str] = []

    def extract_shipment(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        return self._results.pop(0)

    def read_client_intent(self, text: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class SpySink:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def send(self, message: object) -> None:
        self.sent.append(message)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TRANSLOG_OPENROUTER__API_KEY", FAKE_KEY)
    monkeypatch.setenv("TRANSLOG_GMAIL__TEST_ADDRESS", "translog.test@example.com")
    return Settings()


def wire(
    monkeypatch: pytest.MonkeyPatch,
    *emails: RawEmail,
    extractions: tuple[ExtractionResult, ...] = (),
) -> tuple[SpySink, list[int]]:
    """Stub the mailbox and the model; keep the real router and workflow."""
    sink = SpySink()
    extractor = ScriptedExtractor(*extractions)
    limits: list[int] = []
    real_router = gmail_thread.bootstrap.build_inbound_router

    def source(_settings: Settings, *, max_results: int | None = None) -> StubSource:
        if max_results is not None:
            limits.append(max_results)
        return StubSource(*emails)

    def router(settings_arg: Settings, **kwargs: object) -> object:
        kwargs.pop("extractor", None)
        return real_router(settings_arg, extractor=extractor, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gmail_thread.bootstrap, "build_gmail_email_source", source)
    monkeypatch.setattr(gmail_thread.bootstrap, "build_inbound_router", router)
    monkeypatch.setattr(gmail_thread.bootstrap, "build_outbox_sink", lambda *a, **k: sink)
    return sink, limits


# --- the full conversation ------------------------------------------------------


def test_an_approved_conversation_ends_validated(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, REPLY, ENQUIRY, extractions=(ENQUIRY_EXTRACTION, REPLY_EXTRACTION))
    out = io.StringIO()

    code = run_gmail_thread(settings=settings, approved_by=APPROVER, out=out)

    assert code == EXIT_OK
    text = out.getvalue()
    assert "REPLY -> correlated" in text
    assert "Status: VALID" in text


def test_the_conversation_is_processed_oldest_first(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Gmail lists newest first; the enquiry must be processed before the reply
    or there is nothing for the reply to correlate to."""
    wire(monkeypatch, REPLY, ENQUIRY, extractions=(ENQUIRY_EXTRACTION, REPLY_EXTRACTION))
    out = io.StringIO()

    run_gmail_thread(settings=settings, approved_by=APPROVER, out=out)

    text = out.getvalue()
    assert text.index("MESSAGE 1  (NEW ENQUIRY)") < text.index("MESSAGE 2  (REPLY -> correlated)")


def test_the_merged_shipment_keeps_what_the_reply_never_repeated(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, REPLY, ENQUIRY, extractions=(ENQUIRY_EXTRACTION, REPLY_EXTRACTION))
    out = io.StringIO()

    run_gmail_thread(settings=settings, approved_by=APPROVER, out=out)

    merged = out.getvalue().split("MERGED SHIPMENT")[1]
    for carried_over in ("Ahmedabad", "Bahrain", "500 kg", "34 (L) x 24 (W) x 6 (H)"):
        assert carried_over in merged
    for supplied_by_reply in ("Engineering components", "10 pieces", "airport"):
        assert supplied_by_reply in merged


def test_exactly_the_two_messages_reach_the_model(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Two scripted results, both consumed and no more: a third extraction
    would have raised IndexError, and an unconsumed one means a skipped
    message."""
    wire(monkeypatch, REPLY, ENQUIRY, extractions=(ENQUIRY_EXTRACTION, REPLY_EXTRACTION))
    out = io.StringIO()

    assert run_gmail_thread(settings=settings, approved_by=APPROVER, out=out) == EXIT_OK


# --- the approval gate ----------------------------------------------------------


def test_without_an_approver_the_run_stops_at_the_gate(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    sink, _ = wire(monkeypatch, REPLY, ENQUIRY, extractions=(ENQUIRY_EXTRACTION,))
    out = io.StringIO()

    code = run_gmail_thread(settings=settings, approved_by=None, out=out)

    assert code == EXIT_AWAITING_APPROVAL
    text = out.getvalue()
    assert "HUMAN APPROVAL REQUIRED" in text
    assert "--approved-by" in text
    assert sink.sent == []
    # The reply was never processed: only the enquiry's extraction was consumed.
    assert "MESSAGE 2" not in text


def test_the_gate_message_says_nothing_will_be_emailed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, REPLY, ENQUIRY, extractions=(ENQUIRY_EXTRACTION,))
    out = io.StringIO()

    run_gmail_thread(settings=settings, approved_by=None, out=out)

    assert "sends no email" in out.getvalue()


def test_approving_records_the_named_person(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, REPLY, ENQUIRY, extractions=(ENQUIRY_EXTRACTION, REPLY_EXTRACTION))
    out = io.StringIO()

    run_gmail_thread(settings=settings, approved_by=APPROVER, out=out)

    assert f"approved by {APPROVER}" in out.getvalue()


# --- refusal --------------------------------------------------------------------


def test_an_uncorrelatable_reply_is_reported_as_manual_review(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Two separate enquiries, then a reply whose chain spans both."""
    second_enquiry = ENQUIRY.model_copy(
        update={
            "message_id": "<enquiry-2@mail.example.com>",
            "received_at": ENQUIRY.received_at + timedelta(hours=1),
        }
    )
    ambiguous = REPLY.model_copy(
        update={
            "in_reply_to": None,
            "references": (ENQUIRY_ID, "<enquiry-2@mail.example.com>"),
        }
    )
    wire(
        monkeypatch,
        ambiguous,
        second_enquiry,
        ENQUIRY,
        extractions=(ENQUIRY_EXTRACTION, ENQUIRY_EXTRACTION),
    )
    out = io.StringIO()

    run_gmail_thread(settings=settings, approved_by=APPROVER, out=out)

    text = out.getvalue()
    assert "REFUSED — MANUAL REVIEW" in text
    assert "more than one known request" in text


def test_a_fake_re_subject_with_no_threading_headers_is_explained(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """A message composed fresh with "Re:" typed in looks like a reply to a
    person. It correctly becomes its own enquiry, and the run says why."""
    unthreaded = REPLY.model_copy(update={"in_reply_to": None, "references": ()})
    wire(monkeypatch, unthreaded, ENQUIRY, extractions=(ENQUIRY_EXTRACTION, REPLY_EXTRACTION))
    out = io.StringIO()

    run_gmail_thread(settings=settings, approved_by=APPROVER, out=out)

    text = out.getvalue()
    assert "MESSAGE 2  (NEW ENQUIRY)" in text
    assert "it is not a reply" in text
    assert "Reply button" in text


def test_a_genuine_reply_gets_no_such_note(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, REPLY, ENQUIRY, extractions=(ENQUIRY_EXTRACTION, REPLY_EXTRACTION))
    out = io.StringIO()

    run_gmail_thread(settings=settings, approved_by=APPROVER, out=out)

    assert "it is not a reply" not in out.getvalue()


# --- reading the mailbox --------------------------------------------------------


def test_it_asks_for_a_conversation_not_the_whole_mailbox(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _, limits = wire(monkeypatch, ENQUIRY, extractions=(ENQUIRY_EXTRACTION,))

    run_gmail_thread(settings=settings, approved_by=APPROVER, limit=5, out=io.StringIO())

    assert limits == [5]


def test_an_empty_mailbox_is_reported(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    wire(monkeypatch)
    out = io.StringIO()

    assert run_gmail_thread(settings=settings, out=out) == EXIT_NO_MESSAGE
    assert "NO MESSAGE FOUND" in out.getvalue()


def test_a_gmail_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    def refuse(_settings: Settings, **kwargs: object) -> object:
        raise PermanentFailure("No Gmail token file at .secrets/gmail_token.json.")

    monkeypatch.setattr(gmail_thread.bootstrap, "build_gmail_email_source", refuse)
    out = io.StringIO()

    assert run_gmail_thread(settings=settings, out=out) == EXIT_GMAIL
    assert "GMAIL RECEIVE FAILED" in out.getvalue()


def test_a_missing_api_key_stops_before_any_gmail_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSLOG_OPENROUTER__API_KEY", raising=False)
    called: list[str] = []
    monkeypatch.setattr(
        gmail_thread.bootstrap,
        "build_gmail_email_source",
        lambda *a, **k: called.append("built"),
    )
    out = io.StringIO()

    assert run_gmail_thread(settings=Settings(_env_file=None), out=out) == EXIT_CONFIG  # type: ignore[call-arg]
    assert called == []


# --- request identity -----------------------------------------------------------


def test_a_new_enquiry_id_is_stable_and_derived_from_the_message_id() -> None:
    assert _request_id_for(ENQUIRY) == _request_id_for(ENQUIRY)
    assert _request_id_for(ENQUIRY).startswith("R-GMAIL-")
    assert _request_id_for(REPLY) != _request_id_for(ENQUIRY)
