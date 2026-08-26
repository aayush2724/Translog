"""Narrowing an ExtractionResult into the canonical ExtractedFields.

Structural conversion only — the mapping applies no business rule and reaches
no conclusion. These tests pin that down, including the places where it would
be tempting to be clever.
"""

from __future__ import annotations

from translog_quote.domain.extraction import (
    ExtractedValue,
    ExtractionResult,
    to_extracted_fields,
)
from translog_quote.domain.shipment import (
    CargoDimensions,
    DeliveryType,
    ExtractedFields,
    RequestSource,
    build_initial_record,
)
from translog_quote.domain.validation import ValidationRuleId, validate_shipment


def test_stated_values_carry_across() -> None:
    result = ExtractionResult(
        origin=ExtractedValue[str].stated("Ahmedabad"),
        destination=ExtractedValue[str].stated("Bahrain"),
        weight_kg=ExtractedValue[float].stated(480.0),
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=30, width=22, height=5)
        ),
        commodity=ExtractedValue[str].stated("Industrial Adhesive Compound"),
        cargo_type=ExtractedValue[str].stated("Non-Haz"),
        is_chemical=ExtractedValue[bool].stated(value=True),
        msds_attached=ExtractedValue[bool].stated(value=True),
        pcs=ExtractedValue[int].stated(15),
        delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.DOOR),
        delivery_address=ExtractedValue[str].stated("Hidd Industrial Area, Bahrain"),
    )

    fields = to_extracted_fields(result)

    assert fields.origin == "Ahmedabad"
    assert fields.weight_kg == 480.0
    assert fields.dimensions_in == CargoDimensions(length=30, width=22, height=5)
    assert fields.is_chemical is True
    assert fields.msds_attached is True
    assert fields.pcs == 15
    assert fields.delivery_type is DeliveryType.DOOR


def test_an_empty_result_maps_to_an_all_null_extraction() -> None:
    fields = to_extracted_fields(ExtractionResult())

    assert fields == ExtractedFields()


def test_all_three_absence_reasons_collapse_to_none() -> None:
    """The documented lossy step. Silent, denied and ambiguous are different in
    the extraction result and identical in the canonical record, because `None`
    is the only thing the canonical record can say."""
    result = ExtractionResult(
        commodity=ExtractedValue[str].not_stated(),
        delivery_address=ExtractedValue[str].denied(evidence="no address needed"),
        weight_kg=ExtractedValue[float].ambiguous(note="stated in pounds"),
    )

    fields = to_extracted_fields(result)

    assert fields.commodity is None
    assert fields.delivery_address is None
    assert fields.weight_kg is None
    # ...and the reason survives only on the result, which is why it is kept.
    assert result.delivery_address.evidence == "no address needed"
    assert result.weight_kg.note == "stated in pounds"


def test_explicit_false_is_preserved_and_is_not_an_absence() -> None:
    """`msds_attached=False` is an answer. It must reach the canonical record as
    False, never as None — otherwise the client gets asked a question they have
    already answered."""
    result = ExtractionResult(
        is_chemical=ExtractedValue[bool].stated(value=False),
        msds_attached=ExtractedValue[bool].stated(value=False),
    )

    fields = to_extracted_fields(result)

    assert fields.is_chemical is False
    assert fields.msds_attached is False


def test_the_mapping_performs_no_validation() -> None:
    """A chemical shipment with no MSDS maps cleanly. Noticing the gap is the
    validator's job, and it happens afterwards."""
    result = ExtractionResult(
        is_chemical=ExtractedValue[bool].stated(value=True),
        msds_attached=ExtractedValue[bool].not_stated(),
    )

    fields = to_extracted_fields(result)

    assert fields.is_chemical is True
    assert fields.msds_attached is None  # mapped without complaint


def test_the_validator_catches_what_the_mapping_deliberately_ignored() -> None:
    """The full contract boundary in one test: extraction reports, mapping
    narrows, validation decides."""
    result = ExtractionResult(
        origin=ExtractedValue[str].stated("Ankleshwar"),
        destination=ExtractedValue[str].stated("Dammam"),
        weight_kg=ExtractedValue[float].stated(920.0),
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=36, width=24, height=20)
        ),
        commodity=ExtractedValue[str].stated("Specialty Resin Compound"),
        cargo_type=ExtractedValue[str].stated("Haz"),
        is_chemical=ExtractedValue[bool].stated(value=True),
        pcs=ExtractedValue[int].stated(12),
        delivery_type=ExtractedValue[DeliveryType].stated(DeliveryType.AIRPORT),
    )

    record = build_initial_record("R-X", RequestSource.EMAIL, to_extracted_fields(result))
    validation = validate_shipment(record)

    assert not validation.is_valid
    assert ValidationRuleId.MSDS_REQUIRED_FOR_CHEMICAL in {i.rule_id for i in validation.issues}


def test_mapping_is_deterministic() -> None:
    result = ExtractionResult(origin=ExtractedValue[str].stated("Mundra"))

    assert to_extracted_fields(result) == to_extracted_fields(result)


def test_mapping_covers_every_canonical_field() -> None:
    """A field added to ExtractedFields without a mapping rule would silently
    stay null forever. This fails the moment that happens."""
    mapped = set(ExtractedFields.model_fields)
    extraction = set(ExtractionResult.model_fields)

    assert mapped == extraction
