"""Shipment record merging (BR-8) — fill, confirm, and conflict.

Covers Part 5 items U, V, W, X, Y, and the two named scenarios from the brief:
the R-1002-shaped clarification flow, and the weight conflict.
"""

from __future__ import annotations

from translog_quote.domain.shipment import (
    CargoDimensions,
    DeliveryType,
    ExtractedFields,
    FieldName,
    RequestSource,
    ShipmentRecord,
    build_initial_record,
    merge_shipment,
)


def _blank_record(request_id: str = "R-TEST") -> ShipmentRecord:
    return ShipmentRecord(request_id=request_id, source=RequestSource.EMAIL)


# --- U. Merge missing values from a client reply -----------------------------


def test_u_fills_a_missing_field_from_a_reply() -> None:
    existing = _blank_record()
    reply = ExtractedFields(origin="Ahmedabad")

    result = merge_shipment(existing, reply)

    assert result.record.origin == "Ahmedabad"
    assert result.changed == (FieldName.ORIGIN,)
    assert result.unchanged == ()
    assert result.conflicts == ()


def test_u_fills_only_the_fields_the_reply_actually_states() -> None:
    existing = _blank_record().model_copy(update={"origin": "Ahmedabad"})
    reply = ExtractedFields(destination="Bahrain")

    result = merge_shipment(existing, reply)

    assert result.record.origin == "Ahmedabad"  # untouched, and not reported
    assert result.record.destination == "Bahrain"
    assert result.changed == (FieldName.DESTINATION,)


# --- V. Merge identical values -------------------------------------------


def test_v_confirming_an_identical_value_is_reported_as_unchanged() -> None:
    existing = _blank_record().model_copy(update={"weight_kg": 500.0})
    reply = ExtractedFields(weight_kg=500.0)

    result = merge_shipment(existing, reply)

    assert result.record.weight_kg == 500.0
    assert result.unchanged == (FieldName.WEIGHT_KG,)
    assert result.changed == ()
    assert result.conflicts == ()


def test_v_identical_dimensions_are_unchanged_not_a_conflict() -> None:
    dims = CargoDimensions(length=34, width=24, height=6)
    existing = _blank_record().model_copy(update={"dimensions_in": dims})
    reply = ExtractedFields(dimensions_in=CargoDimensions(length=34, width=24, height=6))

    result = merge_shipment(existing, reply)

    assert result.unchanged == (FieldName.DIMENSIONS_IN,)
    assert result.conflicts == ()


# --- W. Detect conflicting values -----------------------------------------


def test_w_conflicting_weight_is_reported_not_resolved() -> None:
    """The named conflict scenario: 500 kg, then a reply claiming 700 kg."""
    existing = _blank_record().model_copy(update={"weight_kg": 500.0})
    reply = ExtractedFields(weight_kg=700.0)

    result = merge_shipment(existing, reply)

    # The original shipment is not silently overwritten...
    assert result.record.weight_kg == 500.0
    # ...but the conflict is surfaced, naming the field and carrying both values,
    # so a later, explicit handling layer has what it needs.
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.field is FieldName.WEIGHT_KG
    assert conflict.existing_value == 500.0
    assert conflict.new_value == 700.0
    # Neither "silently choose 500" nor "silently choose 700": the field is
    # reported as a conflict, not as a change or a confirmation.
    assert result.changed == ()
    assert result.unchanged == ()


def test_w_conflict_does_not_prevent_other_fields_from_filling() -> None:
    existing = _blank_record().model_copy(update={"weight_kg": 500.0})
    reply = ExtractedFields(weight_kg=700.0, commodity="Polyisobutylene Additive")

    result = merge_shipment(existing, reply)

    assert result.record.commodity == "Polyisobutylene Additive"
    assert result.changed == (FieldName.COMMODITY,)
    assert len(result.conflicts) == 1


def test_w_multiple_conflicts_are_all_reported() -> None:
    existing = _blank_record().model_copy(update={"weight_kg": 500.0, "cargo_type": "Non Haz"})
    reply = ExtractedFields(weight_kg=700.0, cargo_type="Haz")

    result = merge_shipment(existing, reply)

    fields_in_conflict = {c.field for c in result.conflicts}
    assert fields_in_conflict == {FieldName.WEIGHT_KG, FieldName.CARGO_TYPE}


# --- X. Do not create a new shipment during a merge -----------------------


def test_x_merge_returns_a_record_not_a_new_shipment_identity() -> None:
    existing = _blank_record(request_id="R-1002").model_copy(update={"origin": "Ahmedabad"})
    reply = ExtractedFields(destination="Bahrain")

    result = merge_shipment(existing, reply)

    assert result.record.request_id == existing.request_id
    assert result.record.source == existing.source


def test_x_a_reply_with_no_new_information_leaves_the_record_equal() -> None:
    existing = _blank_record().model_copy(update={"origin": "Ahmedabad"})
    reply = ExtractedFields()

    result = merge_shipment(existing, reply)

    assert result.record == existing
    assert result.changed == ()
    assert result.unchanged == ()
    assert result.conflicts == ()


# --- Y. Preserve request/thread correlation -------------------------------


def test_y_request_id_survives_repeated_merges() -> None:
    record = build_initial_record(
        "R-2002", RequestSource.EMAIL, ExtractedFields(origin="Ahmedabad")
    )
    record = merge_shipment(record, ExtractedFields(destination="Bahrain")).record
    record = merge_shipment(record, ExtractedFields(commodity="Chemicals")).record

    assert record.request_id == "R-2002"
    assert record.source == RequestSource.EMAIL


# --- build_initial_record --------------------------------------------------


def test_initial_record_is_a_merge_into_a_blank_record() -> None:
    fields = ExtractedFields(origin="Ahmedabad", destination="Bahrain", weight_kg=500.0)
    record = build_initial_record("R-1001", RequestSource.EMAIL, fields)

    assert record.request_id == "R-1001"
    assert record.origin == "Ahmedabad"
    assert record.destination == "Bahrain"
    assert record.weight_kg == 500.0
    assert record.commodity is None


def test_initial_record_normalizes_incoming_text() -> None:
    fields = ExtractedFields(origin="  Ahmedabad  ")
    record = build_initial_record("R-1003", RequestSource.EMAIL, fields)

    assert record.origin == "Ahmedabad"


# --- The named clarification-flow scenario --------------------------------


def test_the_r1002_clarification_reply_fills_exactly_the_stated_fields() -> None:
    """The brief's own worked example, reproduced field for field.

    Initial: origin, destination, weight, dimensions known; commodity, pcs,
    is_chemical, delivery_type null. Reply states exactly those four. After the
    merge, all four must be populated and nothing else disturbed.
    """
    initial = build_initial_record(
        "R-1002",
        RequestSource.EMAIL,
        ExtractedFields(
            origin="Ahmedabad",
            destination="Bahrain",
            weight_kg=500.0,
            dimensions_in=CargoDimensions(length=34, width=24, height=6),
        ),
    )
    assert initial.commodity is None
    assert initial.pcs is None
    assert initial.is_chemical is None
    assert initial.delivery_type is None

    reply = ExtractedFields(
        commodity="Polyisobutylene Additive",
        pcs=20,
        is_chemical=True,
        delivery_type=DeliveryType.DOOR,
    )
    result = merge_shipment(initial, reply)
    merged = result.record

    assert merged.commodity == "Polyisobutylene Additive"
    assert merged.pcs == 20
    assert merged.is_chemical is True
    assert merged.delivery_type is DeliveryType.DOOR
    assert set(result.changed) == {
        FieldName.COMMODITY,
        FieldName.PCS,
        FieldName.IS_CHEMICAL,
        FieldName.DELIVERY_TYPE,
    }
    assert result.conflicts == ()
    # Fields the reply never mentioned are carried over untouched.
    assert merged.origin == "Ahmedabad"
    assert merged.weight_kg == 500.0
