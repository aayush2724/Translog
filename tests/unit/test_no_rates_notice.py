"""The "no eligible rate" client notice and the CLOSED_NO_RATES close-out.

When a search runs and finds no usable rate, the client is emailed a plain
"no rates" notice and the request closes at CLOSED_NO_RATES rather than reading
as stuck at rate search. These pin the composer, the state model and the
timeline; the live send / idempotency / durable-persist path lives in
``test_service_eligibility`` where the live-session harness already is.
"""

from __future__ import annotations

import pytest

from translog_quote.domain.quotation import (
    compose_no_rates_body,
    compose_no_rates_subject,
    no_rates_message,
)
from translog_quote.domain.shipment import CargoDimensions, RequestSource, ShipmentRecord
from translog_quote.domain.validation import validate_shipment
from translog_quote.domain.workflow import TERMINAL_STATES, TRANSITIONS, RequestState
from translog_quote.errors import IllegalTransition
from translog_quote.interface.web.live_serialize import status_json, timeline_json
from translog_quote.interface.web.live_session import LiveRequest
from translog_quote.pipeline import StateMachine


def _record() -> ShipmentRecord:
    return ShipmentRecord(
        request_id="R-1",
        source=RequestSource.EMAIL,
        origin="Kolkata, India",
        destination="Singapore",
        weight_kg=300.0,
        dimensions_in=CargoDimensions(length=30, width=30, height=30),
        commodity="industrial solvent",
        pcs=4,
    )


# --- the composer -----------------------------------------------------------


def test_no_rates_subject_names_the_lane_and_reference() -> None:
    subject = compose_no_rates_subject(_record(), reference="R-1")
    assert "R-1" in subject
    assert "Kolkata, India to Singapore" in subject
    assert "unable to quote" in subject.lower()


def test_no_rates_body_apologises_and_invents_no_figures() -> None:
    body = compose_no_rates_body(_record(), reference="R-1")
    assert "unable to source" in body.lower()
    assert "Kolkata, India" in body and "Singapore" in body
    # Nothing to quote, so no price/currency figure is invented.
    for token in ("Rs", "INR", "USD", "$"):
        assert token not in body
    # It invites a follow-up rather than reading as a dead end.
    assert "alternative shipment date" in body.lower()


def test_no_rates_message_is_addressed_and_threaded() -> None:
    msg = no_rates_message(
        _record(), reference="R-1", to_address="client@example.com", in_reply_to="<m1>"
    )
    assert msg.to_address == "client@example.com"
    assert msg.in_reply_to == "<m1>"
    assert msg.subject == compose_no_rates_subject(_record(), reference="R-1")


# --- the state model --------------------------------------------------------


def test_no_eligible_rate_exits_only_to_closed_no_rates() -> None:
    machine = StateMachine()
    assert machine.can_transition(RequestState.NO_ELIGIBLE_RATE, RequestState.CLOSED_NO_RATES)
    # It is no longer terminal — a search that found nothing is closed out, not
    # left as a dead end.
    assert RequestState.NO_ELIGIBLE_RATE not in TERMINAL_STATES


def test_closed_no_rates_is_terminal() -> None:
    assert RequestState.CLOSED_NO_RATES in TERMINAL_STATES
    assert TRANSITIONS[RequestState.CLOSED_NO_RATES] == frozenset()
    with pytest.raises(IllegalTransition):
        StateMachine().assert_transition(RequestState.CLOSED_NO_RATES, RequestState.VALIDATED)


# --- the timeline no longer reads as stuck ----------------------------------


def _live(state: RequestState) -> LiveRequest:
    record = _record()
    return LiveRequest(
        request_id="R-1",
        client_address="client@example.com",
        state=state,
        record=record,
        validation=validate_shipment(record),
    )


def test_closed_no_rates_timeline_ends_on_the_notice_not_pending_rate_search() -> None:
    rows = timeline_json(_live(RequestState.CLOSED_NO_RATES), [])
    # None of the downstream steps linger as pending/current — that is the
    # "stuck at rate search" symptom this fixes.
    assert all(
        row["key"] not in ("rate_search", "rate_selected", "approval_decided", "quotation_sent")
        for row in rows
    )
    assert not any(row["state"] == "current" and row["note"] == "Pending" for row in rows)
    last = rows[-1]
    assert last["key"] == "no_rates"
    assert last["state"] == "done"
    assert "client notified" in last["label"].lower()
    assert status_json(_live(RequestState.CLOSED_NO_RATES))["label"] == "NO RATES — CLIENT NOTIFIED"


def test_unnotified_no_eligible_rate_shows_a_needs_a_look_row() -> None:
    # The rare "search found nothing but no client to notify" case stays visible
    # for an operator rather than reading as stuck at rate search.
    rows = timeline_json(_live(RequestState.NO_ELIGIBLE_RATE), [])
    last = rows[-1]
    assert last["key"] == "no_rates"
    assert last["state"] == "current"
    assert last["waiting_on"] == "operator"
