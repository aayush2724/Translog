"""Deterministic normalization — whitespace and blank-string handling only."""

from __future__ import annotations

from translog_quote.domain.shipment import ExtractedFields, normalize_extracted_fields


def test_collapses_internal_whitespace() -> None:
    fields = ExtractedFields(origin="  Ahmedabad   City ", commodity="Polyisobutylene   Additive")
    normalized = normalize_extracted_fields(fields)

    assert normalized.origin == "Ahmedabad City"
    assert normalized.commodity == "Polyisobutylene Additive"


def test_blank_string_becomes_none() -> None:
    fields = ExtractedFields(destination="   ")
    normalized = normalize_extracted_fields(fields)

    assert normalized.destination is None


def test_does_not_touch_non_string_fields() -> None:
    fields = ExtractedFields(weight_kg=500.0, pcs=20, is_chemical=True)
    normalized = normalize_extracted_fields(fields)

    assert normalized.weight_kg == 500.0
    assert normalized.pcs == 20
    assert normalized.is_chemical is True


def test_never_invents_a_value_for_a_field_that_was_never_stated() -> None:
    """BR-7: normalization reformats what is there — it never fills a gap."""
    fields = ExtractedFields(origin="Ahmedabad")
    normalized = normalize_extracted_fields(fields)

    assert normalized.destination is None
    assert normalized.commodity is None
    assert normalized.weight_kg is None


def test_leaves_an_already_clean_record_unchanged() -> None:
    fields = ExtractedFields(origin="Ahmedabad", destination="Bahrain")
    assert normalize_extracted_fields(fields) == fields
