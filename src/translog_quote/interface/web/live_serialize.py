"""The live session, serialised for the browser.

Pure functions over domain types and session facts: no I/O, no `Settings`, no
mutation. Credentials have no path into a snapshot because no function here can
see one — the same rule `serialize` follows for the scripted POC, and the
reason the "no credential in any snapshot" test can be written at all.

Field rendering is imported from `serialize` and `formatting` rather than
rewritten, so the live view and the scripted view cannot drift apart on how a
weight or a transit time is spelled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.domain.quotation import SIMULATED_RATE_NOTICE
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.demo.formatting import FIELD_LABELS, render_transit
from translog_quote.interface.demo.poc_demo import FIELD_TITLES
from translog_quote.interface.web.serialize import (
    _render_value as render_value,
)
from translog_quote.interface.web.serialize import (
    email_json,
    validation_json,
)
from translog_quote.pipeline.audit import AuditEventType

if TYPE_CHECKING:
    from translog_quote.domain.quotation import ReviewPacket
    from translog_quote.domain.rates import Rate
    from translog_quote.interface.web.live_session import LiveRequest, LiveSession
    from translog_quote.pipeline import RateSearchOutcome
    from translog_quote.pipeline.audit import AuditEvent

Json = dict[str, object]

#: The disclosure the interface must never render a simulated rate without.
#: Imported from the domain rather than retyped, so the browser cannot end up
#: showing a softer wording than the client's own quotation email carries.
SIMULATED_BANNER = "SIMULATED WEBCARGO DATA — DEMO ONLY"

#: The audit event a message's arrival is recorded under. Named because the
#: timeline has to treat it specially: it is the one event that can legitimately
#: repeat for a single message.
EMAIL_RECEIVED = "email_received"

#: The activity timeline: one row per stage, the audit event that proves it
#: happened, and which occurrence of that event to read.
#:
#: Timestamps are looked up, never generated. A stage with no matching event is
#: reported as not yet reached rather than given a plausible time — a timeline
#: that invents its own history is worse than one with gaps in it, because a
#: reader cannot tell the two apart.
#: Each row is (key, label once done, audit event, which occurrence, label
#: while not yet done). The fifth element exists because a few stages describe
#: an *outcome*, and reading that outcome back before it has happened states
#: something untrue: a row saying "Clarification sent" above a note saying
#: "Waiting for a person to approve and send" contradicts itself, and the half
#: a presenter reads aloud is the wrong half. `None` means the stage reads the
#: same either way.
TIMELINE: tuple[tuple[str, str, str, int, str | None], ...] = (
    ("enquiry_received", "Enquiry email received", "email_received", 0, None),
    ("extraction", "AI extraction", "extraction_called", 0, None),
    ("validation", "Validation", "validated", 0, None),
    (
        "clarification_sent",
        "Clarification sent",
        "clarification_sent",
        0,
        "Clarification awaiting approval",
    ),
    ("reply_received", "Client reply received", "email_received", 1, None),
    ("rate_search", "Rate search", "rates_fetched", 0, None),
    ("rate_selected", "Rate selected", "rate_selected", 0, None),
    ("approval_decided", "Human approval", "approval_decided", 0, None),
    ("quotation_sent", "Quotation sent", "quotation_sent", 0, None),
)

#: The audit events that prove a request entered the clarification loop, and
#: the timeline steps that exist only inside it.
_CLARIFICATION_EVENTS = frozenset(
    {
        AuditEventType.CLARIFICATION_DRAFTED,
        AuditEventType.CLARIFICATION_APPROVED,
        AuditEventType.CLARIFICATION_SENT,
    }
)
_CLARIFICATION_STEPS = frozenset({"clarification_sent", "reply_received"})

#: What the interface says about the step a request is currently sitting on.
#: Only ever attached to the *current* step, so a pending step further down
#: reads as pending rather than as something being waited for.
_WAITING_NOTES: dict[str, str] = {
    "clarification_sent": "Waiting for a person to approve and send",
    "reply_received": "Waiting for client reply",
    "approval_decided": "Waiting for approval",
}

#: Who a waiting step is waiting on. The interface marks the two differently,
#: because "we are blocked on you" and "we are blocked on the client" are the
#: two facts a presenter is actually narrating, and a single marker for both
#: leaves the room unable to tell whose move it is.
_WAITING_ON: dict[str, str] = {
    "clarification_sent": "operator",
    "reply_received": "client",
    "approval_decided": "operator",
}

_STATUS_LABELS: dict[RequestState, tuple[str, str]] = {
    RequestState.RECEIVED: ("RECEIVED", "gray"),
    RequestState.EXTRACTED: ("EXTRACTED", "blue"),
    RequestState.NEEDS_INFO: ("INFORMATION REQUIRED", "amber"),
    RequestState.CLARIFICATION_SENT: ("AWAITING CLIENT REPLY", "blue"),
    RequestState.VALIDATED: ("VALIDATED", "green"),
    RequestState.RATE_SELECTED: ("AWAITING APPROVAL", "amber"),
    RequestState.PENDING_APPROVAL: ("AWAITING APPROVAL", "amber"),
    RequestState.QUOTATION_SENT: ("QUOTATION SENT", "green"),
    RequestState.MAKER_REJECTED: ("DECLINED — NOT SENT", "gray"),
    RequestState.NO_ELIGIBLE_RATE: ("NO ELIGIBLE RATE", "gray"),
    RequestState.MANUAL_REVIEW: ("MANUAL REVIEW", "amber"),
    RequestState.FAILED: ("FAILED", "gray"),
    RequestState.ACCEPTED: ("ACCEPTED", "green"),
    RequestState.DECLINED: ("CLIENT DECLINED", "gray"),
}


def status_json(request: LiveRequest) -> Json:
    label, tone = _STATUS_LABELS.get(request.state, (request.state.value.upper(), "gray"))
    return {"state": request.state.value, "label": label, "tone": tone}


def shipment_json(request: LiveRequest) -> list[Json]:
    """Every canonical field with its value and why it is empty when it is.

    Status comes from the record and the deterministic validator, never from an
    extraction: "known" means the canonical record holds a value and "missing"
    means a rule demands one. Nothing inferred is presented as confirmed.
    """
    missing = set(request.validation.missing_fields)
    rows: list[Json] = []
    for name, label in FIELD_LABELS:
        value = getattr(request.record, name)
        if value is not None:
            status = "known"
        elif any(field.value == name for field in missing):
            status = "missing"
        else:
            status = "not_required"
        rows.append(
            {
                "field": name,
                "label": FIELD_TITLES.get(name, label),
                "value": render_value(name, value),
                "status": status,
                "source": "reply" if name in request.merged_fields else None,
            }
        )
    return rows


def _rate_json(rate: Rate) -> Json:
    return {
        "carrier_code": rate.carrier_code,
        "carrier_name": rate.carrier_name,
        "product": rate.product,
        "amount": str(rate.total_amount) if rate.total_amount is not None else None,
        "currency": rate.currency,
        "transit": render_transit(rate.transit),
    }


def rates_json(outcome: RateSearchOutcome) -> Json:
    """The whole rate pipeline, including what it threw away and why.

    Exclusions are carried deliberately: the approver has to see *why* a
    carrier is absent, and silence there is indistinguishable from a bug.
    """
    selection = outcome.selection
    return {
        "simulated": outcome.uses_mock_data,
        "banner": SIMULATED_BANNER if outcome.uses_mock_data else None,
        "adapter_id": outcome.adapter_id,
        "returned": outcome.returned,
        "eligible_count": len(outcome.filtered.eligible),
        "excluded_count": len(outcome.filtered.excluded),
        "query": {
            # The place as the client stated it, unless a resolver supplied a
            # real identifier. Renamed from "origin_iata": the value is no
            # longer always an airport code, and a key that says it is would
            # be the same lie the lane table used to tell.
            "origin": outcome.query.origin.display,
            "destination": outcome.query.destination.display,
            "weight_kg": outcome.query.weight_kg,
            "date": outcome.query.date.isoformat(),
        },
        "eligible": [_rate_json(rate) for rate in outcome.filtered.eligible],
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
            **_rate_json(selection.rate),
            "reason": selection.reason,
            "runners_up": [_rate_json(rate) for rate in selection.runners_up],
        },
        "strategy": "Fastest eligible transit — ranked by transit time, not price",
    }


def approval_json(packet: ReviewPacket, outcome: RateSearchOutcome, *, approver: str) -> Json:
    """Everything the approval card must show before anyone may click.

    The simulated-rate warning is part of the payload rather than a frontend
    decoration: an approver deciding on invented numbers must be told so by the
    same system that produced them.
    """
    rate = packet.selection.rate
    return {
        "reference": packet.request_id,
        "simulated": outcome.uses_mock_data,
        "banner": SIMULATED_BANNER if outcome.uses_mock_data else None,
        "notice": SIMULATED_RATE_NOTICE if outcome.uses_mock_data else None,
        "review_sent_to": approver,
        "carrier": f"{rate.carrier_name} ({rate.carrier_code})",
        "service": rate.product,
        "transit": render_transit(rate.transit),
        "price": f"{rate.total_amount} {rate.currency}",
        "reason": packet.selection.reason,
        "excluded": [
            {
                "carrier_name": excluded.rate.carrier_name,
                "reason": excluded.reason.value,
                "detail": excluded.detail,
            }
            for excluded in packet.rates.excluded
        ],
    }


def decision_json(request: LiveRequest) -> Json | None:
    """What the person decided, and what followed from it.

    `sent` is reported as observed rather than inferred from the state: "the
    state says QUOTATION_SENT" and "the sink accepted a message" are two
    different claims, and a demonstration is entitled to show both.
    """
    if request.decision is None:
        return None
    approved = request.state is RequestState.QUOTATION_SENT
    return {
        "approved": approved,
        "by": request.decision.by,
        "at": request.decision.at.isoformat(),
        "reason": getattr(request.decision, "reason", ""),
        "sent": request.quotation_sent,
        "headline": "APPROVED — quotation sent to the client"
        if approved
        else "DECLINED — quotation not sent",
    }


def _occurrence(events: list[AuditEvent], name: str, index: int) -> AuditEvent | None:
    """The nth event of a type, or None. How a stage learns when it happened."""
    if name == EMAIL_RECEIVED:
        return _nth_distinct_email(events, index)
    matching = [event for event in events if event.event.value == name]
    return matching[index] if len(matching) > index else None


def _nth_distinct_email(events: list[AuditEvent], index: int) -> AuditEvent | None:
    """The nth distinct *message*, in arrival order — not the nth event.

    Counting raw `email_received` events counts the same message twice. A
    request still awaiting its clarification is deliberately not persisted, so
    every new session re-ingests its enquiry from the mailbox and appends
    another event carrying the very same Message-ID. Indexing that made an
    enquiry processed twice look exactly like a client reply: the timeline
    showed "Client reply received" beneath "Clarification awaiting approval",
    which cannot both be true.

    The Message-ID is what actually distinguishes one message from another, so
    that is what is counted.
    """
    first_seen: dict[str, AuditEvent] = {}
    for event in events:
        if event.event.value != EMAIL_RECEIVED:
            continue
        message_id = event.detail.get("message_id")
        if isinstance(message_id, str) and message_id not in first_seen:
            first_seen[message_id] = event
    ordered = list(first_seen.values())
    return ordered[index] if len(ordered) > index else None


def timeline_json(request: LiveRequest, events: list[AuditEvent]) -> list[Json]:
    """The activity timeline for one request, from its own audit trail.

    Every completed row carries the real moment its event was recorded. The two
    stages that correspond to an actual email — the enquiry and the reply —
    prefer the message's own ``Date`` header over the moment we processed it,
    because "when the client wrote" is the fact a presenter is describing.

    Nothing here is hardcoded as complete: a row is done because an event for
    it exists, and the first row that is not done is the one the request is
    sitting on.
    """
    mine = [event for event in events if event.request_id == request.request_id]
    emails = {"enquiry_received": request.enquiry, "reply_received": request.reply}

    # Whether this request ever entered the clarification loop. A complete
    # enquiry never does, and its timeline must not carry the two steps that
    # belong to that loop: rendered as pending they read as things still owed
    # — "Clarification awaiting approval", "Client reply received" — which
    # invents a human action nobody needs to take. Evidence, not inference:
    # a clarification event in the audit trail, a draft currently held, the
    # NEEDS_INFO state itself, or a merged reply (which only the loop produces).
    saw_clarification = (
        request.clarification is not None
        or request.state is RequestState.NEEDS_INFO
        or request.reply_received
        or any(event.event in _CLARIFICATION_EVENTS for event in mine)
    )

    rows: list[Json] = []
    current_marked = False
    for key, label, event_name, index, pending_label in TIMELINE:
        if key in _CLARIFICATION_STEPS and not saw_clarification:
            continue
        event = _occurrence(mine, event_name, index)
        email = emails.get(key)
        at = email.received_at if email is not None else (event.at if event else None)

        done = at is not None
        current = not done and not current_marked
        if current:
            current_marked = True

        rows.append(
            {
                "key": key,
                # The done label is only used once the audit event proving the
                # stage happened actually exists. For the clarification that
                # event is emitted after the sink accepted the message, so the
                # row cannot read "sent" until something really was.
                "label": label if done else (pending_label or label),
                "state": "done" if done else "current" if current else "pending",
                "at": at.isoformat() if at is not None else None,
                "note": _WAITING_NOTES.get(key, "Pending") if current else None,
                "waiting_on": _WAITING_ON.get(key) if current else None,
            }
        )

    if request.state is RequestState.MANUAL_REVIEW:
        # Automated processing has stopped, and the timeline must say so.
        # Without this, the first not-yet-done template row rendered as the
        # current step — a manual-review request read "Rate search — Pending",
        # which is a promise the workflow will not keep. The rows that already
        # happened stay; the ones that will not happen automatically are
        # replaced by the one true statement about where the request is.
        rows = [row for row in rows if row["state"] == "done"]
        rows.append(
            {
                "key": "manual_review",
                "label": "Manual review",
                "state": "current",
                "at": None,
                "note": "Handed to a person — automated processing has stopped",
                "waiting_on": "operator",
            }
        )
    return rows


def audit_json(events: list[AuditEvent]) -> list[Json]:
    """The activity timeline.

    The pipeline's own audit trail, not a second narration written for the
    screen — so what the operator explains to the room is the same evidence the
    system recorded. Details carry field names, counts, carrier codes and
    approver names; never a body, an address, or a credential.
    """
    return [
        {
            "event": event.event.value,
            "request_id": event.request_id,
            "at": event.at.isoformat(),
            "detail": {key: str(value) for key, value in sorted(event.detail.items())},
        }
        for event in events
    ]


def _headline(request: LiveRequest) -> str:
    """What to call this request on a card.

    The email's own subject, with any reply prefix and the lane suffix trimmed
    so a card reads as a title rather than as a mail header. Falls back to the
    request id, which is always present — a card must never be blank.
    """
    subject = request.subject.strip()
    while subject.lower().startswith("re:"):
        subject = subject[3:].strip()
    head, separator, _ = subject.partition(" - ")
    return (head if separator else subject) or request.request_id


#: States a demonstration request is still "new" in: nothing has been sent for
#: it yet, so the presenter has not acted on it. Once a clarification goes out
#: the request is under way and the badge would be describing the past.
_UNTOUCHED = frozenset({RequestState.RECEIVED, RequestState.EXTRACTED, RequestState.NEEDS_INFO})


def request_summary(request: LiveRequest) -> Json:
    """One dashboard row.

    Carries enough to be useful before extraction has filled anything in: the
    subject and the received time come from the email itself, so a request that
    is still being processed reads as a real enquiry rather than as an empty
    row of dashes.

    Every row is a request of the current demonstration — the snapshot passes
    nothing else — so there is no longer a flag saying which ones are.
    """
    received = request.enquiry.received_at if request.enquiry else None
    fields = request.shipment_field_count
    return {
        "request_id": request.request_id,
        "is_new": request.state in _UNTOUCHED,
        "headline": _headline(request),
        # What the pipeline's own extraction found, reported so the operator
        # can see *why* a message is grouped where it is rather than trusting
        # the grouping. Nothing is hidden — an unrecognised message is still
        # listed, still openable, and still explains itself.
        "is_enquiry": request.looks_like_an_enquiry,
        "shipment_fields": fields,
        "not_enquiry_reason": None
        if fields
        else (
            "No shipment details found. Extraction returned no origin, "
            "destination, weight, commodity or dimensions, so this message did "
            "not state a shipment."
        ),
        "subject": request.subject or None,
        "client_address": request.client_address,
        "origin": request.record.origin,
        "destination": request.record.destination,
        "lane": " → ".join(
            part for part in (request.record.origin, request.record.destination) if part
        )
        or None,
        "weight": render_value("weight_kg", request.record.weight_kg),
        "received_at": received.isoformat() if received else None,
        "status": status_json(request),
        "awaiting_clarification": request.awaiting_clarification_approval,
        # Why this request has no rates, when it has none. Reported rather
        # than hidden: a request stuck before pricing looks idle otherwise.
        "rate_failure": request.rate_failure,
        "manual_review_notes": list(request.manual_review_notes),
        "waiting_replies": len(request.waiting_replies),
        "awaiting_decision": request.awaiting_quotation_decision,
        "settled": request.is_settled,
    }


def request_detail(session: LiveSession, request: LiveRequest) -> Json:
    """One request, in full — the screen a presentation is driven from."""
    clarification = request.clarification
    return {
        "request_id": request.request_id,
        "headline": _headline(request),
        "subject": request.subject or None,
        "is_enquiry": request.looks_like_an_enquiry,
        "shipment_fields": request.shipment_field_count,
        "timeline": timeline_json(request, session.audit.events),
        "reply": email_json(request.reply) if request.reply else None,
        "client_address": request.client_address,
        "status": status_json(request),
        "enquiry": email_json(request.enquiry) if request.enquiry else None,
        "latest_email": email_json(request.latest_email) if request.latest_email else None,
        "reply_received": request.reply_received,
        "merged": [FIELD_TITLES.get(f, f) for f in request.merged_fields],
        "carried": [FIELD_TITLES.get(f, f) for f in request.carried_fields],
        "shipment": shipment_json(request),
        "rate_failure": request.rate_failure,
        "manual_review_notes": list(request.manual_review_notes),
        "validation": validation_json(request.validation),
        "clarification": None
        if clarification is None
        else {
            "subject": clarification.subject,
            "body_text": clarification.body_text,
            "unresolved": [
                {
                    "field": item.field.value,
                    "title": FIELD_TITLES.get(item.field.value, item.field.value),
                    "question": item.question,
                }
                for item in clarification.unresolved
            ],
            "sent_by": request.clarification_sent_by,
            "awaiting_approval": request.awaiting_clarification_approval,
        },
        "clarification_sent_by": request.clarification_sent_by,
        # A reply is already here and cannot be processed until a person sends
        # the clarification it answers. Reported so the operator sees why a
        # request that looks idle is actually waiting on them, rather than on
        # the client.
        "waiting_replies": len(request.waiting_replies),
        "rates": rates_json(request.rates) if request.rates is not None else None,
        "approval": None
        if request.packet is None or request.rates is None
        else approval_json(request.packet, request.rates, approver=session.approver_address),
        "awaiting_decision": request.awaiting_quotation_decision,
        "decision": decision_json(request),
    }


def snapshot(session: LiveSession, *, selected: str | None = None) -> Json:
    """Everything the browser may know, in one shape.

    Only the requests this demonstration is following, and only the ones still
    in play. Not a display filter: the session drops out-of-focus work when a
    demonstration starts and never restores it, and a settled request is one
    nothing further can happen to — the quotation went out or the gate declined
    it, and neither the poll nor the rate pass will touch it again. Anything
    removed here remains in the durable store and the audit trail, correlatable
    and unaltered.

    `selected` is deliberately looked up against every request the session
    holds rather than against this list. Approving a quotation settles it, and
    an operator reading the confirmation of what they just sent must not have
    it disappear from under them; it leaves the *desk*, not the record.
    """
    followed = [r for r in session.requests.values() if session.in_demonstration(r.request_id)]
    active = [r for r in followed if not r.is_settled]
    chosen = session.requests.get(selected) if selected else None
    demonstration = session.demonstration
    return {
        "demonstration": {
            "active": demonstration.is_active,
            "started_at": demonstration.started_at.isoformat()
            if demonstration.started_at
            else None,
            "following": len(active),
            "outside_messages": session.outside_demonstration,
        },
        "mode": {
            "badge": "LIVE — REAL GMAIL",
            "banner": SIMULATED_BANNER,
            "notes": {
                "inbound": "Real Gmail, read-only credential",
                "outbound": "Real Gmail, separate send-only credential",
                "extraction": "Live model call",
                "validation": "Real — deterministic business rules",
                "rates": f"{SIMULATED_BANNER} — no provider is contacted",
                "approval": "Human — explicit, named, no default and no timeout",
            },
            "approver_address": session.approver_address,
        },
        "requests": [request_summary(request) for request in active],
        "selected": None if chosen is None else request_detail(session, chosen),
        "audit": audit_json(session.audit.events),
        "poll": {
            "new_messages": session.last_poll_new,
            "skipped_internal": session.skipped_internal,
            "deferred": session.blocked_messages,
            "enquiries": sum(1 for request in active if request.looks_like_an_enquiry),
            "unrecognised": sum(1 for request in active if not request.looks_like_an_enquiry),
            # The mailbox is read by a background thread now, so the page has
            # no click to infer liveness from. These two are how a dashboard
            # that has not moved tells "nothing arrived" from "nothing is
            # running", and the error is a class name — never provider detail.
            "last_checked_at": session.last_poll_at.isoformat() if session.last_poll_at else None,
            "error": session.last_poll_error,
        },
    }
