"""End-to-end merge + validate, against the two scenarios named in the brief.

No orchestration, no I/O, no state machine — just the two Phase 2 primitives
(merge_shipment, validate_shipment) chained by hand, exactly as a later pipeline
stage will chain them.
"""

from __future__ import annotations

from translog_quote.domain.shipment import (
    CargoDimensions,
    DeliveryType,
    ExtractedFields,
    RequestSource,
    build_initial_record,
    merge_shipment,
)
from translog_quote.domain.validation import ValidationRuleId, validate_shipment


def test_important_flow_clarification_reply_leaves_two_conditional_gaps() -> None:
    """The brief's own IMPORTANT TEST, asserted literally.

    Initial fields are exactly as given: origin, destination, weight, dimensions
    known; commodity, pcs, is_chemical, delivery_type null. (`cargo_type` is not
    part of the brief's initial listing, so it is also missing here — the
    assertions below only claim what the brief claims: after the reply, MSDS and
    delivery_address are missing, and the shipment must not be considered
    complete.)
    """
    initial = build_initial_record(
        "R-IMPORTANT",
        RequestSource.EMAIL,
        ExtractedFields(
            origin="Ahmedabad",
            destination="Bahrain",
            weight_kg=500.0,
            dimensions_in=CargoDimensions(length=34, width=24, height=6),
        ),
    )

    reply = ExtractedFields(
        commodity="Polyisobutylene Additive",
        pcs=20,
        is_chemical=True,
        delivery_type=DeliveryType.DOOR,
    )
    merged = merge_shipment(initial, reply).record

    assert merged.commodity == "Polyisobutylene Additive"
    assert merged.pcs == 20
    assert merged.is_chemical is True
    assert merged.delivery_type is DeliveryType.DOOR

    result = validate_shipment(merged)

    assert not result.is_valid  # must NOT be considered complete
    rule_ids = {i.rule_id for i in result.issues}
    assert ValidationRuleId.MSDS_REQUIRED_FOR_CHEMICAL in rule_ids
    assert ValidationRuleId.ADDRESS_REQUIRED_FOR_DOOR in rule_ids


def test_reference_thread_flow_only_msds_and_address_remain_missing() -> None:
    """The same flow, fully faithful to docs/reference/ (cargo_type included,
    as it was in the real first email: "Cargo type Non Haz"). With every field
    the real thread ever supplied now present, exactly the two conditional
    rules should fire — nothing else.
    """
    initial = build_initial_record(
        "R-1002",
        RequestSource.EMAIL,
        ExtractedFields(
            origin="Ahmedabad",
            destination="Bahrain",
            weight_kg=500.0,
            dimensions_in=CargoDimensions(length=34, width=24, height=6),
            cargo_type="Non Haz",
        ),
    )

    reply = ExtractedFields(
        commodity="Polyisobutylene Additive",
        pcs=20,
        is_chemical=True,
        delivery_type=DeliveryType.DOOR,
    )
    merge_result = merge_shipment(initial, reply)
    assert merge_result.conflicts == ()

    result = validate_shipment(merge_result.record)

    assert not result.is_valid
    assert {i.rule_id for i in result.issues} == {
        ValidationRuleId.MSDS_REQUIRED_FOR_CHEMICAL,
        ValidationRuleId.ADDRESS_REQUIRED_FOR_DOOR,
    }


def test_conflict_scenario_end_to_end() -> None:
    """The named conflict test: a weight correction must not silently resolve,
    and must not silently make the shipment look complete or incomplete based
    on which value "won" — because neither value wins.
    """
    initial = build_initial_record(
        "R-CONFLICT",
        RequestSource.EMAIL,
        ExtractedFields(
            origin="Ahmedabad",
            destination="Bahrain",
            weight_kg=500.0,
            dimensions_in=CargoDimensions(length=34, width=24, height=6),
            commodity="Polyisobutylene Additive",
            cargo_type="Non Haz",
            is_chemical=False,
            pcs=20,
            delivery_type=DeliveryType.AIRPORT,
        ),
    )
    assert validate_shipment(initial).is_valid  # fully valid before the correction

    correction = ExtractedFields(weight_kg=700.0)
    merge_result = merge_shipment(initial, correction)

    assert merge_result.record.weight_kg == 500.0  # not silently overwritten
    assert len(merge_result.conflicts) == 1
    conflict = merge_result.conflicts[0]
    assert conflict.existing_value == 500.0
    assert conflict.new_value == 700.0

    # The record is otherwise unaffected: validation still passes, because the
    # merge did not touch the field at all — it flagged, rather than resolved.
    assert validate_shipment(merge_result.record).is_valid
