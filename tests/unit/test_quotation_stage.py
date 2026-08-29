"""The approval gate stage — the only path from a selected rate to a client.

These are the safety tests. Each one names a way an unapproved, declined, or
duplicate quotation could reach a client, and asserts that it cannot.
"""

from __future__ import annotations

import datetime

import pytest

from translog_quote.adapters.clock import FixedClock
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.store import InMemoryStore
from translog_quote.adapters.webcargo import DemoRateProvider
from translog_quote.domain.quotation import (
    INTERNAL_SUBJECT_PREFIX,
    Approved,
    Rejected,
    ReviewPacket,
)
from translog_quote.domain.rates import FASTEST_ELIGIBLE, filter_rates, select_rate
from translog_quote.domain.shipment import (
    CargoDimensions,
    DeliveryType,
    RequestSource,
    ShipmentRecord,
)
from translog_quote.domain.validation import validate_shipment
from translog_quote.domain.workflow import QuotationRequest, RequestState
from translog_quote.errors import IllegalTransition
from translog_quote.pipeline import QuotationStage, build_query
from translog_quote.pipeline.audit import AuditEvent, AuditEventType

CLOCK = FixedClock()
WHEN = datetime.date(2026, 9, 2)
REQUEST_ID = "R-TEST-001"
CLIENT = "client@example.com"
APPROVER_MAILBOX = "approvals@translog.example"
APPROVER = "ops.manager@translog.example"

#: The reference shipment from docs/architecture.md §13. Chosen over a simpler
#: one because with `CARGO_IS_LIQUID` it drives all three exclusion reasons —
#: no price, no transit, and a carrier restriction — so a packet built from it
#: actually has excluded rates and runners-up to keep away from the client.
RECORD = ShipmentRecord(
    request_id=REQUEST_ID,
    source=RequestSource.EMAIL,
    origin="Ahmedabad",
    destination="Bahrain",
    weight_kg=500.0,
    dimensions_in=CargoDimensions(length=34, width=24, height=6),
    commodity="POLYISOBUTYLENE ADDITIVE",
    cargo_type="Non Haz",
    is_chemical=True,
    msds_attached=True,
    pcs=20,
    delivery_type=DeliveryType.AIRPORT,
)

#: Stated, never derived. The canonical record cannot express physical form
#: (AMB-3), so the test states it exactly as a demo operator would.
CARGO_IS_LIQUID = True


def packet_for(record: ShipmentRecord = RECORD) -> ReviewPacket:
    """A real review packet: real simulated rates, real filter, real selection."""
    result = DemoRateProvider().search(build_query(record, on_date=WHEN))
    filtered = filter_rates(result.rates, cargo_is_liquid=CARGO_IS_LIQUID)
    selection = select_rate(filtered.eligible, FASTEST_ELIGIBLE)
    assert selection is not None
    return ReviewPacket(
        request_id=record.request_id,
        record=record,
        validation=validate_shipment(record),
        clarification_sent=True,
        rates=filtered,
        selection=selection,
    )


class StubApproval:
    """An `ApprovalPort` that answers from a script and counts how often asked."""

    def __init__(self, *decisions: Approved | Rejected) -> None:
        self._decisions = list(decisions)
        self.asked = 0

    def request(self, review: ReviewPacket) -> Approved | Rejected:
        self.asked += 1
        return self._decisions.pop(0)


class CollectingAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)

    def types(self) -> list[AuditEventType]:
        return [e.event for e in self.events]


def approved() -> Approved:
    return Approved(by=APPROVER, at=CLOCK.now())


def rejected(reason: str = "price too high") -> Rejected:
    return Rejected(by=APPROVER, at=CLOCK.now(), reason=reason)


def stage_with(
    approval: StubApproval,
    *,
    sink: CollectingEmailSink | None = None,
    store: InMemoryStore | None = None,
    audit: CollectingAudit | None = None,
) -> tuple[QuotationStage, CollectingEmailSink]:
    outbox = sink or CollectingEmailSink()
    return (
        QuotationStage(
            sink=outbox,
            approval=approval,
            clock=CLOCK,
            approver_address=APPROVER_MAILBOX,
            store=store,
            audit=audit,
        ),
        outbox,
    )


def run(stage: QuotationStage, packet: ReviewPacket | None = None) -> object:
    return stage.run(packet or packet_for(), client_address=CLIENT, is_simulated=True)


# --- the approved path ----------------------------------------------------------


def test_an_approved_quotation_reaches_the_client() -> None:
    stage, outbox = stage_with(StubApproval(approved()))

    outcome = run(stage)

    assert outcome.sent is True  # type: ignore[attr-defined]
    assert outcome.state is RequestState.QUOTATION_SENT  # type: ignore[attr-defined]
    to_client = [m for m in outbox.sent if m.to_address == CLIENT]
    assert len(to_client) == 1


def test_the_approver_is_recorded_on_the_quotation_itself() -> None:
    """Not only in the audit trail. The `Quotation` carries the `Approved`
    that authorised it, so evidence of who approved travels with the artefact."""
    stage, _ = stage_with(StubApproval(approved()))

    outcome = run(stage)

    assert outcome.quotation is not None  # type: ignore[attr-defined]
    assert outcome.quotation.approved.by == APPROVER  # type: ignore[attr-defined]


def test_the_review_goes_to_the_approver_and_the_quotation_to_the_client() -> None:
    """Two messages, two audiences, and they are not the same message."""
    stage, outbox = stage_with(StubApproval(approved()))

    run(stage)

    assert [m.to_address for m in outbox.sent] == [APPROVER_MAILBOX, CLIENT]


def test_the_client_never_sees_the_internal_review_content() -> None:
    """The review packet lists excluded carriers, exclusion reasons and
    runners-up. That is internal commercial detail, and BR-10 says the client
    sees exactly one rate."""
    stage, outbox = stage_with(StubApproval(approved()))
    packet = packet_for()

    run(stage, packet)

    to_client = next(m for m in outbox.sent if m.to_address == CLIENT)
    assert INTERNAL_SUBJECT_PREFIX not in to_client.subject
    for excluded in packet.rates.excluded:
        assert excluded.rate.carrier_name not in to_client.body_text
    for runner_up in packet.selection.runners_up:
        assert runner_up.carrier_name not in to_client.body_text


def test_the_review_is_sent_before_the_gate_is_consulted() -> None:
    """The point of the gate is that a person read the packet first."""
    sent_when_asked: list[int] = []

    class Watching(StubApproval):
        def request(self, review: ReviewPacket) -> Approved | Rejected:
            sent_when_asked.append(len(outbox.sent))
            return super().request(review)

    approval = Watching(approved())
    outbox = CollectingEmailSink()
    stage, _ = stage_with(approval, sink=outbox)

    run(stage)

    assert sent_when_asked == [1]  # the review had already gone out


# --- the declined path ----------------------------------------------------------


def test_a_declined_quotation_is_never_sent() -> None:
    stage, outbox = stage_with(StubApproval(rejected()))

    outcome = run(stage)

    assert outcome.sent is False  # type: ignore[attr-defined]
    assert outcome.quotation is None  # type: ignore[attr-defined]
    assert [m.to_address for m in outbox.sent] == [APPROVER_MAILBOX]


def test_a_decline_lands_in_the_terminal_maker_rejected_state() -> None:
    stage, _ = stage_with(StubApproval(rejected()))

    outcome = run(stage)

    assert outcome.state is RequestState.MAKER_REJECTED  # type: ignore[attr-defined]
    assert outcome.was_approved is False  # type: ignore[attr-defined]


def test_a_decline_records_who_declined_and_why() -> None:
    audit = CollectingAudit()
    stage, _ = stage_with(StubApproval(rejected("carrier not acceptable")), audit=audit)

    outcome = run(stage)

    assert outcome.decision.reason == "carrier not acceptable"  # type: ignore[attr-defined]
    decided = next(e for e in audit.events if e.event is AuditEventType.APPROVAL_DECIDED)
    assert decided.detail == {"by": APPROVER, "approved": False}


# --- duplicates -----------------------------------------------------------------


def test_a_request_already_approved_cannot_be_run_again() -> None:
    """Guard against a double send from a repeated call — the operator hitting
    the command twice, or a retry loop above this stage."""
    stage, outbox = stage_with(StubApproval(approved(), approved()))
    packet = packet_for()
    run(stage, packet)

    with pytest.raises(IllegalTransition, match="already been decided"):
        run(stage, packet)

    assert len([m for m in outbox.sent if m.to_address == CLIENT]) == 1


def test_a_request_already_declined_cannot_be_run_again() -> None:
    """A decline is terminal. Asking again would be a second chance at a
    decision that has already been made."""
    stage, outbox = stage_with(StubApproval(rejected(), approved()))
    packet = packet_for()
    run(stage, packet)

    with pytest.raises(IllegalTransition, match="already been decided"):
        run(stage, packet)

    assert [m.to_address for m in outbox.sent] == [APPROVER_MAILBOX]


def test_a_second_run_never_reaches_the_human_at_all() -> None:
    """The duplicate guard fires before the gate, so a person is not asked to
    decide something they have already decided."""
    approval = StubApproval(approved(), approved())
    stage, _ = stage_with(approval)
    packet = packet_for()
    run(stage, packet)

    with pytest.raises(IllegalTransition):
        run(stage, packet)

    assert approval.asked == 1


def test_the_stage_reports_what_it_has_already_dispatched() -> None:
    stage, _ = stage_with(StubApproval(approved()))
    assert stage.was_dispatched(REQUEST_ID) is False

    run(stage)

    assert stage.was_dispatched(REQUEST_ID) is True
    assert isinstance(stage.decision_for(REQUEST_ID), Approved)


# --- configuration refusals -----------------------------------------------------


def test_the_stage_refuses_to_build_without_an_approver_address() -> None:
    """A review packet that goes nowhere is indistinguishable, from outside,
    from a system that approved on its own."""
    with pytest.raises(IllegalTransition, match="approver address"):
        QuotationStage(
            sink=CollectingEmailSink(),
            approval=StubApproval(approved()),
            clock=CLOCK,
            approver_address="",
        )


# --- state and evidence ---------------------------------------------------------


def test_the_stored_request_state_follows_the_decision() -> None:
    store = InMemoryStore()
    store.save_request(
        QuotationRequest(
            request_id=REQUEST_ID,
            state=RequestState.RATE_SELECTED,
            record=RECORD,
            client_address=CLIENT,
        )
    )
    stage, _ = stage_with(StubApproval(approved()), store=store)

    run(stage)

    stored = store.get_request(REQUEST_ID)
    assert stored is not None
    assert stored.state is RequestState.QUOTATION_SENT


def test_the_audit_trail_shows_the_gate_in_order() -> None:
    audit = CollectingAudit()
    stage, _ = stage_with(StubApproval(approved()), audit=audit)

    run(stage)

    types = audit.types()
    assert types.index(AuditEventType.APPROVAL_REQUESTED) < types.index(
        AuditEventType.APPROVAL_DECIDED
    )
    assert types.index(AuditEventType.APPROVAL_DECIDED) < types.index(AuditEventType.QUOTATION_SENT)


def test_a_decline_emits_no_quotation_sent_event() -> None:
    audit = CollectingAudit()
    stage, _ = stage_with(StubApproval(rejected()), audit=audit)

    run(stage)

    assert AuditEventType.QUOTATION_SENT not in audit.types()


def test_the_audit_trail_carries_no_message_body() -> None:
    """Evidence that the workflow ran as designed, not a copy of the
    correspondence."""
    audit = CollectingAudit()
    stage, _ = stage_with(StubApproval(approved()), audit=audit)

    run(stage)

    for event in audit.events:
        rendered = str(event.detail)
        assert "Dear Sir" not in rendered
        assert CLIENT not in rendered
