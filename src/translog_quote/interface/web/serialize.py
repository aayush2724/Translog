"""Domain objects, serialised for the browser.

Pure functions over domain types: no I/O, no settings, no session mutation —
the web counterpart of `interface.demo.formatting`, producing JSON-able dicts
instead of terminal text. Field labels and transit rendering are imported from
there so the two presentations cannot drift apart.

Nothing here may touch configuration. The snapshot a browser receives is built
entirely from scenario constants and pipeline outcomes; credentials have no
path into it because no function in this module can see them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.extraction import FieldStatus
from translog_quote.domain.shipment import CargoDimensions, DeliveryType, FieldName
from translog_quote.interface.demo.formatting import FIELD_LABELS, render_transit
from translog_quote.interface.demo.poc_demo import FIELD_TITLES
from translog_quote.interface.web.session import DemoStep

if TYPE_CHECKING:
    from translog_quote.domain.clarification import ClarificationMessage
    from translog_quote.domain.email import RawEmail
    from translog_quote.domain.extraction import ExtractedValue, ExtractionResult
    from translog_quote.domain.quotation import Approved, ReviewPacket
    from translog_quote.domain.rates import Rate
    from translog_quote.domain.shipment import ShipmentRecord
    from translog_quote.domain.validation import ValidationResult
    from translog_quote.interface.web.session import DemoSession
    from translog_quote.pipeline import RateSearchOutcome, TurnOutcome

Json = dict[str, object]

UNAVAILABLE = "Not specified in POC"

_STEP_ORDER: tuple[DemoStep, ...] = tuple(DemoStep)


def _reached(session: DemoSession, step: DemoStep) -> bool:
    return _STEP_ORDER.index(session.step) >= _STEP_ORDER.index(step)


# ---------------------------------------------------------------- rendering --


def _render_value(field: str, value: object) -> str | None:
    """One record field as a person reads it. ``None`` stays ``None`` — the
    browser decides how to show an absence, not this function."""
    if value is None:
        return None
    if field == "weight_kg" and isinstance(value, int | float):
        return f"{value:g} kg"
    if field == "pcs":
        return f"{value} pieces"
    if isinstance(value, CargoDimensions):
        return f"{value.length:g} (L) x {value.width:g} (W) x {value.height:g} (H) inches"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, DeliveryType):
        return "Door delivery" if value is DeliveryType.DOOR else "Airport to airport"
    return str(value)


def _conditional_note(record: ShipmentRecord, field: FieldName) -> str | None:
    """Why an empty conditional field is not simply "missing" (VR-8, VR-11)."""
    if field is FieldName.MSDS_ATTACHED:
        if record.is_chemical is None:
            return "Required only if the cargo is a chemical — chemical status not yet known"
        if record.is_chemical is False:
            return "Not required — cargo is not a chemical"
    if field is FieldName.DELIVERY_ADDRESS:
        if record.delivery_type is None:
            return "Required only for door delivery — delivery type not yet known"
        if record.delivery_type is DeliveryType.AIRPORT:
            return "Not required — airport-to-airport delivery"
    return None


# --------------------------------------------------------------- fragments --


def email_json(email: RawEmail) -> Json:
    return {
        "from": email.from_address,
        "subject": email.subject,
        "body_text": email.body_text.strip(),
        "received_at": email.received_at.isoformat(),
    }


def shipment_fields(
    record: ShipmentRecord,
    validation: ValidationResult,
    *,
    enquiry_extraction: ExtractionResult,
    reply_extraction: ExtractionResult | None = None,
    reply_supplied: tuple[FieldName, ...] = (),
) -> list[Json]:
    """Every canonical field with its value, status, provenance and evidence.

    Status is decided by the record and the deterministic validator, never by
    the extraction alone: "known" means the canonical record holds a value,
    "missing" means a validation rule demands one, "ambiguous" repeats what
    extraction could not represent, and "not_required" explains an empty
    conditional field. Nothing inferred is presented as confirmed.
    """
    missing = set(validation.missing_fields)
    supplied_by_reply = set(reply_supplied)
    fields: list[Json] = []

    for name, label in FIELD_LABELS:
        field = FieldName(name)
        value = getattr(record, name)

        from_reply = field in supplied_by_reply and reply_extraction is not None
        source_extraction = reply_extraction if from_reply else enquiry_extraction
        assert source_extraction is not None
        extracted: ExtractedValue[object] = getattr(source_extraction, name)

        if value is not None:
            status = "known"
        elif field in missing:
            status = "missing"
        elif extracted.status is FieldStatus.AMBIGUOUS:
            status = "ambiguous"
        elif extracted.status is FieldStatus.DENIED:
            status = "denied"
        else:
            status = "not_required"

        fields.append(
            {
                "field": name,
                "label": FIELD_TITLES.get(name, label),
                "value": _render_value(name, value),
                "status": status,
                "source": ("reply" if from_reply else "enquiry") if value is not None else None,
                "evidence": extracted.evidence if value is not None else None,
                "note": extracted.note or _conditional_note(record, field),
            }
        )
    return fields


def validation_json(validation: ValidationResult) -> Json:
    return {
        "is_valid": validation.is_valid,
        "issues": [
            {
                "rule_id": issue.rule_id.value,
                "field": issue.field.value,
                "severity": issue.severity.value,
                "message": issue.message,
            }
            for issue in validation.issues
        ],
    }


def extraction_json(extraction: ExtractionResult) -> Json:
    stated = len(extraction.fields_by_status(FieldStatus.STATED))
    return {"stated": stated, "assessed": len(FIELD_LABELS)}


def clarification_json(
    message: ClarificationMessage, *, to_address: str, approval: Approved | None
) -> Json:
    return {
        "status": "approved" if approval else "draft",
        "to": to_address,
        "subject": message.subject,
        "body_text": message.body_text,
        "unresolved": [
            {
                "field": item.field.value,
                "title": FIELD_TITLES.get(item.field.value, item.field.value),
                "question": item.question,
                "reason": item.reason.value,
            }
            for item in message.unresolved
        ],
        "approved_by": approval.by if approval else None,
        "approved_at": approval.at.isoformat() if approval else None,
    }


def _rate_json(rate: Rate) -> Json:
    return {
        "carrier_code": rate.carrier_code,
        "carrier_name": rate.carrier_name,
        "product": rate.product,
        "amount": str(rate.total_amount) if rate.total_amount is not None else None,
        "currency": rate.currency,
        "transit": render_transit(rate.transit),
        "transit_hours": rate.transit.hours if rate.transit is not None else None,
    }


def rates_json(outcome: RateSearchOutcome) -> Json:
    selection = outcome.selection
    recommended_ref = selection.rate.source_ref if selection else None
    return {
        "adapter_id": outcome.adapter_id,
        "uses_mock_data": outcome.uses_mock_data,
        "returned": outcome.returned,
        "query": {
            "origin_iata": outcome.query.origin_iata,
            "destination_iata": outcome.query.destination_iata,
            "weight_kg": outcome.query.weight_kg,
            "date": outcome.query.date.isoformat(),
        },
        "eligible": [
            {**_rate_json(rate), "recommended": rate.source_ref == recommended_ref}
            for rate in outcome.filtered.eligible
        ],
        "excluded": [
            {
                "carrier_code": excluded.rate.carrier_code,
                "carrier_name": excluded.rate.carrier_name,
                "reason": excluded.reason.value,
                "detail": excluded.detail,
            }
            for excluded in outcome.filtered.excluded
        ],
        "selection": None
        if selection is None
        else {
            "rate": _rate_json(selection.rate),
            "reason": selection.reason,
            "ranking": [_rate_json(rate) for rate in (selection.rate, *selection.runners_up)],
        },
        "strategy": "Fastest eligible transit — ranked by transit time, not price",
    }


def quotation_json(packet: ReviewPacket, *, uses_mock_data: bool) -> Json:
    record, rate = packet.record, packet.selection.rate
    shipment_rows = [
        ("Origin", record.origin or UNAVAILABLE),
        ("Destination", record.destination or UNAVAILABLE),
        ("Commodity", record.commodity or UNAVAILABLE),
        ("Cargo type", record.cargo_type or UNAVAILABLE),
        ("Chemical", "Yes" if record.is_chemical else "No"),
        ("Weight", _render_value("weight_kg", record.weight_kg) or UNAVAILABLE),
        ("Dimensions", _render_value("dimensions_in", record.dimensions_in) or UNAVAILABLE),
        ("Pieces", _render_value("pcs", record.pcs) or UNAVAILABLE),
        ("Delivery", _render_value("delivery_type", record.delivery_type) or UNAVAILABLE),
        ("Delivery address", record.delivery_address or UNAVAILABLE),
    ]
    rate_rows = [
        ("Carrier", f"{rate.carrier_name} ({rate.carrier_code})"),
        ("Service", rate.product),
        ("Transit time", render_transit(rate.transit)),
        ("Air freight", f"{rate.total_amount} {rate.currency}"),
    ]
    flags = ["POC QUOTATION PREVIEW", "NOT SENT", "NOT APPROVED"]
    if uses_mock_data:
        flags.insert(1, "SIMULATED WEBCARGO DATA")
    return {
        "reference": packet.request_id,
        "flags": flags,
        "shipment_rows": [{"label": label, "value": value} for label, value in shipment_rows],
        "rate_rows": [{"label": label, "value": value} for label, value in rate_rows],
        "unspecified": ["Taxes and surcharges", "Validity", "Payment terms"],
    }


# ---------------------------------------------------------------- snapshot --


def _merged_json(second: TurnOutcome, first: TurnOutcome) -> Json:
    reply_supplied = second.merge.changed
    carried = tuple(
        FieldName(name)
        for name, _ in FIELD_LABELS
        if getattr(second.record, name) is not None and FieldName(name) not in reply_supplied
    )
    return {
        "shipment": shipment_fields(
            second.record,
            second.validation,
            enquiry_extraction=first.extraction,
            reply_extraction=second.extraction,
            reply_supplied=reply_supplied,
        ),
        "carried": [FIELD_TITLES.get(f.value, f.value) for f in carried],
        "supplied": [FIELD_TITLES.get(f.value, f.value) for f in reply_supplied],
        "validation": validation_json(second.validation),
        "state": second.state.value,
    }


def snapshot(session: DemoSession) -> Json:
    """Everything the browser may know about the demonstration, in one shape.

    Later-step material (the reply, the merge, the rates, the preview) appears
    only once the demonstration has reached it, so the interface cannot show a
    result before the action that produces it has been taken.
    """
    from translog_quote.interface.web import scenario

    first = session.first
    clarification = first.clarification

    reply_visible = _reached(session, DemoStep.REPLY_PROCESSED)
    request_state = first.state.value
    if session.rates is not None:
        request_state = session.rates.state.value
    elif session.second is not None:
        request_state = session.second.state.value
    elif session.approval is not None:
        request_state = "clarification_sent"

    return {
        "demo": {
            "badge": "POC / DEMO MODE",
            "notes": {
                "extraction": (
                    "Scripted for this demonstration — mirrors the live model output "
                    "for this scenario"
                ),
                "validation": "Real — deterministic business rules, not the model",
                "clarification": "Real wording — drafts only; a human approves",
                "rates": "Demo data — WebCargo integration not connected",
                "email": "Not connected — this build cannot send email",
            },
        },
        "step": session.step.value,
        "request_state": request_state,
        "request_id": scenario.REQUEST_ID,
        "client": {
            "name": scenario.CLIENT_NAME,
            "company": scenario.CLIENT_COMPANY,
            "address": scenario.INITIAL_ENQUIRY.from_address,
        },
        "enquiry": email_json(scenario.INITIAL_ENQUIRY),
        "extraction": extraction_json(first.extraction),
        "validation": validation_json(first.validation),
        "shipment": shipment_fields(
            first.record, first.validation, enquiry_extraction=first.extraction
        ),
        "missing": [
            {
                "field": item.field.value,
                "title": FIELD_TITLES.get(item.field.value, item.field.value),
                "question": item.question,
            }
            for item in (clarification.unresolved if clarification else ())
        ],
        "clarification": None
        if clarification is None
        else clarification_json(
            clarification,
            to_address=scenario.INITIAL_ENQUIRY.from_address,
            approval=session.approval,
        ),
        "reply": email_json(scenario.CLIENT_REPLY) if reply_visible else None,
        "merged": _merged_json(session.second, first) if session.second is not None else None,
        "rates": rates_json(session.rates) if session.rates is not None else None,
        "quotation": None
        if session.packet is None or session.rates is None
        else quotation_json(session.packet, uses_mock_data=session.rates.uses_mock_data),
        "quotation_acknowledgement": None
        if session.quotation_acknowledgement is None
        else {
            "by": session.quotation_acknowledgement.by,
            "at": session.quotation_acknowledgement.at.isoformat(),
            "note": (
                "Approval recorded for demonstration only. Quotation dispatch "
                "is not built — nothing was sent."
            ),
        },
    }
