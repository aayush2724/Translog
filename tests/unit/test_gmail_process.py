"""The `gmail-process` command: real inbound mail into the clarification loop.

The Gmail source and the model are stubbed; **the workflow is the real one**.
Merge, validation, the unresolved analysis, the clarification wording and the
approval gate all run as written, so these tests exercise the integration
rather than a re-description of it.

The invariant most of this file exists to defend: nothing is sent, and the
draft stays pending until a person approves it.
"""

from __future__ import annotations

import ast
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest

from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType, FieldName
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import IllegalTransition, PermanentFailure
from translog_quote.interface.demo import gmail_process
from translog_quote.interface.demo.gmail_process import (
    EXIT_CONFIG,
    EXIT_EXTRACTION,
    EXIT_GMAIL,
    EXIT_NO_MESSAGE,
    EXIT_OK,
    _request_id_for,
    run_gmail_process,
)

FAKE_KEY = "test-not-a-real-credential"

#: The Phase 10.4 test enquiry: origin, destination, weight, dimensions and
#: cargo type stated; commodity, chemical status, pieces and delivery type not.
INCOMPLETE_EMAIL = RawEmail(
    message_id="<enquiry-104@mail.example.com>",
    from_address="client@example.com",
    subject="Rate required - Ahmedabad to Bahrain",
    body_text=(
        "Dear Ma'am,\n\n"
        "Please provide a rate for 500 KG from Ahmedabad to Bahrain.\n"
        "Dimensions 24 x 34 x 6 inches. Cargo type Non-Haz.\n\n"
        "Regards,\nMehul\n"
    ),
    received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
)

#: What the model reports for that email. Five fields stated, four silent —
#: which is what the deterministic validator then has to notice.
INCOMPLETE_EXTRACTION = ExtractionResult(
    origin=ExtractedValue[str].stated("Ahmedabad"),
    destination=ExtractedValue[str].stated("Bahrain"),
    weight_kg=ExtractedValue[float].stated(500.0),
    dimensions_in=ExtractedValue[CargoDimensions].stated(
        CargoDimensions(length=34, width=24, height=6)
    ),
    cargo_type=ExtractedValue[str].stated("Non-Haz"),
)

COMPLETE_EXTRACTION = ExtractionResult(
    origin=ExtractedValue[str].stated("Ahmedabad"),
    destination=ExtractedValue[str].stated("Bahrain"),
    weight_kg=ExtractedValue[float].stated(500.0),
    dimensions_in=ExtractedValue[CargoDimensions].stated(
        CargoDimensions(length=34, width=24, height=6)
    ),
    cargo_type=ExtractedValue[str].stated("Non-Haz"),
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


class StubExtractor:
    def __init__(self, result: ExtractionResult | Exception) -> None:
        self._result = result
        self.calls: list[str] = []

    def extract_shipment(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def read_client_intent(self, text: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class SpySink:
    """Records any send attempt. Nothing in this phase may reach it."""

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
    *,
    source: object,
    extraction: ExtractionResult | Exception,
) -> tuple[SpySink, list[object], StubExtractor]:
    """Point the command at a stub mailbox and a stub model, keeping the real
    workflow. Returns the sink, the workflows built, and the extractor."""
    spy = SpySink()
    extractor = StubExtractor(extraction)
    built: list[object] = []
    real_builder = gmail_process.bootstrap.build_clarification_workflow

    def build(settings_arg: Settings, **kwargs: object) -> object:
        kwargs.pop("sink", None)
        kwargs.pop("extractor", None)
        workflow = real_builder(
            settings_arg,
            sink=spy,
            extractor=extractor,
            **kwargs,  # type: ignore[arg-type]
        )
        built.append(workflow)
        return workflow

    monkeypatch.setattr(gmail_process.bootstrap, "build_gmail_email_source", lambda _s: source)
    monkeypatch.setattr(gmail_process.bootstrap, "build_clarification_workflow", build)
    return spy, built, extractor


# --- the integration path -------------------------------------------------------


def test_an_incomplete_real_enquiry_produces_a_clarification_draft(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    sink, _, _ = wire(
        monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION
    )
    out = io.StringIO()

    code = run_gmail_process(settings=settings, out=out)

    assert code == EXIT_OK
    text = out.getvalue()
    assert "CLARIFICATION DRAFT" in text
    assert "HUMAN APPROVAL REQUIRED" in text
    # Nothing left the system.
    assert sink.sent == []


def test_the_draft_asks_for_exactly_the_four_missing_fields(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The four the phase's test case names: commodity, chemical status,
    pieces, delivery type. Decided by the existing deterministic validator."""
    _, built, _ = wire(
        monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION
    )

    run_gmail_process(settings=settings, out=io.StringIO())

    draft = built[0].pending_draft(_request_id_for(INCOMPLETE_EMAIL))  # type: ignore[attr-defined]
    assert draft is not None
    assert set(draft.asked_for) == {
        FieldName.COMMODITY,
        FieldName.IS_CHEMICAL,
        FieldName.PCS,
        FieldName.DELIVERY_TYPE,
    }


def test_the_draft_is_still_pending_after_the_command_returns(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _, built, _ = wire(
        monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION
    )

    run_gmail_process(settings=settings, out=io.StringIO())

    assert built[0].pending_draft(_request_id_for(INCOMPLETE_EMAIL)) is not None  # type: ignore[attr-defined]


def test_the_request_stops_at_needs_info(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION)
    out = io.StringIO()

    run_gmail_process(settings=settings, out=out)

    text = out.getvalue()
    assert f"request state       : {RequestState.NEEDS_INFO.value}" in text
    assert "email sent          : no" in text
    assert "draft pending       : yes" in text


def test_only_an_explicit_named_approval_releases_the_draft(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The gate itself: the command sends nothing, and a later human approval
    is what hands the message to the sink."""
    sink, built, _ = wire(
        monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION
    )
    run_gmail_process(settings=settings, out=io.StringIO())
    assert sink.sent == []

    built[0].approve_clarification(  # type: ignore[attr-defined]
        _request_id_for(INCOMPLETE_EMAIL), by="ops.manager@translog.example"
    )

    assert len(sink.sent) == 1


def test_a_second_approval_is_refused(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    _, built, _ = wire(
        monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION
    )
    run_gmail_process(settings=settings, out=io.StringIO())
    request_id = _request_id_for(INCOMPLETE_EMAIL)
    built[0].approve_clarification(request_id, by="ops.manager@translog.example")  # type: ignore[attr-defined]

    with pytest.raises(IllegalTransition):
        built[0].approve_clarification(request_id, by="ops.manager@translog.example")  # type: ignore[attr-defined]


def test_a_complete_enquiry_needs_no_clarification_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    sink, _, _ = wire(
        monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=COMPLETE_EXTRACTION
    )
    out = io.StringIO()

    code = run_gmail_process(settings=settings, out=out)

    assert code == EXIT_OK
    text = out.getvalue()
    assert "No clarification was needed" in text
    assert "CLARIFICATION DRAFT" not in text
    assert sink.sent == []


def test_the_model_sees_the_real_email_body_unmodified(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    _, _, extractor = wire(
        monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION
    )

    run_gmail_process(settings=settings, out=io.StringIO())

    assert extractor.calls == [INCOMPLETE_EMAIL.body_text]


# --- the audit trail as evidence ------------------------------------------------


def test_the_audit_trail_records_the_draft_as_not_sent(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION)
    out = io.StringIO()

    run_gmail_process(settings=settings, out=out)

    text = out.getvalue()
    assert "clarification_drafted" in text
    assert "sent=False" in text
    assert "awaiting=human approval" in text
    # The event that would mean an email went out never appears.
    assert "clarification_sent" not in text


def test_the_audit_trail_carries_no_email_body_or_address(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION)
    out = io.StringIO()

    run_gmail_process(settings=settings, out=out)

    audit_section = out.getvalue().split("AUDIT TRAIL")[1].split("HUMAN APPROVAL")[0]
    assert INCOMPLETE_EMAIL.from_address not in audit_section
    assert "Mehul" not in audit_section


# --- request identity -----------------------------------------------------------


def test_the_request_id_is_derived_from_the_message_id_and_is_stable() -> None:
    first = _request_id_for(INCOMPLETE_EMAIL)

    assert first == _request_id_for(INCOMPLETE_EMAIL)
    assert first.startswith("R-GMAIL-")
    # The client's address is not part of the identity.
    assert "client@example.com" not in first


def test_different_messages_get_different_request_ids() -> None:
    other = INCOMPLETE_EMAIL.model_copy(update={"message_id": "<other@mail.example.com>"})

    assert _request_id_for(other) != _request_id_for(INCOMPLETE_EMAIL)


# --- failure paths --------------------------------------------------------------


def test_an_empty_mailbox_reports_what_to_do_next(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, source=StubSource(), extraction=INCOMPLETE_EXTRACTION)
    out = io.StringIO()

    assert run_gmail_process(settings=settings, out=out) == EXIT_NO_MESSAGE
    assert "NO MESSAGE FOUND" in out.getvalue()


def test_a_gmail_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    def refuse(_settings: Settings) -> object:
        raise PermanentFailure("No Gmail token file at .secrets/gmail_token.json.")

    monkeypatch.setattr(gmail_process.bootstrap, "build_gmail_email_source", refuse)
    out = io.StringIO()

    assert run_gmail_process(settings=settings, out=out) == EXIT_GMAIL
    assert "GMAIL RECEIVE FAILED" in out.getvalue()


def test_an_extraction_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(
        monkeypatch,
        source=StubSource(INCOMPLETE_EMAIL),
        extraction=PermanentFailure("model refused"),
    )
    out = io.StringIO()

    assert run_gmail_process(settings=settings, out=out) == EXIT_EXTRACTION
    assert "WORKFLOW FAILED" in out.getvalue()


def test_a_missing_api_key_stops_before_any_gmail_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSLOG_OPENROUTER__API_KEY", raising=False)
    called: list[str] = []
    monkeypatch.setattr(
        gmail_process.bootstrap,
        "build_gmail_email_source",
        lambda _s: called.append("built"),
    )
    out = io.StringIO()

    assert run_gmail_process(settings=Settings(_env_file=None), out=out) == EXIT_CONFIG  # type: ignore[call-arg]
    assert called == []


# --- what this phase must not touch ---------------------------------------------


def test_no_rate_provider_or_outbox_sink_is_ever_constructed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    forbidden: list[str] = []
    for name in ("build_rate_provider", "build_outbox_sink"):
        monkeypatch.setattr(
            gmail_process.bootstrap, name, lambda *a, _n=name, **k: forbidden.append(_n)
        )
    wire(monkeypatch, source=StubSource(INCOMPLETE_EMAIL), extraction=INCOMPLETE_EXTRACTION)

    run_gmail_process(settings=settings, out=io.StringIO())

    assert forbidden == []


def test_the_command_contains_no_call_that_could_release_a_draft() -> None:
    """Structural, not textual: an AST walk over the module finds no call to
    `approve_clarification` or `send`, so no code path — reachable or not —
    can put a message on a sink."""
    tree = ast.parse(Path(gmail_process.__file__).read_text(encoding="utf-8"))

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "approve_clarification" not in called
    assert "send" not in called
