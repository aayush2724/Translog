"""The end-to-end POC demo, driven offline.

A scripted extractor stands in for Qwen; everything else is the real code path —
real validation, real clarification wording, real merge, real mock adapter, real
filtering, real selection. So these tests exercise the demo's orchestration and
its refusal behaviour, not its whitespace.
"""

from __future__ import annotations

import io

import pytest

from translog_quote.config import Settings
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
from translog_quote.domain.shipment import CargoDimensions, DeliveryType
from translog_quote.interface.demo import poc_demo
from translog_quote.interface.demo.poc_demo import (
    EXIT_CONFIG,
    EXIT_EXTRACTION,
    EXIT_NO_RATE,
    EXIT_OK,
    EXIT_STILL_INCOMPLETE,
    run_demo,
)

FAKE_KEY = "test-not-a-real-credential"

#: What a well-behaved model returns for the initial enquiry: four fields short.
FIRST = ExtractionResult(
    origin=ExtractedValue[str].stated("Ahmedabad"),
    destination=ExtractedValue[str].stated("Bahrain"),
    weight_kg=ExtractedValue[float].stated(500.0),
    dimensions_in=ExtractedValue[CargoDimensions].stated(
        CargoDimensions(length=34, width=24, height=6)
    ),
    cargo_type=ExtractedValue[str].stated("Non-Haz"),
)

#: The reply: exactly the four missing fields, and nothing else.
REPLY = ExtractionResult(
    commodity=ExtractedValue[str].stated("Engineering components"),
    is_chemical=ExtractedValue[bool].stated(value=False),
    pcs=ExtractedValue[int].stated(10),
    delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
)


class ScriptedExtractor:
    def __init__(self, *results: ExtractionResult | Exception) -> None:
        self._results = list(results)
        self.calls: list[str] = []

    def extract_shipment(self, text: str) -> ExtractionResult:
        self.calls.append(text)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def read_client_intent(self, text: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TRANSLOG_OPENROUTER__API_KEY", FAKE_KEY)
    return Settings()


def run(
    settings: Settings, *results: ExtractionResult | Exception, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str]:
    """Run the demo with a scripted extractor in place of the live model."""
    extractor = ScriptedExtractor(*results)
    real_builder = poc_demo.bootstrap.build_clarification_workflow

    def build(s: Settings, **kw: object):  # type: ignore[no-untyped-def]
        return real_builder(s, extractor=extractor, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(poc_demo.bootstrap, "build_clarification_workflow", build)
    out = io.StringIO()
    return run_demo(settings=settings, out=out), out.getvalue()


# --- 1, 12. an incomplete enquiry clarifies and does not reach a quotation ------


def test_the_initial_enquiry_is_incomplete_and_triggers_clarification(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    assert code == EXIT_OK
    assert "Result: INCOMPLETE" in output
    assert "CLARIFICATION DRAFT GENERATED" in output
    assert "NOT SENT — REQUIRES HUMAN APPROVAL" in output
    # The four fields the enquiry left out, and only those.
    for asked in ("commodity", "chemical", "pieces", "door delivery"):
        assert asked in output.lower()
    # Never re-asks what the enquiry already gave.
    clarification = output.split("CLARIFICATION DRAFT GENERATED")[1].split("5. CLIENT REPLY")[0]
    assert "origin" not in clarification.lower()
    assert "destination" not in clarification.lower()


def test_the_demo_never_claims_the_clarification_was_sent(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stakeholder requirement, as an assertion on what a client sees."""
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    assert "STATUS:          DRAFT — NOT SENT" in output
    assert "HUMAN APPROVAL:  REQUIRED" in output
    assert "[ REVIEW / APPROVE DRAFT ]" in output
    assert "never" in output and "mails a client on its own" in output
    assert "No email was sent" in output
    assert "CLARIFICATION SENT" not in output
    assert "Emails sent       none" in output


def test_an_incomplete_shipment_never_reaches_a_quotation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reply supplies only one of the four gaps, so the demo must stop."""
    partial = ExtractionResult(commodity=ExtractedValue[str].stated("Engineering components"))

    code, output = run(settings, FIRST, partial, monkeypatch=monkeypatch)

    assert code == EXIT_STILL_INCOMPLETE
    assert "Still incomplete after clarification" in output
    assert "QUOTATION PREVIEW" not in output


# --- 2, 3, 4. reply processed, merged, revalidated -----------------------------


def test_the_reply_completes_the_shipment_through_the_real_merge(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    assert code == EXIT_OK
    assert "VALIDATION: PASS" in output
    assert "STATUS:     VALIDATED" in output


def test_information_from_the_enquiry_survives_the_merge(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reply never mentions origin, destination or weight."""
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    updated = output.split("6. UPDATED SHIPMENT")[1].split("7. RATE SEARCH")[0]
    assert "Ahmedabad" in updated
    assert "Bahrain" in updated
    assert "500 kg" in updated
    assert "Engineering components" in updated
    assert "carried over from the enquiry" in updated


def test_only_the_two_messages_are_extracted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two live calls, no more. The demo must not re-extract to check itself."""
    extractor = ScriptedExtractor(FIRST, REPLY)
    real_builder = poc_demo.bootstrap.build_clarification_workflow
    monkeypatch.setattr(
        poc_demo.bootstrap,
        "build_clarification_workflow",
        lambda s, **kw: real_builder(s, extractor=extractor, **kw),  # type: ignore[arg-type]
    )
    run_demo(settings=settings, out=io.StringIO())

    assert len(extractor.calls) == 2


# --- 5, 6, 7, 8. rate search, filtering, selection ------------------------------


def test_rate_search_runs_on_the_validated_shipment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    assert "Query: AMD -> BAH   500 kg" in output
    assert "Rates returned: 6" in output


def test_ineligible_rates_are_filtered_with_reasons(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    assert "Rejected: 2" in output
    assert "no price returned" in output
    assert "no transit time available" in output


def test_the_fastest_eligible_rate_is_selected_and_ranking_is_visible(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    selected = output.split("8. FASTEST ELIGIBLE RATE")[1].split("9. QUOTATION")[0]
    assert "Turkish Cargo" in selected
    assert "1 day" in selected and "1 days" not in selected
    # The ranking is shown, and it is not price order — 2 days at 20762 outranks
    # 4 days at 18340, which is the whole point of BR-1.
    assert "Ranked by transit time, not price" in selected
    assert selected.index("Emirates") < selected.index("Etihad")


def test_no_eligible_rate_stops_before_the_quotation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misleading quotation is worse than no quotation."""
    from translog_quote.adapters.webcargo import MockWebCargoAdapter
    from translog_quote.domain.rates import Rate

    unpriced = Rate(carrier_code="XX", carrier_name="No Price Air", product="GEN")
    monkeypatch.setattr(
        poc_demo.bootstrap,
        "build_demo_rate_provider",
        lambda: MockWebCargoAdapter(rates=(unpriced,)),
    )

    code, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    assert code == EXIT_NO_RATE
    assert "NO ELIGIBLE RATE" in output
    assert "QUOTATION PREVIEW" not in output


# --- 9, 10. the quotation preview -----------------------------------------------


def test_the_preview_uses_the_selected_rate_and_the_merged_record(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    preview = output.split("9. QUOTATION PREVIEW")[1]
    assert "Turkish Cargo (TK)" in preview
    assert "16900.00 INR" in preview
    assert "Engineering components" in preview
    assert "10 pieces" in preview


def test_the_preview_invents_nothing_and_says_what_is_missing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tax, surcharge, validity or payment term is specified anywhere in this
    project, so none may appear as a number."""
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    preview = output.split("9. QUOTATION PREVIEW")[1]
    for field in ("Taxes and surcharges", "Validity", "Payment terms", "Delivery address"):
        line = next(ln for ln in preview.splitlines() if ln.strip().startswith(field))
        assert "Not specified in POC" in line


def test_the_preview_is_a_preview_and_sends_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    assert "PREVIEW — not sent, not approved" in output
    assert "[ APPROVE QUOTATION ]" in output
    assert "not wired" in output
    assert "Quotation sent    none" in output
    assert "Emails sent       none" in output


def test_a_preview_cannot_be_mistaken_for_a_quotation_by_the_type_system() -> None:
    """`Quotation` requires an `Approved`, so the approval gate is enforced by a
    signature rather than by discipline. The preview is a `ReviewPacket`."""
    from translog_quote.domain.quotation import Quotation

    assert "approved" in Quotation.model_fields
    assert Quotation.model_fields["approved"].is_required()


# --- 11. mock data is labelled -------------------------------------------------


def test_simulated_rate_data_is_labelled_everywhere_it_appears(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invented rates must never read as a provider's."""
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    assert "SIMULATED WEBCARGO DATA — DEMO ONLY" in output
    assert "No WebCargo request was made" in output
    assert "POC QUOTATION PREVIEW — SIMULATED WEBCARGO DATA" in output
    assert "Rate data         SIMULATED" in output
    # The one phrasing that would be a lie.
    assert "LIVE PROVIDER DATA" not in output


# --- failure paths --------------------------------------------------------------


def test_a_missing_api_key_stops_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRANSLOG_OPENROUTER__API_KEY", raising=False)
    out = io.StringIO()

    code = run_demo(settings=Settings(_env_file=None), out=out)  # type: ignore[call-arg]

    assert code == EXIT_CONFIG
    assert "no OpenRouter API key" in out.getvalue()


def test_an_extraction_failure_stops_concisely(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from translog_quote.errors import TransientFailure

    code, output = run(
        settings, TransientFailure("OpenRouter returned 429"), monkeypatch=monkeypatch
    )

    assert code == EXIT_EXTRACTION
    assert "EXTRACTION FAILED" in output
    assert "429" in output
    assert "Traceback" not in output


def test_the_api_key_never_reaches_the_output(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, output = run(settings, FIRST, REPLY, monkeypatch=monkeypatch)

    for secret in (FAKE_KEY, "Authorization", "Bearer", "password"):
        assert secret not in output


def test_no_real_client_correspondence_appears_in_the_demo() -> None:
    """The 50-PDF corpus stays local. The demo's client is fictional."""
    from pathlib import Path

    source = Path(poc_demo.__file__).read_text(encoding="utf-8")

    assert ".example" in source, "demo addresses must use the reserved TLD"
    for real in ("translogexpress.com", "shreejiforwarder", "Mehul", "Darji"):
        assert real not in source
