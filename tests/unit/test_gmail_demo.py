"""The `gmail-test` command, driven offline.

`bootstrap.build_gmail_email_source` and `bootstrap.build_extractor` are
replaced with stubs, so this exercises the command's real orchestration,
banner, and failure reporting without a mailbox, a token, or a model call.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest

from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.errors import PermanentFailure
from translog_quote.interface.demo import gmail_demo
from translog_quote.interface.demo.gmail_demo import (
    EXIT_CONFIG,
    EXIT_EXTRACTION,
    EXIT_GMAIL,
    EXIT_NO_MESSAGE,
    EXIT_OK,
    run_gmail_test,
)

FAKE_KEY = "test-not-a-real-credential"

EMAIL = RawEmail(
    message_id="<enquiry-1@mail.example.com>",
    from_address="client@example.com",
    subject="Air freight enquiry AMD to BAH",
    body_text="500 kg, 34x24x6 inches, engineering components, 10 pcs, airport delivery.",
    received_at=datetime(2026, 8, 26, 10, 15, tzinfo=UTC),
)

COMPLETE = ExtractionResult(
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


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TRANSLOG_OPENROUTER__API_KEY", FAKE_KEY)
    monkeypatch.setenv("TRANSLOG_GMAIL__TEST_ADDRESS", "translog.test@example.com")
    return Settings()


def wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: object,
    extractor: object | None = None,
) -> None:
    monkeypatch.setattr(gmail_demo.bootstrap, "build_gmail_email_source", lambda _s: source)
    if extractor is not None:
        monkeypatch.setattr(gmail_demo.bootstrap, "build_extractor", lambda _s: extractor)


def test_one_received_email_reaches_extraction_and_validation(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    extractor = StubExtractor(COMPLETE)
    wire(monkeypatch, source=StubSource(EMAIL), extractor=extractor)
    out = io.StringIO()

    code = run_gmail_test(settings=settings, out=out)

    assert code == EXIT_OK
    # The extractor saw the real email body, unmodified by the Gmail layer.
    assert extractor.calls == [EMAIL.body_text]
    text = out.getvalue()
    assert "Ahmedabad" in text
    assert "Bahrain" in text


def test_the_banner_states_what_is_real_and_what_is_not(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, source=StubSource(EMAIL), extractor=StubExtractor(COMPLETE))
    out = io.StringIO()

    run_gmail_test(settings=settings, out=out)

    text = out.getvalue()
    assert "GMAIL CONNECTION: REAL" in text
    assert "MESSAGE RECEIVED: REAL" in text
    assert "AI EXTRACTION:    LIVE" in text
    assert "VALIDATION:       REAL" in text
    assert "EMAIL SENDING:    NONE" in text
    assert "WEBCARGO:         MOCK / NOT CONTACTED" in text


def test_an_empty_mailbox_reports_what_to_do_next(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(monkeypatch, source=StubSource(), extractor=StubExtractor(COMPLETE))
    out = io.StringIO()

    code = run_gmail_test(settings=settings, out=out)

    assert code == EXIT_NO_MESSAGE
    assert "NO MESSAGE FOUND" in out.getvalue()


def test_a_gmail_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    def refuse(_settings: Settings) -> object:
        raise PermanentFailure("No Gmail token file at .secrets/gmail_token.json.")

    monkeypatch.setattr(gmail_demo.bootstrap, "build_gmail_email_source", refuse)
    out = io.StringIO()

    code = run_gmail_test(settings=settings, out=out)

    assert code == EXIT_GMAIL
    assert "GMAIL RECEIVE FAILED" in out.getvalue()


def test_an_extraction_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    wire(
        monkeypatch,
        source=StubSource(EMAIL),
        extractor=StubExtractor(PermanentFailure("model refused")),
    )
    out = io.StringIO()

    code = run_gmail_test(settings=settings, out=out)

    assert code == EXIT_EXTRACTION
    assert "EXTRACTION FAILED" in out.getvalue()


def test_a_missing_api_key_stops_before_any_gmail_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRANSLOG_OPENROUTER__API_KEY", raising=False)
    called: list[str] = []
    monkeypatch.setattr(
        gmail_demo.bootstrap,
        "build_gmail_email_source",
        lambda _s: called.append("built"),
    )
    out = io.StringIO()

    code = run_gmail_test(settings=Settings(_env_file=None), out=out)  # type: ignore[call-arg]

    assert code == EXIT_CONFIG
    assert called == []


def test_the_command_constructs_no_sink_and_no_rate_provider(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """No outbound path and no WebCargo request can exist in this test."""
    forbidden: list[str] = []
    for name in ("build_outbox_sink", "build_rate_provider", "build_clarification_workflow"):
        monkeypatch.setattr(
            gmail_demo.bootstrap,
            name,
            lambda *a, _name=name, **k: forbidden.append(_name),
        )
    wire(monkeypatch, source=StubSource(EMAIL), extractor=StubExtractor(COMPLETE))

    run_gmail_test(settings=settings, out=io.StringIO())

    assert forbidden == []
