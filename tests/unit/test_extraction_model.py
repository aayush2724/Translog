"""The extraction contract's own invariants.

No network, no API key, no provider. Every test here constructs the contract
types directly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from translog_quote.domain.extraction import (
    ExtractedValue,
    ExtractionResult,
    FieldStatus,
)
from translog_quote.domain.shipment import CargoDimensions, DeliveryType

# --- status / value coherence ------------------------------------------------


def test_stated_requires_a_value() -> None:
    with pytest.raises(ValidationError, match="requires a value"):
        ExtractedValue[str](status=FieldStatus.STATED)


@pytest.mark.parametrize(
    "status", [FieldStatus.NOT_STATED, FieldStatus.DENIED, FieldStatus.AMBIGUOUS]
)
def test_non_stated_statuses_must_not_carry_a_value(status: FieldStatus) -> None:
    with pytest.raises(ValidationError, match="must not carry a value"):
        ExtractedValue[str](status=status, value="something", note="n")


def test_ambiguous_requires_a_note() -> None:
    """An ambiguity nobody explained is indistinguishable from a bug."""
    with pytest.raises(ValidationError, match="requires a note"):
        ExtractedValue[str](status=FieldStatus.AMBIGUOUS)


def test_constructors_produce_coherent_values() -> None:
    assert ExtractedValue[str].stated("Ahmedabad").value == "Ahmedabad"
    assert ExtractedValue[str].not_stated().value is None
    assert ExtractedValue[str].denied().value is None
    assert ExtractedValue[str].ambiguous(note="weight in lbs").note == "weight in lbs"


def test_is_stated_only_for_stated() -> None:
    assert ExtractedValue[int].stated(20).is_stated
    assert not ExtractedValue[int].not_stated().is_stated
    assert not ExtractedValue[int].denied().is_stated
    assert not ExtractedValue[int].ambiguous(note="x").is_stated


def test_extracted_value_is_frozen() -> None:
    value = ExtractedValue[str].stated("Bahrain")
    with pytest.raises(ValidationError):
        value.value = "Muscat"  # type: ignore[misc]


# --- the four states are genuinely distinct ---------------------------------


def test_the_four_absence_reasons_are_distinguishable() -> None:
    """The whole reason this type exists rather than a bare `T | None`."""
    silent = ExtractedValue[bool].not_stated()
    denied = ExtractedValue[bool].denied(evidence="we have no MSDS")
    unclear = ExtractedValue[bool].ambiguous(note="contradicts itself")
    answered_no = ExtractedValue[bool].stated(value=False, evidence="MSDS: no")

    statuses = {silent.status, denied.status, unclear.status, answered_no.status}
    assert len(statuses) == 4
    # Three carry no value, but for three different reasons.
    assert silent.value is None and denied.value is None and unclear.value is None
    # The fourth carries an explicit False, which is an answer, not an absence.
    assert answered_no.value is False


# --- default result ----------------------------------------------------------


def test_an_empty_result_is_every_field_not_stated() -> None:
    """The safe default: a model that says nothing has stated nothing."""
    result = ExtractionResult()

    assert len(result.fields_by_status(FieldStatus.NOT_STATED)) == 11
    assert result.fields_by_status(FieldStatus.STATED) == ()


def test_fields_by_status_reports_the_right_names() -> None:
    result = ExtractionResult(
        origin=ExtractedValue[str].stated("Vapi"),
        weight_kg=ExtractedValue[float].ambiguous(note="given in lbs"),
        delivery_address=ExtractedValue[str].denied(evidence="no address needed"),
    )

    assert result.fields_by_status(FieldStatus.STATED) == ("origin",)
    assert result.fields_by_status(FieldStatus.AMBIGUOUS) == ("weight_kg",)
    assert result.fields_by_status(FieldStatus.DENIED) == ("delivery_address",)


# --- impossible numbers are rejected at the boundary ------------------------


@pytest.mark.parametrize("weight", [0.0, -1.0, -500.0])
def test_a_non_positive_stated_weight_is_rejected(weight: float) -> None:
    """A client email does not say "-500 kg"; a model that produces one has
    malfunctioned, and that is an extraction failure rather than a shipment
    with a strange weight."""
    with pytest.raises(ValidationError, match="weight_kg must be positive"):
        ExtractionResult(weight_kg=ExtractedValue[float].stated(weight))


@pytest.mark.parametrize("pieces", [0, -3])
def test_a_non_positive_stated_piece_count_is_rejected(pieces: int) -> None:
    with pytest.raises(ValidationError, match="pcs must be positive"):
        ExtractionResult(pcs=ExtractedValue[int].stated(pieces))


def test_an_ambiguous_weight_is_not_subject_to_the_positivity_check() -> None:
    """Nothing to check — an ambiguous field carries no number at all."""
    result = ExtractionResult(weight_kg=ExtractedValue[float].ambiguous(note="stated as '500 lbs'"))

    assert result.weight_kg.status is FieldStatus.AMBIGUOUS


def test_impossible_dimensions_are_rejected_by_the_dimensions_type() -> None:
    """`CargoDimensions` already refuses non-positive sides, so the extraction
    result needs no separate rule for it."""
    with pytest.raises(ValidationError):
        CargoDimensions(length=-34, width=24, height=6)


# --- unknown fields and enums -------------------------------------------------


def test_an_unknown_field_is_rejected() -> None:
    """extra='forbid': a model inventing `hs_code` is a contract breach, not a
    field to quietly ignore."""
    with pytest.raises(ValidationError):
        ExtractionResult(hs_code=ExtractedValue[str].stated("3902.10"))  # type: ignore[call-arg]


def test_an_invalid_delivery_type_enum_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedValue[DeliveryType](status=FieldStatus.STATED, value="warehouse")


def test_a_valid_delivery_type_enum_is_accepted() -> None:
    value = ExtractedValue[DeliveryType].stated(DeliveryType.DOOR)

    assert value.value is DeliveryType.DOOR


def test_an_invalid_field_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractedValue[str](status="probably", value="x")  # type: ignore[arg-type]


# --- malformed structured output ---------------------------------------------


def test_a_wrongly_typed_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractionResult(pcs=ExtractedValue[int](status=FieldStatus.STATED, value="twenty"))


def test_a_bare_value_instead_of_an_extracted_value_is_rejected() -> None:
    """A model returning `{"origin": "Ahmedabad"}` has ignored the schema."""
    with pytest.raises(ValidationError):
        ExtractionResult(origin="Ahmedabad")  # type: ignore[arg-type]


def test_malformed_json_shaped_payload_is_rejected() -> None:
    """The shape a provider adapter will validate against in a later phase."""
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({"origin": {"status": "made_up", "value": "X"}})


def test_a_well_formed_payload_round_trips() -> None:
    payload = {
        "origin": {"status": "stated", "value": "Ahmedabad", "evidence": "from Ahmedabad"},
        "weight_kg": {"status": "not_stated"},
    }
    result = ExtractionResult.model_validate(payload)

    assert result.origin.value == "Ahmedabad"
    assert result.weight_kg.status is FieldStatus.NOT_STATED
