"""Measurements in any unit are converted in code; a missing unit is asked for.

Production cases this pins (16 Oct 2026):
- "100 x 80 x 60 cm" went to manual review ("cannot be converted to inches per
  rules") instead of being converted.
- "40x30x24" and "15x20x20" — bare numbers sent in reply to a clarification —
  went straight to manual review instead of a clarification asking the unit.
- "pharmaceutical samples" then "Pharmaceutical samples" raised a conflict.

The model reports numbers as written plus the unit it saw; the adapter converts
with `domain.shipment.units` (exact, rounded UP to 0.1) and turns a missing or
unsupported unit into an INVALID field that drives a clarification.
"""

from __future__ import annotations

import json

import pytest
from tests.unit.test_clarification_loop import REQ, approve, complete, email, workflow
from tests.unit.test_openrouter_adapter import adapter_for, envelope

from translog_quote.domain.extraction import ExtractedValue, ExtractionResult, FieldStatus
from translog_quote.domain.shipment import CargoDimensions, FieldName, ShipmentRecord
from translog_quote.domain.shipment.merge import merge_shipment
from translog_quote.domain.shipment.units import (
    UNIT_MISSING_MARKER,
    is_unit_note,
    length_unit,
    to_inches,
    to_kg,
    unit_note,
    weight_unit,
)
from translog_quote.domain.workflow import RequestState

# --- the arithmetic -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "inches"),
    [
        (100, "cm", 39.4),  # 39.370… rounded UP
        (80, "cm", 31.5),  # 31.496…
        (60, "cm", 23.7),  # 23.622… — up, never down to 23.6
        (254, "mm", 10.0),  # exact: no spurious extra tenth
        (1, "m", 39.4),
        (2, "ft", 24.0),
        (34, "in", 34.0),  # canonical unit: untouched
        (33.33, "in", 33.33),  # untouched means not rounded either
    ],
)
def test_lengths_convert_to_inches_rounding_up(value: float, unit: str, inches: float) -> None:
    assert to_inches(value, unit) == inches


@pytest.mark.parametrize(
    ("value", "unit", "kg"),
    [(220, "lb", 99.8), (1.5, "t", 1500.0), (500, "g", 0.5), (80, "kg", 80.0)],
)
def test_weights_convert_to_kg_rounding_up(value: float, unit: str, kg: float) -> None:
    assert to_kg(value, unit) == kg


@pytest.mark.parametrize(
    ("raw", "canon"),
    [
        ("cm", "cm"),
        ("CMS", "cm"),
        ("c.m.", "cm"),
        ("Centimetres", "cm"),
        ("inches", "in"),
        ('"', "in"),
        ("mm", "mm"),
        ("Mtr", "m"),
        ("feet", "ft"),
    ],
)
def test_length_spellings_are_recognised(raw: str, canon: str) -> None:
    assert length_unit(raw) == canon


@pytest.mark.parametrize("raw", [None, "", "yards", "furlongs", "units"])
def test_unknown_or_absent_length_units_are_never_guessed(raw: str | None) -> None:
    assert length_unit(raw) is None


@pytest.mark.parametrize(("raw", "canon"), [("KGS", "kg"), ("lbs", "lb"), ("tonnes", "t")])
def test_weight_spellings_are_recognised(raw: str, canon: str) -> None:
    assert weight_unit(raw) == canon


def test_bare_ton_is_not_guessed() -> None:
    """Short ton, long ton or tonne: asked, never assumed."""
    assert weight_unit("tons") is None


# --- the adapter step -----------------------------------------------------------


def _extract(payload: dict[str, object]) -> ExtractionResult:
    adapter, _ = adapter_for(envelope(json.dumps(payload)))
    return adapter.extract_shipment("body")


def test_centimetre_dimensions_are_converted_to_inches() -> None:
    result = _extract(
        {
            "dimensions_in": {
                "status": "stated",
                "value": {"length": 100, "width": 80, "height": 60},
                "unit": "cm",
                "evidence": "100 x 80 x 60 cm",
            }
        }
    )
    assert result.dimensions_in.is_stated
    assert result.dimensions_in.value == CargoDimensions(length=39.4, width=31.5, height=23.7)


def test_the_unit_may_also_sit_inside_the_dimensions_value() -> None:
    result = _extract(
        {
            "dimensions_in": {
                "status": "stated",
                "value": {"length": 1, "width": 1, "height": 2, "unit": "ft"},
            }
        }
    )
    assert result.dimensions_in.value == CargoDimensions(length=12, width=12, height=24)


def test_pounds_are_converted_to_kilograms() -> None:
    result = _extract({"weight_kg": {"status": "stated", "value": 220, "unit": "lbs"}})
    assert result.weight_kg.is_stated
    assert result.weight_kg.value == 99.8


def test_inches_and_kilograms_pass_through_untouched() -> None:
    result = _extract(
        {
            "weight_kg": {"status": "stated", "value": 80, "unit": "kg"},
            "dimensions_in": {
                "status": "stated",
                "value": {"length": 20, "width": 20, "height": 20},
                "unit": "inches",
            },
        }
    )
    assert result.weight_kg.value == 80.0
    assert result.dimensions_in.value == CargoDimensions(length=20, width=20, height=20)


def test_dimensions_without_a_unit_become_a_correctable_invalid_field() -> None:
    """The production "40x30x24": never guessed, and the client's own numbers
    are kept in the note so the clarification can quote them back."""
    result = _extract(
        {
            "dimensions_in": {
                "status": "stated",
                "value": {"length": 40, "width": 30, "height": 24},
                "unit": None,
            }
        }
    )
    assert result.dimensions_in.status is FieldStatus.INVALID
    assert result.dimensions_in.value is None
    assert result.dimensions_in.note == "Dimensions given without a unit: 40 x 30 x 24"


def test_an_unconvertible_unit_becomes_a_correctable_invalid_field() -> None:
    result = _extract(
        {
            "dimensions_in": {
                "status": "stated",
                "value": {"length": 4, "width": 3, "height": 2},
                "unit": "yards",
            },
            "weight_kg": {"status": "stated", "value": 2, "unit": "tons"},
        }
    )
    assert result.dimensions_in.status is FieldStatus.INVALID
    assert is_unit_note(result.dimensions_in.note)
    assert result.weight_kg.status is FieldStatus.INVALID
    assert result.weight_kg.note == "Weight given in a unit that cannot be converted: 2 tons"


def test_a_unit_key_on_a_field_that_is_not_stated_is_ignored() -> None:
    """Live finding: the model writes `"unit": null` on a weight the email never
    mentions. That must not reject the whole extraction."""
    result = _extract(
        {
            "weight_kg": {"status": "not_stated", "unit": None},
            "dimensions_in": {
                "status": "stated",
                "value": {"length": 40, "width": 30, "height": 24},
                "unit": None,
            },
        }
    )
    assert result.weight_kg.status is FieldStatus.NOT_STATED
    assert result.dimensions_in.status is FieldStatus.INVALID


def test_output_without_a_unit_key_keeps_the_original_contract() -> None:
    """Older-format output (no `unit` key) is read as inches / kg, as before."""
    result = _extract(
        {"dimensions_in": {"status": "stated", "value": {"length": 34, "width": 24, "height": 6}}}
    )
    assert result.dimensions_in.value == CargoDimensions(length=34, width=24, height=6)


def test_a_converted_value_that_is_impossible_is_still_invalid() -> None:
    result = _extract(
        {
            "dimensions_in": {
                "status": "stated",
                "value": {"length": 0, "width": 30, "height": 30},
                "unit": "cm",
            }
        }
    )
    assert result.dimensions_in.status is FieldStatus.INVALID
    assert not is_unit_note(result.dimensions_in.note)


# --- the clarification flow -----------------------------------------------------

_UNITLESS = ExtractedValue[CargoDimensions].invalid(
    note=unit_note("Dimensions", UNIT_MISSING_MARKER, "40 x 30 x 24")
)


def test_unitless_dimensions_ask_for_the_unit_quoting_the_numbers() -> None:
    wf, _sink = workflow(complete(dimensions_in=_UNITLESS))

    first = wf.handle(REQ, email("Dimensions: 40x30x24", n=1))

    assert first.clarification is not None
    assert first.clarification.asked_for == (FieldName.DIMENSIONS_IN,)
    body = first.clarification.body_text
    assert "The unit of the package dimensions you gave (40 x 30 x 24)" in body
    assert '"40 x 30 x 24 cm"' in body


def test_unitless_dimensions_in_a_reply_clarify_again_instead_of_manual_review() -> None:
    """The production path: dimensions asked for, client replies with bare
    numbers. Previously AMBIGUOUS → MANUAL_REVIEW; now a second clarification,
    and the client's unit reply completes the request."""
    no_dims = complete(dimensions_in=ExtractedValue[CargoDimensions].not_stated())
    unitless_reply = ExtractionResult(dimensions_in=_UNITLESS)
    unit_reply = ExtractionResult(
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=15.8, width=11.9, height=9.5)
        )
    )
    wf, _sink = workflow(no_dims, unitless_reply, unit_reply)

    wf.handle(REQ, email("rate please", n=1))
    approve(wf)
    second = wf.handle(REQ, email("Dimensions: 40x30x24", n=2))

    assert second.state is not RequestState.MANUAL_REVIEW
    assert second.clarification is not None
    assert "The unit of the package dimensions you gave" in second.clarification.body_text

    approve(wf)
    third = wf.handle(REQ, email("40 x 30 x 24 cm", n=3))
    assert third.is_complete
    assert third.state is RequestState.VALIDATED


# --- the merge ------------------------------------------------------------------


def _record(**fields: object) -> ShipmentRecord:
    return ShipmentRecord(request_id=REQ, source="email", **fields)  # type: ignore[arg-type]


def test_text_differing_only_in_case_or_spacing_is_not_a_conflict() -> None:
    from translog_quote.domain.extraction import to_extracted_fields

    existing = _record(commodity="pharmaceutical samples")
    incoming = to_extracted_fields(
        ExtractionResult(commodity=ExtractedValue[str].stated("Pharmaceutical  Samples"))
    )
    merge = merge_shipment(existing, incoming)
    assert merge.conflicts == ()
    assert merge.record.commodity == "pharmaceutical samples"


def test_genuinely_different_values_still_conflict() -> None:
    from translog_quote.domain.extraction import to_extracted_fields

    existing = _record(weight_kg=500.0)
    incoming = to_extracted_fields(ExtractionResult(weight_kg=ExtractedValue[float].stated(700.0)))
    assert len(merge_shipment(existing, incoming).conflicts) == 1


# --- edge cases found in the offline sweep (16 Oct 2026) ------------------------


def test_a_european_thousands_separator_is_asked_about_never_guessed() -> None:
    """ "1.200 kg" read as 1.2 kg is a thousand-fold under-declaration that
    prices wrong silently. The weight becomes a correctable field instead."""
    result = _extract(
        {"weight_kg": {"status": "stated", "value": 1.2, "unit": "kg", "evidence": "1.200 kg"}}
    )
    assert result.weight_kg.status is FieldStatus.INVALID
    assert result.weight_kg.value is None


@pytest.mark.parametrize(
    ("evidence", "value"), [("1.5 kg", 1.5), ("12.25 kg", 12.25), ("1,200 kg", 1200)]
)
def test_ordinary_decimals_and_comma_thousands_are_untouched(evidence: str, value: float) -> None:
    result = _extract(
        {"weight_kg": {"status": "stated", "value": value, "unit": "kg", "evidence": evidence}}
    )
    assert result.weight_kg.is_stated
    assert result.weight_kg.value == value


def test_the_separator_question_offers_both_readings() -> None:
    result = _extract(
        {"weight_kg": {"status": "stated", "value": 2.5, "unit": "kgs", "evidence": "2.500 kgs"}}
    )
    wf, _sink = workflow(complete(weight_kg=result.weight_kg))
    out = wf.handle(REQ, email("2.500 kgs", n=1))
    assert out.clarification is not None
    assert 'you wrote "2.500 kg"' in out.clarification.body_text
    assert "2,500 kg or 2.5 kg" in out.clarification.body_text


def test_a_date_that_reads_either_way_round_is_confirmed() -> None:
    result = _extract(
        {"ship_date": {"status": "stated", "value": "2026-11-05", "evidence": "05/11/2026"}}
    )
    assert result.ship_date.status is FieldStatus.AMBIGUOUS
    wf, _sink = workflow(complete(ship_date=result.ship_date))
    out = wf.handle(REQ, email("Ship date: 05/11/2026", n=1))
    assert out.clarification is not None
    assert "5 November 2026 or 11 May 2026" in out.clarification.body_text


@pytest.mark.parametrize(
    ("evidence", "iso"),
    [
        ("27/10/2026", "2026-10-27"),
        ("10/27/2026", "2026-10-27"),
        ("11/11/2026", "2026-11-11"),
        ("27 October 2026", "2026-10-27"),
    ],
)
def test_dates_with_only_one_reading_are_untouched(evidence: str, iso: str) -> None:
    result = _extract({"ship_date": {"status": "stated", "value": iso, "evidence": evidence}})
    assert result.ship_date.is_stated


def test_missing_dimension_and_weight_questions_ask_for_the_unit() -> None:
    from translog_quote.domain.clarification.questions import missing_question

    assert "and their unit" in missing_question(FieldName.DIMENSIONS_IN)
    assert "in inches" not in missing_question(FieldName.DIMENSIONS_IN)
    assert "its unit" in missing_question(FieldName.WEIGHT_KG)
