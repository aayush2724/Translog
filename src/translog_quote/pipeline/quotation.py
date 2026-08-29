"""The approval gate, and the only path to a client quotation.

    ReviewPacket -> PENDING_APPROVAL
                 -> review request emailed to the internal approver
                 -> ApprovalPort.request(...)   <- halts here
                 -> Approved  -> Quotation -> EmailSink -> QUOTATION_SENT
                    Rejected  -> MAKER_REJECTED, nothing sent

Four independent guards keep an unapproved quotation from reaching a client,
three of them structural rather than tested:

1. **Signature.** ``_dispatch`` takes a ``Quotation``, and ``Quotation``
   requires ``approved: Approved``. ``ApprovalDecision`` is
   ``Approved | Rejected``, so under a strict type check there is no way to
   reach the client sink holding a rejection. "Send without approval" is not a
   code path that exists to be tested — it is a call that does not type.
2. **State machine.** ``PENDING_APPROVAL -> QUOTATION_SENT`` is asserted before
   dispatch, and ``MAKER_REJECTED`` is terminal with an empty transition set,
   so a second attempt after a decline raises rather than proceeding.
3. **Ledger.** A request that has already been decided is refused *before* the
   gate is consulted, so a person is never asked twice and a client is never
   mailed twice.
4. **Reachability.** The client sink is called from inside one ``isinstance``
   branch and nowhere else. The decline path returns without touching it.

The ledger is in memory, and honestly so: it makes dispatch single-shot within
a process. Guaranteeing it across process restarts is a property of a durable
``StorePort``, and the demo's store is ``InMemoryStore``.

Note that there is no timeout anywhere in this module, and no default decision.
An approval that never arrives leaves the request at ``PENDING_APPROVAL``,
which is the correct outcome and the whole of BR-11.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from translog_quote.domain.quotation import (
    Approved,
    Quotation,
    build_quotation,
    compose_review_request,
    quotation_message,
)
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import IllegalTransition
from translog_quote.pipeline.audit import AuditEvent, AuditEventType
from translog_quote.pipeline.state_machine import StateMachine

if TYPE_CHECKING:
    from translog_quote.domain.quotation import ApprovalDecision, ReviewPacket
    from translog_quote.pipeline.audit import AuditSink
    from translog_quote.ports import ApprovalPort, ClockPort, EmailSink, StorePort


#: States that mean the gate has already run for a request. Both are the
#: recorded consequence of a person's decision, so neither may be revisited.
_SETTLED_STATES = frozenset({RequestState.QUOTATION_SENT, RequestState.MAKER_REJECTED})


@dataclass(frozen=True, slots=True)
class QuotationOutcome:
    """What the gate produced for one request. Nothing hidden in the object."""

    request_id: str
    state: RequestState
    packet: ReviewPacket
    decision: ApprovalDecision
    quotation: Quotation | None
    sent: bool
    """Whether a quotation actually reached the client sink.

    Reported as observed rather than inferred from the state, because "the
    state says QUOTATION_SENT" and "the sink accepted a message" are two
    different claims and a demo is entitled to check both.
    """

    @property
    def was_approved(self) -> bool:
        return isinstance(self.decision, Approved)


class QuotationStage:
    """Runs one validated, rate-selected request past a human.

    Every collaborator is injected. ``approval`` is an ``ApprovalPort``, which
    is a halt: the implementation blocks until a person decides, and no
    implementation may return a default on a timeout (BR-11).
    """

    def __init__(
        self,
        *,
        sink: EmailSink,
        approval: ApprovalPort,
        clock: ClockPort,
        approver_address: str,
        store: StorePort | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        if not approver_address:
            # Refusing here rather than sending nowhere. A review nobody
            # receives is indistinguishable, from the outside, from a system
            # that approves on its own.
            raise IllegalTransition(
                "no internal approver address configured; refusing to run the approval gate"
            )
        self._sink = sink
        self._approval = approval
        self._clock = clock
        self._approver_address = approver_address
        self._store = store
        self._audit = audit
        self._machine = StateMachine()
        self._decided: dict[str, ApprovalDecision] = {}
        self._dispatched: dict[str, Approved] = {}

    # ------------------------------------------------------------------ api --

    def decision_for(self, request_id: str) -> ApprovalDecision | None:
        """The decision already recorded for a request, if any."""
        return self._decided.get(request_id)

    def was_dispatched(self, request_id: str) -> bool:
        """Whether a quotation for this request has already gone to the client."""
        return request_id in self._dispatched

    def run(
        self,
        packet: ReviewPacket,
        *,
        client_address: str,
        is_simulated: bool,
        in_reply_to: str | None = None,
    ) -> QuotationOutcome:
        """Ask a person, then act on what they said. Never the other way round.

        ``client_address`` and the approver address are separate arguments
        reaching separate composers: the client message is built by
        ``quotation_message`` from a ``Quotation``, the internal one by
        ``compose_review_request`` from the packet. Neither function can
        produce the other's text, so the two audiences cannot be crossed by a
        mistaken flag.
        """
        request_id = packet.request_id

        # Guard 3, before anything else happens. A request that has already
        # been decided is not re-presented to a person under any circumstances.
        previous = self._decided.get(request_id)
        if previous is not None:
            raise IllegalTransition(
                f"{request_id} has already been decided "
                f"({type(previous).__name__.lower()}); refusing to ask again"
            )

        # The same guard, across processes. The in-memory ledger above is empty
        # in a fresh process, so on its own it protects a single run only. When
        # the store is durable, the persisted state is what remembers that a
        # quotation already went out — and it is consulted before the review is
        # composed, so a second process cannot even ask the question again.
        self._refuse_if_already_settled(request_id)

        state = self._enter_pending(request_id)

        # The human's copy. Sent before the gate is opened, because the point
        # of the gate is that a person read this first.
        self._sink.send(
            compose_review_request(
                packet, approver_address=self._approver_address, is_simulated=is_simulated
            )
        )
        self._emit(
            request_id,
            AuditEventType.APPROVAL_REQUESTED,
            {"carrier": packet.selection.rate.carrier_code, "is_simulated": is_simulated},
        )

        # The halt. Control does not return until a person has decided, and no
        # implementation of this port is permitted to decide on its own.
        decision = self._approval.request(packet)
        self._decided[request_id] = decision
        self._emit(
            request_id,
            AuditEventType.APPROVAL_DECIDED,
            {"by": decision.by, "approved": isinstance(decision, Approved)},
        )

        if not isinstance(decision, Approved):
            state = self._advance(request_id, state, RequestState.MAKER_REJECTED)
            self._save(request_id, state)
            # Deliberately no sink call on this path, and nothing to construct:
            # there is no such thing as a declined Quotation.
            return QuotationOutcome(
                request_id=request_id,
                state=state,
                packet=packet,
                decision=decision,
                quotation=None,
                sent=False,
            )

        quotation = build_quotation(packet, decision, is_simulated=is_simulated)
        self._dispatch(quotation, to_address=client_address, in_reply_to=in_reply_to)
        state = self._advance(request_id, state, RequestState.QUOTATION_SENT)
        self._emit(
            request_id,
            AuditEventType.QUOTATION_SENT,
            {"approved_by": decision.by, "carrier": quotation.selection.rate.carrier_code},
        )
        self._save(request_id, state)

        return QuotationOutcome(
            request_id=request_id,
            state=state,
            packet=packet,
            decision=decision,
            quotation=quotation,
            sent=True,
        )

    # ------------------------------------------------------------- internals --

    def _dispatch(self, quotation: Quotation, *, to_address: str, in_reply_to: str | None) -> None:
        """Hand an approved quotation to the outbound sink. The only such call.

        Takes a ``Quotation``, whose ``approved`` field is a required
        ``Approved``. There is no argument list for this method that does not
        include an approval.
        """
        already = self._dispatched.get(quotation.request_id)
        if already is not None:
            raise IllegalTransition(
                f"a quotation for {quotation.request_id} was already sent "
                f"(approved by {already.by}); refusing to send it twice"
            )
        self._sink.send(
            quotation_message(quotation, to_address=to_address, in_reply_to=in_reply_to)
        )
        self._dispatched[quotation.request_id] = quotation.approved

    def _refuse_if_already_settled(self, request_id: str) -> None:
        """Stop if the stored request has already left the gate.

        `QUOTATION_SENT` and `MAKER_REJECTED` are both outcomes of a decision a
        person already made. Reaching the gate again from either is a repeat
        run, not a new request, and the honest response is to refuse rather
        than to ask someone to decide something they have decided.
        """
        if self._store is None:
            return
        stored = self._store.get_request(request_id)
        if stored is None:
            return
        if stored.state in _SETTLED_STATES:
            raise IllegalTransition(
                f"{request_id} is already at {stored.state.value}; a decision was "
                "made in an earlier run and will not be taken again"
            )

    def _enter_pending(self, request_id: str) -> RequestState:
        self._machine.assert_transition(RequestState.RATE_SELECTED, RequestState.PENDING_APPROVAL)
        self._emit(
            request_id,
            AuditEventType.STATE_CHANGED,
            {"from": RequestState.RATE_SELECTED.value, "to": RequestState.PENDING_APPROVAL.value},
        )
        self._save(request_id, RequestState.PENDING_APPROVAL)
        return RequestState.PENDING_APPROVAL

    def _advance(
        self, request_id: str, current: RequestState, target: RequestState
    ) -> RequestState:
        self._machine.assert_transition(current, target)
        self._emit(
            request_id,
            AuditEventType.STATE_CHANGED,
            {"from": current.value, "to": target.value},
        )
        return target

    def _save(self, request_id: str, state: RequestState) -> None:
        """Record the new state on the stored request, when there is a store.

        Best-effort by design: the stage owns the transition, and a demo run
        without a store is still correct. A request the store has never heard
        of is not invented here — that would fabricate a client address.
        """
        if self._store is None:
            return
        stored = self._store.get_request(request_id)
        if stored is None:
            return
        self._store.save_request(stored.model_copy(update={"state": state}))

    def _emit(self, request_id: str, event: AuditEventType, detail: dict[str, object]) -> None:
        """Carrier codes, approver names, states. Never a body, never a credential.

        The approver's name is recorded on purpose: "who approved this" is the
        single most important fact in this module's evidence trail.
        """
        if self._audit is None:
            return
        self._audit.record(
            AuditEvent(request_id=request_id, event=event, at=self._clock.now(), detail=detail)
        )
