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

from datetime import UTC, datetime
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
    from translog_quote.interface.web.multi_account_session import MultiAccountSession
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
    RequestState.CLOSED_NO_RATES: ("NO RATES — CLIENT NOTIFIED", "gray"),
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
        # The adapter's own per-rate identity, so the browser can mark exactly
        # the selected card. Matching on carrier_code alone flagged every rate
        # from the winning carrier as SELECTED; source_ref is unique per rate.
        "source_ref": rate.source_ref,
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


def _candidate_scope(*, returned: int, simulated: bool, completeness: str | None) -> str:
    """One accurate sentence about how wide the "fastest eligible" search was.

    Never claims the selected rate is globally fastest: it is the fastest of the
    candidates the provider returned, which for WebCargo is a price-truncated
    "lowest rates" set. A simulated run says so plainly; a real run reports the
    returned count and, when the provider stated no total, says completeness is
    unconfirmed rather than implying it. Nothing here is invented — the count is
    the count, and the absence of a provider total is reported as an absence.
    """
    plural = "" if returned == 1 else "s"
    if simulated:
        return (
            f"Fastest eligible among {returned} simulated rate{plural} — "
            "demonstration data, not a live WebCargo result."
        )
    scope = (
        f"Fastest eligible among the {returned} rate{plural} WebCargo returned — "
        "not necessarily the fastest that exists."
    )
    if not completeness:
        scope += " WebCargo stated no total, so completeness is unconfirmed."
    return scope


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
        # The day this rate departs. The search aggregates every returned date
        # tab (AMB-8: all-dates candidate set), so the winner may leave on a
        # different day than the searched shipment date — the provider's own
        # date label is shown when it gave one, falling back to the searched
        # date. Never reformatted, never guessed.
        "departure_date": rate.departure_date_label or outcome.query.date.isoformat(),
        "searched_date": outcome.query.date.isoformat(),
        "reason": packet.selection.reason,
        # The scope of the optimisation, so "fastest" is never read as global.
        # `candidate_scope` is the plain-language sentence the card shows;
        # `completeness` is the provider's own verbatim note, or null when none
        # was given (which the sentence then states outright).
        "candidate_scope": _candidate_scope(
            returned=outcome.returned,
            simulated=outcome.uses_mock_data,
            completeness=outcome.completeness,
        ),
        "completeness": outcome.completeness,
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

    if request.awaiting_clarification_approval and not any(
        row["key"] == "clarification_sent" and row["state"] == "current" for row in rows
    ):
        # A second or later clarification round, and the template has no row
        # for one: it carries a single clarification row and a single reply
        # row, both pinned to the first occurrence. Once round one has been
        # sent and answered, both read as done and the next template row —
        # rate search — inherits "current". The screen then reports the system
        # as pricing the shipment while it is in fact parked on a person, which
        # is the one thing a desk must never state backwards.
        #
        # Only the marker moves. Unlike manual review the later rows are not
        # dropped, because they will still happen: this request resumes the
        # moment the draft is approved and the client answers.
        for row in rows:
            if row["state"] == "current":
                row["state"] = "pending"
                row["note"] = None
                row["waiting_on"] = None
        after_last_done = max(
            (index for index, row in enumerate(rows) if row["state"] == "done"), default=-1
        )
        rows.insert(
            after_last_done + 1,
            {
                "key": "clarification_pending",
                "label": "Clarification awaiting approval",
                "state": "current",
                "at": None,
                "note": _WAITING_NOTES["clarification_sent"],
                "waiting_on": _WAITING_ON["clarification_sent"],
            },
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

    if request.state in (RequestState.CLOSED_NO_RATES, RequestState.NO_ELIGIBLE_RATE):
        # The search ran and found no usable rate. Same failure mode the manual-
        # review block fixes: without this the first not-done row rendered as
        # "Rate search — Pending" (or "Rate selected — Pending"), reading as
        # stuck when the request is in fact done. Drop the rows that will not
        # happen and end on the true, terminal outcome.
        notified = request.state is RequestState.CLOSED_NO_RATES
        rows = [row for row in rows if row["state"] == "done"]
        rows.append(
            {
                "key": "no_rates",
                "label": "No eligible rate — client notified"
                if notified
                else "No eligible rate found",
                "state": "done" if notified else "current",
                "at": None,
                "note": None
                if notified
                else "No rate could be sourced; a client could not be notified — needs a look",
                "waiting_on": None if notified else "operator",
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


def _worker_notice(session: LiveSession, request: LiveRequest) -> str | None:
    """A note for a *pending* browser search when the worker is not running.

    Only for a request whose search is queued (`rate_search_pending`), and only
    for the states that warrant a claim: `offline` (no worker draining the
    queue) and `needs_login` (a worker stopped, awaiting operator sign-in).
    `online` needs no note and `unknown` must never claim 'offline', so both add
    nothing. The liveness is the value cached on the last poll — no Redis here."""
    from translog_quote.interface.jobs import WORKER_NEEDS_LOGIN, WORKER_OFFLINE

    if not request.rate_search_pending:
        return None
    if session.worker_status == WORKER_OFFLINE:
        return "Rate-search worker offline — searches are queued, not running."
    if session.worker_status == WORKER_NEEDS_LOGIN:
        return (
            "Rate-search worker needs sign-in — searches are queued until an "
            "operator re-authenticates."
        )
    return None


def _goods_type_hold(session: LiveSession, request: LiveRequest) -> Json | None:
    """When a request is held for an operator Goods Type pick: the client's own
    cargo facts to judge by (commodity, cargo type, chemical status) and the
    catalog to pick from — or a note that the catalog is not configured."""
    if not request.awaiting_goods_type:
        return None
    options, configured = session.goods_type_hold_options()
    return {
        "commodity": request.record.commodity,
        "cargo_type": request.record.cargo_type,
        "is_chemical": request.record.is_chemical,
        "msds": _msds_note(request.record.msds_attached),
        "catalog": options,
        "catalog_configured": configured,
    }


def _msds_note(msds_attached: bool | None) -> str | None:
    """The MSDS status in words for the operator judging a Goods Type hold.

    ``False`` means the client explicitly answered "no MSDS" (whether the model
    said so as ``STATED False`` or as a ``DENIED`` fix 1 carried to ``False``) —
    the operator sees the shipment is a chemical with no MSDS on file."""
    if msds_attached is True:
        return "attached"
    if msds_attached is False:
        return "not available (client stated)"
    return None


def _first_seen_at(session: LiveSession, request: LiveRequest) -> str | None:
    """The earliest time this request is known to have been seen: the live
    enquiry's receipt time, or — for a request restored from the store, which
    carries no enquiry email — the earliest persisted audit event for it."""
    if request.enquiry is not None:
        return request.enquiry.received_at.isoformat()
    times = [event.at for event in session.audit.events if event.request_id == request.request_id]
    return min(times).isoformat() if times else None


def request_summary(session: LiveSession, request: LiveRequest) -> Json:
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
        # When the desk first saw this request. Unlike ``received_at`` (the live
        # enquiry email, absent on a request restored from the store) this falls
        # back to the earliest audit event, so a restored request still has an
        # age the operations healthcheck can measure staleness against.
        "first_seen_at": _first_seen_at(session, request),
        "status": status_json(request),
        "awaiting_clarification": request.awaiting_clarification_approval,
        # Why this request has no rates, when it has none. Reported rather
        # than hidden: a request stuck before pricing looks idle otherwise.
        "rate_failure": request.rate_failure,
        # A queued WebCargo search is in flight (browser mode): the panel shows
        # "Searching…" rather than an empty rate section that reads as a stall.
        "rate_search_pending": request.rate_search_pending,
        # When that search is queued but no worker is draining it, say so instead
        # of leaving the request "pending" forever. None unless it applies.
        "worker_notice": _worker_notice(session, request),
        # Held for an operator to pick a WebCargo Goods Type. None unless it applies.
        "goods_type_hold": _goods_type_hold(session, request),
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
        "rate_search_pending": request.rate_search_pending,
        "worker_notice": _worker_notice(session, request),
        "goods_type_hold": _goods_type_hold(session, request),
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


#: A floor for a request that carries no live email (one restored from the store
#: on startup), so it sorts to the end of the newest-first active list rather than
#: breaking the comparison against tz-aware email timestamps.
_OLDEST_ACTIVITY = datetime.min.replace(tzinfo=UTC)


def _activity_at(request: LiveRequest) -> datetime:
    """When this request last did something — the key the active list sorts on.

    The most recent email the desk holds for it (a reply lifts it above an older,
    untouched enquiry), then its enquiry, and finally the floor for a restored
    request with no live email. This is display ordering only; it reads state,
    changes none."""
    for email in (request.latest_email, request.reply, request.enquiry):
        if email is not None:
            return email.received_at
    return _OLDEST_ACTIVITY


def snapshot(session: LiveSession | MultiAccountSession, *, selected: str | None = None) -> Json:
    """Everything the browser may know, in one shape.

    A :class:`MultiAccountSession` is rendered by :func:`_multi_snapshot`, which
    walks the owning session of each request so per-account approver,
    demonstration and audit are correct, and tags every row with its account.

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
    from translog_quote.interface.web.multi_account_session import MultiAccountSession

    if isinstance(session, MultiAccountSession):
        return _multi_snapshot(session, selected=selected)

    # Operations mode follows the store, not the demonstration's request_ids
    # (a past restart may have reset them): every restored request is in view,
    # terminal ones as history. Demonstration mode keeps the focuses filter.
    if session.operations_mode:
        followed = list(session.requests.values())
    else:
        followed = [r for r in session.requests.values() if session.in_demonstration(r.request_id)]
    # Two groups only: Active (anything still in play, any age) and History
    # (terminal/completed — a quotation sent, a decline, a no-rates close, or a
    # request restored from the store already terminal). A live-settled request
    # therefore moves into History rather than vanishing. Active leads with the
    # newest activity. No age cutoff: operations follows the whole store.
    active = sorted(
        (r for r in followed if not r.is_settled and not r.history),
        key=_activity_at,
        reverse=True,
    )
    history = [r for r in followed if r.is_settled or r.history]
    chosen = session.requests.get(selected) if selected else None
    demonstration = session.demonstration
    return {
        "demonstration": {
            "active": demonstration.is_active,
            "startup_mode": "operations" if session.operations_mode else "demonstration",
            "started_at": demonstration.started_at.isoformat()
            if demonstration.started_at
            else None,
            "last_poll_watermark": demonstration.last_poll_watermark.isoformat()
            if demonstration.last_poll_watermark
            else None,
            "following": len(active),
            "history": len(history),
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
        "requests": [request_summary(session, request) for request in active],
        # Terminal requests, restored from the store. Sent to the browser but
        # hidden behind a filter by default, so the desk leads with live work
        # while a settled request stays inspectable rather than gone.
        "history": [request_summary(session, request) for request in history],
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


def _multi_snapshot(session: MultiAccountSession, *, selected: str | None = None) -> Json:
    """The unified snapshot across every account's session.

    Each request is rendered by its OWNING session, so its approver,
    demonstration membership and audit timeline are that account's, and each row
    is tagged with its ``account``. The top-level blocks aggregate across
    accounts; the audit is the merged, time-ordered trail."""
    active_pairs: list[tuple[datetime, Json]] = []
    history_rows: list[Json] = []
    for account_id, sess in session.sessions.items():
        if sess.operations_mode:
            followed = list(sess.requests.values())
        else:
            followed = [r for r in sess.requests.values() if sess.in_demonstration(r.request_id)]
        for request in followed:
            row = request_summary(sess, request)
            row["account"] = account_id
            # Two groups only: History is terminal/completed (settled, or restored
            # already terminal); Active is everything else, any age. A live-settled
            # request moves to History rather than vanishing.
            if request.history or request.is_settled:
                history_rows.append(row)
            else:
                active_pairs.append((_activity_at(request), row))
    # Newest activity first, across all accounts.
    active_rows = [row for _, row in sorted(active_pairs, key=lambda pair: pair[0], reverse=True)]

    chosen_detail: Json | None = None
    if selected:
        for account_id, sess in session.sessions.items():
            chosen = sess.requests.get(selected)
            if chosen is not None:
                chosen_detail = request_detail(sess, chosen)
                chosen_detail["account"] = account_id
                break

    started_ats = [
        s.demonstration.started_at
        for s in session.sessions.values()
        if s.demonstration.started_at is not None
    ]
    watermarks = [
        s.demonstration.last_poll_watermark
        for s in session.sessions.values()
        if s.demonstration.last_poll_watermark is not None
    ]
    started_at = min(started_ats) if started_ats else None
    watermark = min(watermarks) if watermarks else None
    poll_error = next(
        (s.last_poll_error for s in session.sessions.values() if s.last_poll_error),
        session.last_poll_error,
    )

    return {
        "demonstration": {
            "active": any(s.demonstration.is_active for s in session.sessions.values()),
            "startup_mode": "operations" if session.operations_mode else "demonstration",
            "started_at": started_at.isoformat() if started_at else None,
            "last_poll_watermark": watermark.isoformat() if watermark else None,
            "following": len(active_rows),
            "history": len(history_rows),
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
        "requests": active_rows,
        "history": history_rows,
        "selected": chosen_detail,
        "audit": audit_json(session.audit.events),
        "poll": {
            "new_messages": session.last_poll_new,
            "skipped_internal": session.skipped_internal,
            "deferred": session.blocked_messages,
            "enquiries": sum(1 for row in active_rows if row["is_enquiry"]),
            "unrecognised": sum(1 for row in active_rows if not row["is_enquiry"]),
            "last_checked_at": session.last_poll_at.isoformat() if session.last_poll_at else None,
            "error": poll_error,
        },
    }
