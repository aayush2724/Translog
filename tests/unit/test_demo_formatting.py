"""The demo's presentation and control flow, offline.

No network and no API key. The formatting functions are pure, and the runner is
driven with a stub extractor — so everything except the model call is the real
code path.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest

from translog_quote.config import Settings
from translog_quote.domain.email import RawEmail
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import (
    CargoDimensions,
    DeliveryType,
    RequestSource,
    ShipmentRecord,
)
from translog_quote.domain.validation import validate_shipment
from translog_quote.errors import ContractViolation, TransientFailure
from translog_quote.interface.demo import run_demo
from translog_quote.interface.demo.extraction_demo import (
    EXIT_CONFIG,
    EXIT_EXTRACTION,
    EXIT_FIXTURE,
    EXIT_INVALID_SHIPMENT,
    EXIT_OK,
)
from translog_quote.interface.demo.formatting import (
    format_email,
    format_extraction,
    format_validation,
    render_evidence,
    render_value,
)

EMAIL = RawEmail(
    message_id="<demo@example.example>",
    from_address="Kavita Rao <kavita.rao@meridianexports.example>",
    subject="Export rate required - Ahmedabad to Bahrain",
    body_text="Origin: Ahmedabad\nDestination: Bahrain\nGross weight: 480 kgs\n",
    received_at=datetime(2026, 9, 1, 11, 20, tzinfo=UTC),
)

COMPLETE = ExtractionResult(
    origin=ExtractedValue[str].stated("Ahmedabad", evidence="Origin: Ahmedabad"),
    destination=ExtractedValue[str].stated("Bahrain", evidence="Destination: Bahrain"),
    weight_kg=ExtractedValue[float].stated(480.0, evidence="Gross weight: 480 kgs"),
    dimensions_in=ExtractedValue[CargoDimensions].stated(
        CargoDimensions(length=22, width=30, height=5)
    ),
    commodity=ExtractedValue[str].stated("Industrial Adhesive Compound"),
    cargo_type=ExtractedValue[str].stated("Non-Haz"),
    is_chemical=ExtractedValue[bool].stated(value=True),
    msds_attached=ExtractedValue[bool].stated(value=True),
    pcs=ExtractedValue[int].stated(15),
    delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.DOOR),
    delivery_address=ExtractedValue[str].stated("Hidd Industrial Area, Bahrain"),
)


class StubExtractor:
    def __init__(self, result: ExtractionResult | Exception) -> None:
        self._result = result

    def extract_shipment(self, text: str) -> ExtractionResult:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def read_client_intent(self, text: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TRANSLOG_OPENROUTER__API_KEY", "sk-or-v1-NOTREAL")
    return Settings()


def run(settings: Settings, **kw) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    out = io.StringIO()
    code = run_demo(settings=settings, out=out, **kw)
    return code, out.getvalue()


# --- value rendering -----------------------------------------------------------


def test_units_are_shown_with_their_values() -> None:
    assert render_value("weight_kg", ExtractedValue[float].stated(480.0)) == "480 kg"
    assert render_value("pcs", ExtractedValue[int].stated(15)) == "15 pieces"


def test_dimensions_are_labelled_not_positional() -> None:
    """A bare "22 x 30 x 5" would reproduce exactly the ambiguity the contract
    exists to remove."""
    dims = ExtractedValue[CargoDimensions].stated(CargoDimensions(length=22, width=30, height=5))

    assert render_value("dimensions_in", dims) == "22 (L) x 30 (W) x 5 (H) inches"


def test_booleans_read_as_yes_and_no() -> None:
    assert render_value("is_chemical", ExtractedValue[bool].stated(value=True)) == "Yes"
    assert render_value("msds_attached", ExtractedValue[bool].stated(value=False)) == "No"


def test_the_three_absence_reasons_render_differently() -> None:
    """Showing the difference between "silent" and "the client said no" is much
    of what this demo is for."""
    silent = render_value("commodity", ExtractedValue[str].not_stated())
    denied = render_value("delivery_address", ExtractedValue[str].denied())
    unclear = render_value("weight_kg", ExtractedValue[float].ambiguous(note="stated in lbs"))

    assert len({silent, denied, unclear}) == 3
    assert "not stated" in silent
    assert "explicitly none" in denied
    assert "stated in lbs" in unclear


# --- evidence rendering ---------------------------------------------------------


def test_multi_line_evidence_collapses_to_one_line() -> None:
    """A quoted address arrives with newlines; left alone it breaks the column
    alignment of everything under it."""
    quote = render_evidence("Delivery address:\nWarehouse 7, Road 2114,\nBahrain")

    assert "\n" not in quote
    assert "Warehouse 7" in quote


def test_long_evidence_is_truncated_with_an_ellipsis() -> None:
    quote = render_evidence("x" * 500)

    assert len(quote) <= 76
    assert quote.endswith("…")


# --- section formatting ----------------------------------------------------------


def test_the_email_section_shows_subject_client_and_body() -> None:
    rendered = format_email(EMAIL)

    assert "Export rate required" in rendered
    assert "kavita.rao@meridianexports.example" in rendered
    assert "Gross weight: 480 kgs" in rendered


def test_a_long_body_is_truncated_with_a_count() -> None:
    long_email = EMAIL.model_copy(update={"body_text": "\n".join(f"line {i}" for i in range(60))})

    rendered = format_email(long_email)

    assert "more line(s)" in rendered
    assert "line 59" not in rendered


def test_every_canonical_field_appears_in_the_extraction_section() -> None:
    rendered = format_extraction(COMPLETE)

    for label in (
        "Origin",
        "Destination",
        "Weight",
        "Dimensions",
        "Commodity",
        "Cargo Type",
        "Chemical",
        "MSDS",
        "PCS",
        "Delivery Type",
        "Delivery Address",
    ):
        assert f"{label}:" in rendered
    assert "11 of 11 fields stated" in rendered


def test_evidence_is_shown_so_values_can_be_checked_against_the_email() -> None:
    rendered = format_extraction(COMPLETE)

    assert 'evidence: "Origin: Ahmedabad"' in rendered


def test_evidence_can_be_suppressed() -> None:
    assert "evidence:" not in format_extraction(COMPLETE, show_evidence=False)


def test_an_invalid_shipment_lists_its_rule_ids() -> None:
    record = ShipmentRecord(request_id="R", source=RequestSource.EMAIL)

    rendered = format_validation(validate_shipment(record))

    assert "Status: INVALID" in rendered
    assert "ORIGIN_REQUIRED" in rendered
    assert "issue(s) found" in rendered


# --- runner control flow ---------------------------------------------------------


def test_a_missing_api_key_fails_clearly_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRANSLOG_OPENROUTER__API_KEY", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    code, output = run(settings)

    assert code == EXIT_CONFIG
    assert "CONFIGURATION ERROR" in output
    assert "TRANSLOG_OPENROUTER__API_KEY" in output


def test_a_missing_fixture_fails_clearly(configured: Settings) -> None:
    code, output = run(configured, scenario="does_not_exist")

    assert code == EXIT_FIXTURE
    assert "FIXTURE ERROR" in output


def test_an_invalid_extraction_fails_clearly(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo must surface a contract violation, never paper over it."""
    monkeypatch.setattr(
        "translog_quote.bootstrap.build_extractor",
        lambda settings: StubExtractor(ContractViolation("model returned nonsense")),
    )

    code, output = run(configured)

    assert code == EXIT_EXTRACTION
    assert "EXTRACTION FAILED" in output
    assert "model returned nonsense" in output


def test_a_transport_failure_fails_clearly(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "translog_quote.bootstrap.build_extractor",
        lambda settings: StubExtractor(TransientFailure("OpenRouter returned 503")),
    )

    code, output = run(configured)

    assert code == EXIT_EXTRACTION
    assert "503" in output


def test_a_complete_extraction_reports_valid(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "translog_quote.bootstrap.build_extractor", lambda settings: StubExtractor(COMPLETE)
    )

    code, output = run(configured)

    assert code == EXIT_OK
    assert "Status: VALID" in output
    assert "Ahmedabad" in output


def test_an_incomplete_extraction_reports_invalid_and_exits_nonzero(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = ExtractionResult(origin=ExtractedValue[str].stated("Mundra"))
    monkeypatch.setattr(
        "translog_quote.bootstrap.build_extractor", lambda settings: StubExtractor(partial)
    )

    code, output = run(configured)

    assert code == EXIT_INVALID_SHIPMENT
    assert "Status: INVALID" in output
    assert "DESTINATION_REQUIRED" in output


def test_the_api_key_never_reaches_the_output(
    configured: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "translog_quote.bootstrap.build_extractor", lambda settings: StubExtractor(COMPLETE)
    )

    _, output = run(configured)

    assert "sk-or-v1-NOTREAL" not in output
    assert "Authorization" not in output
    assert "Bearer" not in output


def test_the_real_fixture_loads_through_the_composition_root(configured: Settings) -> None:
    """The demo reads the actual Phase 3 fixture from disk, not a copy."""
    from translog_quote import bootstrap

    emails = bootstrap.build_fixture_email_source(configured, "a_complete_request").fetch_new()

    assert len(emails) == 1
    assert "Ahmedabad" in emails[0].body_text
