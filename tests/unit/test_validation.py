"""The validation engine.

Covers Part 5 items A through T, plus Z (determinism). Each test is named for
the letter it satisfies so a failure points straight at the brief's own list.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from translog_quote.domain.shipment import (
    CargoDimensions,
    DeliveryType,
    RequestSource,
    ShipmentRecord,
)
from translog_quote.domain.validation import ValidationRuleId, ValidationSeverity, validate_shipment


def _complete_record(**overrides: object) -> ShipmentRecord:
    """A fully valid, non-chemical, airport-delivery shipment. Override fields
    per test so each case isolates exactly the thing it is checking."""
    base: dict[str, object] = {
        "request_id": "R-TEST",
        "source": RequestSource.EMAIL,
        "origin": "Ahmedabad",
        "destination": "Bahrain",
        "weight_kg": 500.0,
        "dimensions_in": CargoDimensions(length=34, width=24, height=6),
        "commodity": "Polyisobutylene Additive",
        "cargo_type": "Non Haz",
        "is_chemical": False,
        "pcs": 20,
        "delivery_type": DeliveryType.AIRPORT,
    }
    base.update(overrides)
    return ShipmentRecord(**base)


# --- A. Completely valid shipment ------------------------------------------


def test_a_completely_valid_shipment_has_no_issues() -> None:
    result = validate_shipment(_complete_record())

    assert result.is_valid
    assert result.issues == ()


# --- B-J. Each always-required field, missing in isolation ------------------


def test_b_missing_origin() -> None:
    result = validate_shipment(_complete_record(origin=None))

    assert not result.is_valid
    assert ValidationRuleId.ORIGIN_REQUIRED in {i.rule_id for i in result.issues}


def test_c_missing_destination() -> None:
    result = validate_shipment(_complete_record(destination=None))

    assert ValidationRuleId.DESTINATION_REQUIRED in {i.rule_id for i in result.issues}


def test_d_missing_weight() -> None:
    result = validate_shipment(_complete_record(weight_kg=None))

    assert ValidationRuleId.WEIGHT_REQUIRED in {i.rule_id for i in result.issues}


def test_e_missing_dimensions() -> None:
    result = validate_shipment(_complete_record(dimensions_in=None))

    assert ValidationRuleId.DIMENSIONS_REQUIRED in {i.rule_id for i in result.issues}


def test_f_missing_commodity() -> None:
    result = validate_shipment(_complete_record(commodity=None))

    assert ValidationRuleId.COMMODITY_REQUIRED in {i.rule_id for i in result.issues}


def test_g_missing_cargo_type() -> None:
    result = validate_shipment(_complete_record(cargo_type=None))

    assert ValidationRuleId.CARGO_TYPE_REQUIRED in {i.rule_id for i in result.issues}


def test_h_missing_chemical_status() -> None:
    result = validate_shipment(_complete_record(is_chemical=None))

    assert ValidationRuleId.CHEMICAL_STATUS_REQUIRED in {i.rule_id for i in result.issues}


def test_i_missing_pieces() -> None:
    result = validate_shipment(_complete_record(pcs=None))

    assert ValidationRuleId.PCS_REQUIRED in {i.rule_id for i in result.issues}


def test_j_missing_delivery_type() -> None:
    result = validate_shipment(_complete_record(delivery_type=None))

    assert ValidationRuleId.DELIVERY_TYPE_REQUIRED in {i.rule_id for i in result.issues}


# --- K, L, M. The chemical / MSDS conditional (VR-8) ------------------------


def test_k_chemical_shipment_with_msds_present_is_valid() -> None:
    result = validate_shipment(_complete_record(is_chemical=True, msds_attached=True))

    assert result.is_valid


def test_k_chemical_shipment_with_msds_explicitly_absent_is_still_valid() -> None:
    """The client answered "no MSDS" — the field is known, not missing."""
    result = validate_shipment(_complete_record(is_chemical=True, msds_attached=False))

    assert result.is_valid


def test_l_chemical_shipment_with_msds_missing_is_invalid() -> None:
    result = validate_shipment(_complete_record(is_chemical=True, msds_attached=None))

    assert not result.is_valid
    assert ValidationRuleId.MSDS_REQUIRED_FOR_CHEMICAL in {i.rule_id for i in result.issues}


def test_m_non_chemical_shipment_never_needs_msds() -> None:
    result = validate_shipment(_complete_record(is_chemical=False, msds_attached=None))

    assert result.is_valid
    assert ValidationRuleId.MSDS_REQUIRED_FOR_CHEMICAL not in {i.rule_id for i in result.issues}


# --- N, O, P. The door-delivery / address conditional (VR-11) --------------


def test_n_door_delivery_with_address_is_valid() -> None:
    result = validate_shipment(
        _complete_record(delivery_type=DeliveryType.DOOR, delivery_address="Hidd Industrial Area")
    )

    assert result.is_valid


def test_o_door_delivery_without_address_is_invalid() -> None:
    result = validate_shipment(
        _complete_record(delivery_type=DeliveryType.DOOR, delivery_address=None)
    )

    assert not result.is_valid
    assert ValidationRuleId.ADDRESS_REQUIRED_FOR_DOOR in {i.rule_id for i in result.issues}


def test_p_non_door_delivery_never_needs_an_address() -> None:
    result = validate_shipment(
        _complete_record(delivery_type=DeliveryType.AIRPORT, delivery_address=None)
    )

    assert result.is_valid
    assert ValidationRuleId.ADDRESS_REQUIRED_FOR_DOOR not in {i.rule_id for i in result.issues}


# --- Q. Multiple simultaneous missing fields --------------------------------


def test_q_every_missing_field_is_reported_in_one_pass() -> None:
    """The validator never stops at the first problem (BR-9 depends on this)."""
    result = validate_shipment(
        _complete_record(commodity=None, pcs=None, is_chemical=None, delivery_type=None)
    )

    rule_ids = {i.rule_id for i in result.issues}
    assert rule_ids == {
        ValidationRuleId.COMMODITY_REQUIRED,
        ValidationRuleId.PCS_REQUIRED,
        ValidationRuleId.CHEMICAL_STATUS_REQUIRED,
        ValidationRuleId.DELIVERY_TYPE_REQUIRED,
    }


# --- R, S. Invalid numeric fields -------------------------------------------


def test_r_negative_weight_is_invalid_not_missing() -> None:
    result = validate_shipment(_complete_record(weight_kg=-500.0))

    assert not result.is_valid
    issue = next(i for i in result.issues if i.rule_id == ValidationRuleId.WEIGHT_INVALID)
    assert issue.severity is ValidationSeverity.INVALID
    assert ValidationRuleId.WEIGHT_REQUIRED not in {i.rule_id for i in result.issues}


def test_r_zero_weight_is_invalid() -> None:
    result = validate_shipment(_complete_record(weight_kg=0.0))

    assert ValidationRuleId.WEIGHT_INVALID in {i.rule_id for i in result.issues}


def test_s_negative_pieces_is_invalid_not_missing() -> None:
    result = validate_shipment(_complete_record(pcs=-3))

    issue = next(i for i in result.issues if i.rule_id == ValidationRuleId.PCS_INVALID)
    assert issue.severity is ValidationSeverity.INVALID
    assert ValidationRuleId.PCS_REQUIRED not in {i.rule_id for i in result.issues}


def test_s_zero_pieces_is_invalid() -> None:
    result = validate_shipment(_complete_record(pcs=0))

    assert ValidationRuleId.PCS_INVALID in {i.rule_id for i in result.issues}


# --- T. Invalid dimensions ---------------------------------------------------


def test_t_a_non_positive_dimension_cannot_be_constructed() -> None:
    """`CargoDimensions` enforces positivity at the type boundary (Phase 1),
    so invalid dimensions are caught before a `ShipmentRecord` can ever hold
    them — there is no reachable ValidationRuleId for this failure mode; the
    validator's `DIMENSIONS_REQUIRED` rule only ever sees "present" or "absent".
    """
    with pytest.raises(ValidationError):
        CargoDimensions(length=-34, width=24, height=6)

    with pytest.raises(ValidationError):
        CargoDimensions(length=34, width=0, height=6)


# --- Z. Determinism -----------------------------------------------------------


def test_z_repeated_validation_of_an_unchanged_record_is_identical() -> None:
    record = _complete_record(is_chemical=True, msds_attached=None)

    first = validate_shipment(record)
    second = validate_shipment(record)

    assert first == second


def test_z_validation_does_not_mutate_the_record() -> None:
    record = _complete_record(commodity=None)
    before = record.model_copy()

    validate_shipment(record)

    assert record == before
