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
    UnresolvedField,
    UnresolvedReason,
    compose_clarification,
    identify_unresolved,
    location_question,
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
    from collections.abc import Sequence

    from translog_quote.domain.clarification import ClarificationMessage, UnresolvedPlace
    from translog_quote.domain.email import RawEmail
    from translog_quote.domain.extraction import ExtractionResult
    from translog_quote.domain.shipment import FieldName, MergeResult
    from translog_quote.domain.validation import ValidationResult
    from translog_quote.pipeline.audit import AuditSink
    from translog_quote.ports import ClockPort, EmailSink, ExtractionPort, StorePort

DEFAULT_MAX_ROUNDS = 3

#: Why a request was handed over when the client answered and said nothing the
#: record could take. Deterministic wording, like every other client-facing and
#: operator-facing sentence in this workflow: the model has no opinion to quote
#: here, because the whole point is that it reported nothing at all.
SILENT_REPLY_NOTE = (
    "The client replied to the clarification, but the reply stated nothing that "
    "answers it. Asking the same question again will not resolve it."
)


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
        """Handed over to a person.

        A shipment blocked by an explicit client denial of a required field now
        *moves* to MANUAL_REVIEW (the EXTRACTED -> MANUAL_REVIEW edge), so the
        state alone tells the whole story; `is_stuck` is kept as a belt-and-braces
        check in case a caller inspects an analysis without applying the turn.
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
        # by asking *again* — but the reply in hand might be the one that finally
        # completes the shipment. So the budget is only *measured* here; the
        # hand-over is decided after this reply is merged and validated (below),
        # so a final reply that resolves everything continues to VALIDATED rather
        # than being abandoned on the doorstep. Over-budget blocks only the
        # drafting of the *next* question, never a reply that resolves the record.
        over_budget = (
            state is RequestState.CLARIFICATION_SENT
            and self._rounds.get(request_id, 0) >= self._max_rounds
        )

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
        notes: list[str] = []
        if state is RequestState.CLARIFICATION_SENT:
            still_missing = validate_shipment(merge.record).missing_fields
            futile = tuple(
                field.value
                for field in still_missing
                if getattr(extraction, field.value).status is FieldStatus.AMBIGUOUS
            )
            notes = [note for field in futile if (note := getattr(extraction, field).note)]

            # The second way a reply can be futile: it answers, and states
            # nothing at all. A client asked "whether the cargo is a chemical
            # product" replied "yes" — a real answer to a human, and no answer
            # to the record, because the question it refers to is not in the
            # message. Extraction reported nothing rather than guessing, which
            # is BR-7 working correctly, and the loop then re-sent the same
            # question to a client who believed they had answered it. Two more
            # rounds and two more approval clicks before `max_rounds` gave up.
            #
            # Narrow on purpose, and narrower than the ambiguous case above:
            # the clarification must have gone out, the reply must have stated
            # *nothing* — not merely too little — the merge must have changed
            # nothing, and something required must still be missing. A reply
            # that moved any field at all is progress and stays in the loop.
            if (
                not futile
                and still_missing
                and not merge.changed
                and not extraction.fields_by_status(FieldStatus.STATED)
            ):
                futile = tuple(field.value for field in still_missing)
                notes = [SILENT_REPLY_NOTE]
        if futile:
            state = self._advance(request_id, state, RequestState.MANUAL_REVIEW)
            self._emit(
                request_id,
                AuditEventType.MANUAL_REVIEW_ESCALATED,
                {"fields": list(futile), "notes": notes},
            )
        else:
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

        # The client explicitly said there is no MSDS for a chemical shipment.
        # It is a valid answer (VR-8 is satisfied), so the request proceeds — but
        # the operator picking a Goods Type must see *why* there is no MSDS, so it
        # is recorded here and surfaced on the hold card. Fires only on the turn
        # the client states it, whether the model said STATED False or (wrongly)
        # DENIED — fix 1 carries both to msds_attached=False.
        if merge.record.is_chemical is True and _stated_no_msds(extraction):
            self._emit(
                request_id,
                AuditEventType.MSDS_UNAVAILABLE,
                {"msds": "not available (client stated)"},
            )

        analysis = identify_unresolved(validation, extraction, merge.conflicts)

        # An explicit client denial of a *required* field is the second way a
        # thread has nowhere to go: nothing to ask (they answered) and nothing
        # the record can take. It is handed to a person rather than parked
        # silently at EXTRACTED — the dead-end this replaces — which is why the
        # table now carries an EXTRACTED -> MANUAL_REVIEW edge. MSDS never reaches
        # here: an explicit "no MSDS" is an answer (fix 1), so it validates.
        if not futile and analysis.is_stuck:
            notes = [_denial_note(analysis.blocked_by_denial)]
            state = self._advance(request_id, state, RequestState.MANUAL_REVIEW)
            self._emit(
                request_id,
                AuditEventType.MANUAL_REVIEW_ESCALATED,
                {
                    "fields": [field.value for field in analysis.blocked_by_denial],
                    "notes": notes,
                    "reason": "client_denied_required_field",
                },
            )

        clarification: ClarificationMessage | None = None
        if not futile and not analysis.is_stuck:
            if over_budget and analysis.needs_clarification:
                # The reply did not complete the shipment and the clarification
                # budget is spent. Hand over — but name what is still open, so the
                # operator never sees a silent MANUAL_REVIEW. (Previously the
                # thread was abandoned before this reply was even read, and with
                # no note at all — a valid final reply was discarded and an
                # exhausted one gave the desk no reason.)
                notes = [_abandoned_note(analysis, self._rounds.get(request_id, 0))]
                state = self._advance(request_id, state, RequestState.MANUAL_REVIEW)
                self._emit(
                    request_id,
                    AuditEventType.MANUAL_REVIEW_ESCALATED,
                    {
                        "fields": [u.field.value for u in analysis.unresolved],
                        "notes": notes,
                        "reason": "clarification_budget_exhausted",
                    },
                )
            else:
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
            escalation_notes=tuple(notes),
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
            # Nothing left unresolved. A client denial of a required field
            # (`is_stuck`) is handled by `handle` before this point and routed to
            # MANUAL_REVIEW, so it cannot be true here.
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

    # ---------------------------------------------------------- location ask --

    def request_location_clarification(
        self,
        request_id: str,
        unresolved: Sequence[UnresolvedPlace],
        *,
        to_address: str,
        subject: str,
        in_reply_to: str,
    ) -> ClarificationMessage | None:
        """Draft one clarification for stated places that cannot be resolved to
        an airport without guessing.

        Discovered after validation, in the rate-search step — not during
        extraction — so it has its own entry point rather than riding
        ``identify_unresolved``. It registers into the same ``_pending`` the
        ordinary loop uses, so ``pending_draft`` and ``approve_clarification``
        release it unchanged, and it counts as a clarification round (sharing
        ``_max_rounds``): a place answered with the same unusable wording
        escalates to manual review through the existing round cap.

        It clears the unresolved field(s) on the stored record, so the client's
        reply fills an empty field (merge rule 1 → change) instead of clashing
        with the wording we could not use (rule 3 → conflict). The client's
        original wording is kept in each question's ``detail`` and in the audit
        event, never lost, and never turned into a code. The cleared record is
        written to the working store only; it reaches the durable store at
        approval, so a restart before approval re-derives the draft rather than
        stranding a cleared field.

        Idempotent: a second call while a draft is pending returns it without a
        second draft, transition, round or audit event.
        """
        pending = self.pending_draft(request_id)
        if pending is not None:
            return pending
        if not unresolved:
            return None

        stored = self._store.get_request(request_id)
        if stored is None:  # pragma: no cover - a validated request is always stored
            raise IllegalTransition(f"no request {request_id} to clarify against")

        # The same budget the ordinary loop honours: a place asked for the maximum
        # number of times without ever resolving to an airport will not resolve by
        # asking again. Rather than re-draft round N+1, hand it to a person — with
        # a reason — from the state it is actually in (VALIDATED here). Previously
        # this cap was enforced only when the *reply* arrived, in ``handle``; now
        # the ordinary and location loops both cap at the point of re-drafting.
        if self._rounds.get(request_id, 0) >= self._max_rounds:
            note = _abandoned_note_places(unresolved, self._rounds.get(request_id, 0))
            state = self._advance(request_id, stored.state, RequestState.MANUAL_REVIEW)
            self._store.save_request(stored.model_copy(update={"state": state}))
            self._emit(
                request_id,
                AuditEventType.MANUAL_REVIEW_ESCALATED,
                {
                    "fields": [p.field.value for p in unresolved],
                    "notes": [note],
                    "reason": "location_unresolvable_budget_exhausted",
                },
            )
            return None

        fields = tuple(
            UnresolvedField(
                field=place.field,
                reason=UnresolvedReason.AMBIGUOUS,
                question=location_question(place.field, place.stated),
                detail=place.stated,
            )
            for place in unresolved
        )
        clarification = compose_clarification(request_id, UnresolvedAnalysis(unresolved=fields))
        assert clarification is not None  # unresolved is non-empty

        cleared = stored.record.model_copy(
            update={place.field.value: None for place in unresolved}
        )
        state = self._advance(request_id, stored.state, RequestState.NEEDS_INFO)
        self._store.save_request(stored.model_copy(update={"state": state, "record": cleared}))

        self._pending[request_id] = _PendingDraft(
            message=clarification,
            to_address=to_address,
            subject=_reply_subject(subject, clarification.subject),
            in_reply_to=in_reply_to,
        )
        self._rounds[request_id] = self._rounds.get(request_id, 0) + 1
        self._emit(
            request_id,
            AuditEventType.LOCATION_UNRESOLVED,
            {
                "fields": [p.field.value for p in unresolved],
                "stated": [p.stated for p in unresolved],
            },
        )
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
        return clarification

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


def _stated_no_msds(extraction: ExtractionResult) -> bool:
    """Whether this email explicitly says there is no MSDS.

    Both shapes count: the contract's ``STATED False`` ("no MSDS available") and
    a model's ``DENIED`` for the same "no" — fix 1 carries both to
    ``msds_attached=False``. ``NOT_STATED`` (silent) and ``AMBIGUOUS`` do not.
    """
    field = extraction.msds_attached
    return field.status is FieldStatus.DENIED or (field.is_stated and field.value is False)


def _denial_note(fields: Sequence[FieldName]) -> str:
    """A deterministic operator-facing sentence for a client-denied required
    field. The wording carries no internal vocabulary beyond the field names,
    which the operator already sees on the record."""
    named = ", ".join(field.value for field in fields) or "a required detail"
    return (
        f"The client stated they cannot supply: {named}. Asking again will not "
        "resolve it — a person must decide whether to proceed."
    )


def _abandoned_note(analysis: UnresolvedAnalysis, rounds: int) -> str:
    """Operator-facing reason when the clarification budget is exhausted and the
    shipment is still incomplete. Names what is still open, like the other
    hand-over notes, so a MANUAL_REVIEW reached this way is never silent."""
    fields = [u.field.value for u in analysis.unresolved]
    named = ", ".join(fields) or "the outstanding details"
    return (
        f"After {rounds} clarification rounds the shipment still needs: {named}. "
        "Automated clarification has stopped; a person must decide how to proceed."
    )


def _abandoned_note_places(places: Sequence[UnresolvedPlace], rounds: int) -> str:
    """Operator-facing reason when a stated place cannot be resolved to an airport
    after the clarification budget is spent. Names the client's own wording, never
    a code — the same discipline as the other hand-over notes."""
    named = ", ".join(place.stated for place in places) or "the stated place"
    return (
        f"After {rounds} clarification rounds these place(s) still could not be "
        f"resolved to an airport: {named}. Automated clarification has stopped; a "
        "person must decide how to proceed."
    )


def _reply_subject(inbound: str, fallback: str) -> str:
    subject = inbound.strip() or fallback
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"
