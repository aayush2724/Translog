"""The clarification loop: one client message in, one decision out.

    RawEmail -> ExtractionPort -> ExtractionResult -> ExtractedFields
             -> merge into the existing ShipmentRecord
             -> validate
             -> identify what is still unresolved
             -> ask, finish, or hand to a person

Called once per inbound client message. It holds no loop of its own: a thread
that needs three rounds is three calls, which is what lets a real mailbox, a
fixture, or a test drive it identically.

The division this module exists to protect: **the model reports what the client
said; deterministic code decides whether that is enough.** No model is consulted
about completeness, and none can be — the only port this class calls is
`ExtractionPort.extract_shipment`, which takes text and returns fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from translog_quote.domain.clarification import (
    UnresolvedAnalysis,
    compose_clarification,
    identify_unresolved,
)
from translog_quote.domain.email import OutboundMessage
from translog_quote.domain.extraction import FieldStatus, to_extracted_fields
from translog_quote.domain.quotation import Approved
from translog_quote.domain.shipment import RequestSource, ShipmentRecord, merge_shipment
from translog_quote.domain.validation import validate_shipment
from translog_quote.domain.workflow import QuotationRequest, RequestState
from translog_quote.errors import IllegalTransition
from translog_quote.pipeline.audit import AuditEvent, AuditEventType
from translog_quote.pipeline.state_machine import StateMachine

if TYPE_CHECKING:
    from translog_quote.domain.clarification import ClarificationMessage
    from translog_quote.domain.email import RawEmail
    from translog_quote.domain.extraction import ExtractionResult
    from translog_quote.domain.shipment import MergeResult
    from translog_quote.domain.validation import ValidationResult
    from translog_quote.pipeline.audit import AuditSink
    from translog_quote.ports import ClockPort, EmailSink, ExtractionPort, StorePort

DEFAULT_MAX_ROUNDS = 3


@dataclass(frozen=True, slots=True)
class _PendingDraft:
    """A clarification written but not released, and where it would go."""

    message: ClarificationMessage
    to_address: str
    subject: str
    in_reply_to: str


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """Everything one inbound message produced. Nothing hidden in the object."""

    request_id: str
    state: RequestState
    record: ShipmentRecord
    extraction: ExtractionResult
    merge: MergeResult
    validation: ValidationResult
    analysis: UnresolvedAnalysis
    clarification: ClarificationMessage | None
    round_number: int

    escalation_notes: tuple[str, ...] = ()
    """The model's own explanation of why a reply's answer could not be used,
    for each field that sent this request to manual review. Shown to the
    operator — an escalation nobody can see the reason for is just a stall."""

    @property
    def is_complete(self) -> bool:
        return self.state is RequestState.VALIDATED

    @property
    def awaiting_approval(self) -> bool:
        """A draft exists and is waiting on a person. Nothing has been sent."""
        return self.clarification is not None and self.state is RequestState.NEEDS_INFO

    @property
    def asked_for_more(self) -> bool:
        """A clarification was drafted this turn — sent or not."""
        return self.clarification is not None

    @property
    def needs_a_person(self) -> bool:
        """Handed over, or stuck in a way that only a person can settle.

        `is_stuck` is included because the approved transition table has no
        EXTRACTED -> MANUAL_REVIEW edge, so a shipment blocked by an explicit
        client denial cannot currently *move* to manual review. It reports the
        condition instead of pretending otherwise.
        """
        return self.state is RequestState.MANUAL_REVIEW or self.analysis.is_stuck


class ClarificationWorkflow:
    """Drives one request through as many clarification rounds as it needs."""

    def __init__(
        self,
        *,
        extractor: ExtractionPort,
        sink: EmailSink,
        store: StorePort,
        clock: ClockPort,
        audit: AuditSink | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ) -> None:
        self._extractor = extractor
        self._sink = sink
        self._store = store
        self._clock = clock
        self._audit = audit
        self._machine = StateMachine()
        self._max_rounds = max_rounds
        self._rounds: dict[str, int] = {}
        self._pending: dict[str, _PendingDraft] = {}

    # ------------------------------------------------------------------ api --

    def handle(self, request_id: str, email: RawEmail) -> TurnOutcome:
        """Process one client message for one request.

        The first call for a ``request_id`` starts the shipment; every later
        call merges into what is already known. The reply is never treated as
        the whole shipment — a client who writes "20 bags, non-hazardous" has
        not retracted their origin.
        """
        self._emit(request_id, AuditEventType.EMAIL_RECEIVED, {"message_id": email.message_id})

        existing = self._store.get_request(request_id)
        record = existing.record if existing else self._blank(request_id)
        state = existing.state if existing else RequestState.RECEIVED

        # A thread that has not converged after this many asks will not converge
        # by asking again. Decided here, before the turn advances, because the
        # approved table allows CLARIFICATION_SENT -> MANUAL_REVIEW but not
        # EXTRACTED -> MANUAL_REVIEW: handing over has to happen from the state
        # the thread is actually in. The message is still extracted and recorded
        # below, so whoever takes over can see what it said.
        abandoned = (
            state is RequestState.CLARIFICATION_SENT
            and self._rounds.get(request_id, 0) >= self._max_rounds
        )
        if abandoned:
            state = self._advance(request_id, state, RequestState.MANUAL_REVIEW)

        # --- the model's only involvement -------------------------------------
        extraction = self._extractor.extract_shipment(email.body_text)
        self._emit(
            request_id,
            AuditEventType.EXTRACTION_CALLED,
            {"stated_fields": len(extraction.fields_by_status(FieldStatus.STATED))},
        )
        # --- everything below is deterministic --------------------------------
        merge = merge_shipment(record, to_extracted_fields(extraction))

        # A reply whose answer could not be used will not be fixed by asking
        # the same question again. The known instance: a client asked for
        # dimensions answered with two package sizes — a true fact about their
        # shipment that the canonical record cannot hold — so extraction
        # correctly returned AMBIGUOUS (BR-7: never guess), the field stayed
        # empty, and the loop would have re-sent the identical question to a
        # client who had already answered it in full. That is a person's
        # problem to take over, and MANUAL_REVIEW is the state that says so.
        #
        # Deliberately narrow: only after a clarification actually went out
        # (a first-contact enquiry with an ambiguous field is exactly what
        # clarification exists for), and only for a field that is required,
        # still missing after this merge, and ambiguous in this extraction —
        # the client engaged with the question and we still could not use the
        # answer. Detected before the EXTRACTED advance because the approved
        # table exits to MANUAL_REVIEW from CLARIFICATION_SENT, not from
        # EXTRACTED.
        futile: tuple[str, ...] = ()
        if not abandoned and state is RequestState.CLARIFICATION_SENT:
            still_missing = validate_shipment(merge.record).missing_fields
            futile = tuple(
                field.value
                for field in still_missing
                if getattr(extraction, field.value).status is FieldStatus.AMBIGUOUS
            )
        if futile:
            state = self._advance(request_id, state, RequestState.MANUAL_REVIEW)
            notes = [note for field in futile if (note := getattr(extraction, field).note)]
            self._emit(
                request_id,
                AuditEventType.MANUAL_REVIEW_ESCALATED,
                {"fields": list(futile), "notes": notes},
            )
        elif not abandoned:
            state = self._advance(request_id, state, RequestState.EXTRACTED)
        self._emit(
            request_id,
            AuditEventType.RECORD_MERGED,
            {"changed": [f.value for f in merge.changed], "conflicts": len(merge.conflicts)},
        )
        if merge.has_conflicts:
            self._emit(
                request_id,
                AuditEventType.CONFLICT_DETECTED,
                {"fields": [c.field.value for c in merge.conflicts]},
            )

        validation = validate_shipment(merge.record)
        self._emit(
            request_id,
            AuditEventType.VALIDATED,
            {"valid": validation.is_valid, "missing": len(validation.missing_fields)},
        )

        analysis = identify_unresolved(validation, extraction, merge.conflicts)

        clarification: ClarificationMessage | None = None
        if not abandoned and not futile:
            state, clarification = self._decide(request_id, state, email, analysis)

        self._store.save_request(
            QuotationRequest(
                request_id=request_id,
                state=state,
                record=merge.record,
                client_address=email.from_address,
            )
        )

        return TurnOutcome(
            request_id=request_id,
            state=state,
            record=merge.record,
            extraction=extraction,
            merge=merge,
            validation=validation,
            analysis=analysis,
            clarification=clarification,
            round_number=self._rounds.get(request_id, 0),
            escalation_notes=tuple(
                note for field in futile if (note := getattr(extraction, field).note)
            ),
        )

    # ------------------------------------------------------------- decision --

    def _decide(
        self,
        request_id: str,
        state: RequestState,
        email: RawEmail,
        analysis: UnresolvedAnalysis,
    ) -> tuple[RequestState, ClarificationMessage | None]:
        if not analysis.needs_clarification:
            if analysis.is_stuck:
                # The client has explicitly said they cannot supply something
                # required. Asking again would be rude and useless, and whether
                # to quote anyway is a person's call.
                #
                # The request stays at EXTRACTED and reports `is_stuck`: the
                # approved table has no EXTRACTED -> MANUAL_REVIEW edge, and
                # adding one changes the state model, which needs sign-off.
                return state, None

            return self._advance(request_id, state, RequestState.VALIDATED), None

        state = self._advance(request_id, state, RequestState.NEEDS_INFO)
        clarification = compose_clarification(request_id, analysis)
        assert clarification is not None  # needs_clarification guarantees one

        # Drafted, not sent. The system never mails a client on its own: it
        # shows what is missing, shows the draft, and waits for a person. The
        # request stops at NEEDS_INFO, which already means "gaps found,
        # clarification not yet sent" — no new state is needed to say this.
        #
        # Nothing is handed to the EmailSink here. Releasing the draft happens
        # in `approve_clarification`, and only a person can call it.
        self._pending[request_id] = _PendingDraft(
            message=clarification,
            to_address=email.from_address,
            subject=_reply_subject(email.subject, clarification.subject),
            in_reply_to=email.message_id,
        )
        self._rounds[request_id] = self._rounds.get(request_id, 0) + 1
        self._emit(
            request_id,
            AuditEventType.CLARIFICATION_DRAFTED,
            {
                "round": self._rounds[request_id],
                "fields": [u.field.value for u in clarification.unresolved],
                "reasons": sorted(r.value for r in clarification.reasons),
                "sent": False,
                "awaiting": "human approval",
            },
        )
        return state, clarification

    # ------------------------------------------------------------- approval --

    def pending_draft(self, request_id: str) -> ClarificationMessage | None:
        """The draft waiting on a person, if there is one."""
        held = self._pending.get(request_id)
        return held.message if held else None

    def approve_clarification(self, request_id: str, *, by: str) -> Approved:
        """A person approved the draft. The only path out of NEEDS_INFO.

        This is the business control the stakeholder asked for: the system
        drafts and shows; a human decides. There is no timeout into approval and
        no caller that can reach this without naming who approved.

        Releasing the draft means handing it to the `EmailSink`, which in a live
        run really does deliver over Gmail.

        **The draft is consumed only once the sink has accepted it.** It used to
        be popped on the way in, which meant a send that raised — an expired
        credential, a scope the token does not hold, a provider outage — took
        the pending draft with it. The request stayed in NEEDS_INFO with nothing
        left to approve: the operator's next click answered "no clarification
        draft is awaiting approval", the client's reply stayed deferred behind a
        clarification that had never gone out, and the audit trail recorded an
        approval with no send beside it. Holding the draft until the send
        succeeds makes the operation retryable, which is the correct shape for
        something that depends on a remote service.
        """
        held = self._pending.get(request_id)
        if held is None:
            raise IllegalTransition(f"no clarification draft is awaiting approval for {request_id}")

        stored = self._store.get_request(request_id)
        if stored is None:  # pragma: no cover - a draft implies a stored request
            raise IllegalTransition(f"no request {request_id} to approve against")

        approval = Approved(by=by, at=self._clock.now())
        self._emit(
            request_id,
            AuditEventType.CLARIFICATION_APPROVED,
            {"by": by, "fields": [u.field.value for u in held.message.unresolved]},
        )

        # Everything after this line is contingent on the provider accepting the
        # message. If `send` raises, it propagates: the draft is still pending,
        # the state is still NEEDS_INFO, nothing was persisted, and the operator
        # can approve again once the cause is fixed.
        self._sink.send(
            OutboundMessage(
                to_address=held.to_address,
                subject=held.subject,
                body_text=held.message.body_text,
                in_reply_to=held.in_reply_to,
            )
        )

        # Sent. Only now is the draft spent, so it cannot be released twice.
        del self._pending[request_id]
        state = self._advance(request_id, stored.state, RequestState.CLARIFICATION_SENT)
        self._emit(request_id, AuditEventType.CLARIFICATION_SENT, {"approved_by": by})
        self._store.save_request(stored.model_copy(update={"state": state}))
        return approval

    # -------------------------------------------------------------- helpers --

    def _advance(
        self, request_id: str, current: RequestState, target: RequestState
    ) -> RequestState:
        if current is target:
            return current
        self._machine.assert_transition(current, target)
        self._emit(
            request_id,
            AuditEventType.STATE_CHANGED,
            {"from": current.value, "to": target.value},
        )
        return target

    def _blank(self, request_id: str) -> ShipmentRecord:
        return ShipmentRecord(request_id=request_id, source=RequestSource.EMAIL)

    def _emit(self, request_id: str, event: AuditEventType, detail: dict[str, object]) -> None:
        """Record what happened.

        The audit trail is this layer's only observability channel — `pipeline`
        may not reach the application logger, and does not need to: everything
        worth recording here is a workflow event, not a diagnostic.

        Details carry field names, counts and states. Never the email body,
        never an address, never a credential. This is evidence that the workflow
        ran as designed, not a copy of the client's correspondence.
        """
        if self._audit is None:
            return
        self._audit.record(
            AuditEvent(request_id=request_id, event=event, at=self._clock.now(), detail=detail)
        )


def _reply_subject(inbound: str, fallback: str) -> str:
    subject = inbound.strip() or fallback
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"
