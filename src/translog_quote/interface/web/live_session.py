"""The real-Gmail workflow, driven from a browser instead of a terminal.

    real mailbox (read-only credential)
        -> InboundRouter -> correlation -> live extraction -> merge -> validate
        -> clarification, released by a named person clicking, SENT for real
        -> the client's reply, correlated and merged -> VALIDATED
        -> DemoRateProvider -> filter -> select
        -> QuotationStage -> review emailed to the approver
                          -> the browser renders the packet and waits
                          -> APPROVE -> quotation emailed to the client
                             DECLINE -> nothing sent

This module is wiring and bookkeeping. It owns no business rule: every object
it holds is the one `gmail-quote` already builds through `bootstrap`, and every
decision it reports was made by code with its own test suite. What it adds is
the ability to *stop between steps* and be asked what happened, which is what a
browser needs and a linear script does not provide.

The two gates are unchanged and unreachable from here without a person:

- a clarification is released only by `approve_clarification(by=...)`, which
  has no default for `by` and no caller inside this class;
- a quotation is decided only by `decide(...)`, which hands a
  `RecordedDecisionGate` one explicit decision and then runs the existing
  `QuotationStage`. Nothing in this module calls the email sink itself.

Persistence follows the same commit-point rule as the CLI: the working store is
in memory, and the durable store is written only once something irreversible
has happened. A session that ends at a gate having sent nothing leaves the
demonstration exactly where it was.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from translog_quote import bootstrap
from translog_quote.domain.quotation import (
    INTERNAL_SUBJECT_PREFIX,
    ReviewPacket,
    decision_from_choice,
)
from translog_quote.domain.rates import FASTEST_ELIGIBLE
from translog_quote.domain.validation import validate_shipment
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import IllegalTransition, PermanentFailure, TranslogError
from translog_quote.interface.demo.gmail_thread import _request_id_for
from translog_quote.interface.web.audit_log import JsonFileAuditLog
from translog_quote.interface.web.demonstration import DemonstrationFile
from translog_quote.observability import get_logger
from translog_quote.pipeline import RateSearchStage

if TYPE_CHECKING:
    from collections.abc import Collection

    from translog_quote.config import Settings
    from translog_quote.domain.clarification import ClarificationMessage
    from translog_quote.domain.email import RawEmail
    from translog_quote.domain.quotation import ApprovalDecision
    from translog_quote.domain.shipment import ShipmentRecord
    from translog_quote.domain.validation import ValidationResult
    from translog_quote.interface.web.demonstration import Demonstration
    from translog_quote.pipeline import QuotationStage, RateSearchOutcome
    from translog_quote.pipeline.audit import AuditEvent
    from translog_quote.ports import (
        ClockPort,
        EmailSink,
        EmailSource,
        ExtractionPort,
        LocationResolverPort,
        StorePort,
    )

#: How many mailbox messages one poll may read. A conversation is an enquiry
#: and its replies; a small ceiling, not a mailbox scan.
MESSAGE_LIMIT = 10

#: AMB-8: no approved source for a rate-search date exists, so the session
#: states one rather than letting anything downstream invent it.
SEARCH_DATE = datetime.date(2026, 9, 2)

_log = get_logger("interface.web.live_session")


class LiveSequenceError(Exception):
    """An action was requested out of order. A client error, not a system one."""


class CollectingAudit:
    """Keeps the pipeline's evidence trail so the browser can display it.

    A presentation concern, which is why it lives here rather than in
    `adapters/`: the events are the argument that the gates held, and an
    argument nobody can read proves nothing.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class LiveRequest:
    """One request as the interface knows it.

    Deliberately built from *facts* — a record, a state, a selection — rather
    than from a `TurnOutcome`, because a session restarted after the client
    went home has a persisted request and no turn to go with it. Both paths
    produce this same shape.
    """

    request_id: str
    client_address: str
    state: RequestState
    record: ShipmentRecord
    validation: ValidationResult
    last_message_id: str | None = None
    subject: str = ""
    enquiry: RawEmail | None = None
    reply: RawEmail | None = None
    latest_email: RawEmail | None = None
    reply_received: bool = False
    waiting_replies: list[str] = field(default_factory=list)
    """Messages that answer this request but cannot be processed yet.

    A reply is refused while its own request is holding an unsent clarification
    — the table permits no way out of NEEDS_INFO except CLARIFICATION_SENT. The
    refusal is correct, and silent: without this the operator sees a request
    that looks idle while their client is waiting, and no reason why.
    """

    merged_fields: tuple[str, ...] = ()
    carried_fields: tuple[str, ...] = ()
    stated_count: int = 0
    clarification: ClarificationMessage | None = None
    clarification_sent_by: str | None = None
    manual_review_notes: tuple[str, ...] = ()
    """Why this request was handed to a person: the model's own explanation of
    the answer it could not use. Empty for every request that was not."""

    rates: RateSearchOutcome | None = None
    rate_failure: str | None = None
    """Why rate search could not run for this request, if it could not.

    Set instead of the search result, never alongside it: a request either has
    rates or has a reason it has none. It is a *report*, not a state — the
    request stays where the state machine put it, so the next poll retries it
    for free once the cause is fixed.
    """

    packet: ReviewPacket | None = None
    decision: ApprovalDecision | None = None
    quotation_sent: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def shipment_field_count(self) -> int:
        """How many canonical shipment fields this message actually filled in."""
        return sum(1 for name in _RECORD_FIELDS if getattr(self.record, name) is not None)

    @property
    def looks_like_an_enquiry(self) -> bool:
        """Whether this message carried any shipment information at all.

        The classification the pipeline already performs, read rather than
        re-derived: extraction is forbidden from filling a field the email did
        not state (BR-7), so a message that yields no origin, destination,
        weight, commodity or anything else stated no shipment. An ordinary
        inbox email — a notification, a newsletter — lands here with zero.

        It is a *display* judgement and nothing more. It decides nothing, sends
        nothing, and blocks nothing: the operator sees both groups and picks
        the request they mean. That is why it can afford to be a simple count
        rather than a rule anybody has to maintain a list for.
        """
        return self.shipment_field_count > 0

    @property
    def awaiting_clarification_approval(self) -> bool:
        """A draft exists and is waiting on a person. Nothing has been sent."""
        return self.clarification is not None and self.state is RequestState.NEEDS_INFO

    @property
    def awaiting_quotation_decision(self) -> bool:
        """A rate is selected and the gate has not yet been answered."""
        return self.packet is not None and self.decision is None

    @property
    def is_settled(self) -> bool:
        return self.state in {RequestState.QUOTATION_SENT, RequestState.MAKER_REJECTED}


class LiveSession:
    """One browser-driven run of the real workflow.

    Every collaborator is injectable so the interface can be exercised without
    a mailbox, a model or a credential — the same discipline the terminal
    commands follow. Nothing is built lazily at first use: a misconfigured send
    credential stops the session at construction rather than halfway through a
    client's enquiry.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        source: EmailSource | None = None,
        sink: EmailSink | None = None,
        extractor: ExtractionPort | None = None,
        durable: StorePort | None = None,
        clock: ClockPort | None = None,
        audit: CollectingAudit | None = None,
        resolver: LocationResolverPort | None = None,
    ) -> None:
        self._settings = settings
        # Injectable like every other collaborator, so a test can exercise a
        # provider that cannot identify a particular place without needing a
        # real one. The default is whatever the configured mode calls for.
        self._resolver = resolver or bootstrap.build_location_resolver(settings)
        # The wall clock, not the fixed one. A live run's audit trail is a
        # record of when things actually happened; freezing it would stamp
        # every event with the same invented moment, and the interface would
        # then be displaying a fabricated time.
        self._clock = clock or bootstrap.build_system_clock()
        # Persisted, so a restarted server still shows what happened rather
        # than an empty history for a request that plainly progressed.
        self.audit: CollectingAudit | JsonFileAuditLog = audit or JsonFileAuditLog(
            settings.demo.state_dir
        )

        self._durable = (
            durable if durable is not None else bootstrap.build_persistent_store(settings)
        )
        self._working = bootstrap.build_memory_store()
        bootstrap.seed_store(self._working, self._durable)

        # Built before anything is read: a broken send credential should stop
        # the session here, not after a client's mail has been processed.
        self._sink = sink if sink is not None else bootstrap.build_gmail_email_sink(settings)
        self._source = source
        # Built here rather than left to the router so shutdown can reach it.
        # `build_inbound_router` would otherwise construct the real adapter
        # itself and keep it private, and the OpenRouter client inside it would
        # be the one connection nobody could close. Identical timing — the
        # router built it at this same moment — and an injected extractor still
        # wins, so every test is unaffected.
        self._extractor = (
            extractor if extractor is not None else bootstrap.build_extractor(settings)
        )

        self._router = bootstrap.build_inbound_router(
            settings,
            new_request_id=_request_id_for,
            store=self._working,
            extractor=self._extractor,
            audit=self.audit,
            sink=self._sink,
            clock=self._clock,
        )
        self._gate = bootstrap.build_recorded_approval()
        self._quotation: QuotationStage = bootstrap.build_quotation_stage(
            settings,
            sink=self._sink,
            approval=self._gate,
            store=self._working,
            audit=self.audit,
            clock=self._clock,
        )

        self.approver_address = settings.gmail.approver_address or ""
        # Which of the mailbox's real messages this presentation is following.
        # Deletes nothing and names nothing: a demonstration is whatever
        # arrived after the presenter pressed Start.
        self._demonstration = DemonstrationFile(settings.demo.state_dir)
        self.outside_demonstration = 0
        self.skipped_internal = 0
        self.blocked_messages = 0
        # Messages this process has already routed. The durable store only
        # remembers messages whose work was *committed*, and an enquiry waiting
        # on its clarification commits nothing on purpose — so without this,
        # every poll would re-extract every open enquiry, at one live model
        # call each. That was expensive when a person pressed the button; with
        # the server polling on a timer it would be unbounded. A deferred
        # message is deliberately not added: it has to be retried once its
        # clarification has gone out.
        self._routed: set[str] = set()
        self.last_poll_new = 0
        self.last_poll_at: datetime.datetime | None = None
        """When the mailbox was last read successfully. Displayed, so a room
        watching a dashboard that has not moved can tell "nothing arrived" from
        "nothing is running"."""

        self.last_poll_error: str | None = None
        """The class of the last failed poll, or None. Written by whatever
        drives the polling — the background poller does, and clears it on the
        next success — so an unreachable mailbox is visible, not silent."""

        self.requests: dict[str, LiveRequest] = {}
        self._restore()

    # ------------------------------------------------------------- actions --

    def poll(self) -> None:
        """Read the mailbox and process whatever is new. Sends nothing.

        Stops at a held clarification rather than pushing past it: the
        transition table permits no way out of NEEDS_INFO except
        CLARIFICATION_SENT, so a reply cannot be processed until the question
        it answers has actually gone out — and only a person can send it.
        """
        received = self._fetch()
        client_mail = [email for email in received if not _is_internal(email)]
        self.skipped_internal = len(received) - len(client_mail)

        # Messages older than the demonstration are history, not this
        # presentation. Left unread rather than read-and-hidden: extracting a
        # year of newsletters to then not show them would cost a live model
        # call each and make the first poll unusable.
        in_scope = [e for e in client_mail if self._demonstration.current.covers(e.received_at)]
        self.outside_demonstration = len(client_mail) - len(in_scope)

        conversation = sorted(in_scope, key=lambda email: email.received_at)
        fresh = [
            email
            for email in conversation
            if email.message_id not in self._routed
            and not self._router.already_processed(email.message_id)
        ]
        self.last_poll_new = len(fresh)

        self.blocked_messages = 0
        for request in self.requests.values():
            # Recomputed from scratch each poll, so a reply that has since been
            # merged stops being reported as waiting.
            request.waiting_replies = []

        for email in fresh:
            # Recomputed each time round: the enquiry processed a moment ago is
            # exactly what blocks the reply behind it, so a map captured before
            # the loop would still be empty when it mattered.
            blocking = self._blocked_request_for(email, self._ids_awaiting_clarification())
            if blocking is not None:
                # Deferred without extracting. The costly version of this is
                # letting it through and catching the refusal below: the
                # clarification loop calls the model *before* it checks the
                # transition, so every poll would pay a live call per waiting
                # reply and every poll would get slower the longer a
                # conversation stayed open.
                self.blocked_messages += 1
                waiting = self.requests[blocking].waiting_replies
                if email.message_id not in waiting:
                    waiting.append(email.message_id)
                continue
            try:
                self._route(email)
            except IllegalTransition:
                # This message belongs to a request that is holding a
                # clarification draft. The table permits no way out of
                # NEEDS_INFO except CLARIFICATION_SENT, so it cannot be
                # processed until a person sends that clarification.
                #
                # Skipped rather than fatal, and skipped *individually*: this
                # guard used to stop the whole loop, which meant one ordinary
                # inbox message holding a draft blocked every other message
                # behind it. Requests are independent, so the block is too.
                #
                # Nothing is recorded for it, so the next poll — after the
                # clarification has gone out — picks it up normally.
                self.blocked_messages += 1
                _log.info("Message deferred: its request is awaiting a clarification send")
            else:
                self._routed.add(email.message_id)

        self._search_rates_for_validated()
        self.last_poll_at = self._clock.now()

    def start_demonstration(self) -> None:
        """Begin a fresh demonstration from this moment.

        Deletes nothing — not a Gmail message, not a persisted request, not an
        audit entry. What it does is set the cutoff and empty the *live view*:
        work from before this moment stays in the durable store, stays
        correlatable, and stops being surfaced as active.

        Both halves matter. Keeping earlier requests on screen was the previous
        behaviour and it does not survive a mailbox with history in it: a
        restarted server rebuilt every persisted request into the interface,
        and the background poller then ran rate search against each one — so
        old work did not merely appear, it advanced. Dropping them here is what
        makes "only the current request is active" true of the session rather
        than of the page rendering it.
        """
        self._demonstration.start(self._clock.now())
        self.requests = {
            request_id: request
            for request_id, request in self.requests.items()
            if self.in_demonstration(request_id)
        }

    @property
    def demonstration(self) -> Demonstration:
        return self._demonstration.current

    def in_demonstration(self, request_id: str) -> bool:
        """Whether this request is one the current demonstration follows."""
        return self._demonstration.current.focuses(request_id)

    def approve_clarification(self, *, by: str, request_id: str | None = None) -> None:
        """Release one held draft on a named person's authority.

        The email leaves here — through the existing send-only Gmail sink, from
        the server. This is the one and only path out of NEEDS_INFO, and it has
        no default for `by`.

        ``request_id`` names *which* draft. It is not optional in practice: the
        browser always sends the request the operator was looking at, and
        without it this method used to release whichever draft happened to be
        first in the dictionary. With several enquiries awaiting clarification
        — the normal state of a mailbox with more than one open conversation —
        that meant clicking Approve on one request mailed a different client
        about a different shipment, in that client's own thread.
        """
        who = by.strip()
        if not who:
            raise LiveSequenceError("A clarification can only be approved by a named person.")

        request = self._awaiting_clarification(request_id)
        self._router.approve(request.request_id, by=who)

        stored = self._working.get_request(request.request_id)
        request.state = stored.state if stored else RequestState.CLARIFICATION_SENT
        request.clarification_sent_by = who
        bootstrap.commit_request(self._working, self._durable, request.request_id)

    def decide(self, request_id: str, *, choice: str, by: str, reason: str = "") -> LiveRequest:
        """Apply one explicit human decision to the quotation gate.

        The browser's click is the decision the `ApprovalPort` exists to carry.
        `decision_from_choice` refuses anything that is not exactly an approval
        or a decline by a named person, and `QuotationStage` — unchanged — is
        what actually sends or does not send. Nothing in this method touches
        the email sink.
        """
        request = self.requests.get(request_id)
        if request is None:
            raise LiveSequenceError(f"There is no request {request_id}.")
        if request.packet is None:
            raise LiveSequenceError("There is no quotation awaiting a decision on this request.")
        if request.decision is not None:
            raise LiveSequenceError(
                f"{request_id} has already been decided; it will not be decided again."
            )

        decision = decision_from_choice(choice, by=by, at=self._clock.now(), reason=reason)
        self._gate.record(decision)
        outcome = self._quotation.run(
            request.packet,
            client_address=request.client_address,
            is_simulated=request.rates.uses_mock_data if request.rates else True,
            in_reply_to=request.last_message_id,
        )

        request.decision = outcome.decision
        request.quotation_sent = outcome.sent
        request.state = outcome.state
        bootstrap.commit_request(self._working, self._durable, request_id)
        return request

    def close(self) -> None:
        """Release the HTTP connections this session's collaborators hold.

        Each of the three keeps one pooled `httpx.Client` for its lifetime, so
        the sockets outlive any single call and want closing when the server
        does. Duck-typed on purpose: the stubs a test injects have no
        connections and no `close`, and the ports they satisfy describe the
        workflow rather than a lifecycle.
        """
        for collaborator in (self._source, self._sink, self._extractor):
            closer = getattr(collaborator, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - shutdown must not raise
                    _log.warning("A collaborator refused to close cleanly", exc_info=True)

    # ------------------------------------------------------------ internals --

    def _fetch(self) -> tuple[RawEmail, ...]:
        """Read the mailbox through this session's own source, built once.

        Every poll used to construct a new one. That was reasonable when a
        person pressed a button; on a timer it meant re-reading the OAuth token
        file from disk and buying a fresh access token — a round trip to
        Google's token endpoint ahead of the first Gmail call, every ten
        seconds, for the life of the process.

        Nothing about what is read changes: the same credential, the same
        query, the same ceiling. `sent_by_us` was already a callable precisely
        so a source could outlive the moment it was built, so a reused one
        still sees every message sent since.

        A failed build leaves the attribute unset rather than caching the
        failure, so the next poll tries again — one bad moment must not leave
        the session permanently unable to read mail.
        """
        if self._source is None:
            self._source = bootstrap.build_gmail_email_source(
                self._settings, max_results=MESSAGE_LIMIT, sent_by_us=self._sent_provider_ids
            )
        return self._source.fetch_new()

    def _sent_provider_ids(self) -> Collection[str]:
        """Provider ids of everything this session has delivered.

        Empty for a sink that does not track them — the collecting sink used in
        tests, and any future one — so the filter simply does nothing rather
        than requiring every sink to implement it.
        """
        ids = getattr(self._sink, "sent_provider_ids", None)
        return ids if isinstance(ids, set | frozenset | tuple | list) else ()

    def _route(self, email: RawEmail) -> None:
        routed = self._router.route(email)
        if routed.was_refused or routed.outcome is None or routed.request_id is None:
            return

        outcome = routed.outcome
        request = self.requests.get(routed.request_id) or LiveRequest(
            request_id=routed.request_id,
            client_address=email.from_address,
            state=outcome.state,
            record=outcome.record,
            validation=outcome.validation,
            enquiry=email,
        )
        # Recorded on every routed message, not only the first. Membership is
        # "this demonstration processed mail for it", and the case that needs
        # the difference is a server restarted mid-conversation: the enquiry is
        # in the durable store but not in this demonstration's list, and the
        # client's reply — which arrives after the new cutoff and is therefore
        # legitimately in scope — must bring its request into focus rather than
        # advance one the interface refuses to show. `include` is idempotent.
        self._demonstration.include(routed.request_id)

        request.state = outcome.state
        request.record = outcome.record
        request.validation = outcome.validation
        request.last_message_id = email.message_id
        request.latest_email = email
        request.stated_count = len(outcome.merge.changed) + request.stated_count
        request.clarification = outcome.clarification
        if outcome.escalation_notes:
            request.manual_review_notes = outcome.escalation_notes
        request.messages.append(email.message_id)

        if not request.subject:
            request.subject = email.subject
        if routed.is_reply:
            request.reply_received = True
            request.reply = email
            request.merged_fields = tuple(f.value for f in outcome.merge.changed)
            request.carried_fields = tuple(
                name
                for name in _RECORD_FIELDS
                if getattr(outcome.record, name) is not None and name not in request.merged_fields
            )

        self.requests[routed.request_id] = request

        # A merged reply is worth keeping whatever happens next; a draft that
        # nobody has approved is not, and persisting it would leave the request
        # unable to advance in any later session.
        if outcome.state is not RequestState.NEEDS_INFO:
            bootstrap.commit_request(self._working, self._durable, routed.request_id)
        elif not request.looks_like_an_enquiry:
            # An ordinary inbox message that carried no shipment. Record only
            # that it was seen, so it is never extracted again, and leave no
            # request behind for anyone to have to explain.
            bootstrap.commit_thread(self._working, self._durable, routed.request_id)

    def _search_rates_for_validated(self) -> None:
        """Run the rate pipeline for any request that has just validated.

        Not a gate and not a send: searching, filtering and ranking are
        deterministic and contact nobody, so they happen as soon as a shipment
        is complete. The gate is the next step, and it is a person's.
        """
        stage = RateSearchStage(
            provider=bootstrap.build_demo_rate_provider(),
            resolver=self._resolver,
            strategy=FASTEST_ELIGIBLE,
            audit=self.audit,
            clock=self._clock,
        )
        for request in self.requests.values():
            if request.rates is not None or request.state is not RequestState.VALIDATED:
                continue
            try:
                outcome = stage.run(
                    request.request_id,
                    request.record,
                    on_date=SEARCH_DATE,
                    cargo_is_liquid=None,  # AMB-3: stated, never derived
                )
            except TranslogError as exc:
                # One request that cannot be priced is one request that cannot
                # be priced. Before this, an unroutable lane raised out of the
                # loop, out of `poll`, and out of the request handler as a 500
                # — so a single enquiry naming a place outside the demo lane
                # table stopped every *other* request from being searched and
                # ended the poll that would have read the rest of the mailbox.
                #
                # Caught narrowly on purpose. The project taxonomy is the set
                # of failures a *request* can have — `UnresolvedLocation` among
                # them, since a place the provider cannot identify is one
                # enquiry's problem. Anything else is a defect in this process
                # and must still escape loudly rather than be recorded as a
                # property of somebody's enquiry.
                #
                # Nothing else is touched: the state stays VALIDATED and
                # `rates` stays None, which is exactly the pair this loop
                # selects on — so the retry is the next poll, with no queue to
                # drain and nothing to reset. No packet is built, so the
                # request cannot reach the approval gate, and no sink is
                # called, so the failure cannot send anything.
                request.rate_failure = str(exc)
                _log.warning("Rate search failed for %s: %s", request.request_id, exc)
                continue

            request.rate_failure = None
            request.rates = outcome
            request.state = outcome.state
            if outcome.selection is not None:
                request.packet = ReviewPacket(
                    request_id=request.request_id,
                    record=request.record,
                    validation=request.validation,
                    clarification_sent=request.clarification_sent_by is not None,
                    rates=outcome.filtered,
                    selection=outcome.selection,
                )

    def _ids_awaiting_clarification(self) -> dict[str, str]:
        """Message id -> the request holding an unsent draft, for each such request."""
        return {
            message_id: request.request_id
            for request in self.requests.values()
            if request.awaiting_clarification_approval
            for message_id in request.messages
        }

    @staticmethod
    def _blocked_request_for(email: RawEmail, blocked: dict[str, str]) -> str | None:
        """Which request, if any, this message answers and cannot yet advance.

        A cheap pre-check, and deliberately not a correlation decision: its only
        possible effect is to *defer*, never to merge. A wrong answer costs one
        retry on the next poll, which is why it is safe to read headers here
        while the real placement stays entirely with `CorrelationPolicy`.
        """
        if not blocked:
            return None
        for candidate in (email.in_reply_to, *email.references):
            if candidate is not None and candidate in blocked:
                return blocked[candidate]
        return None

    def _awaiting_clarification(self, request_id: str | None) -> LiveRequest:
        """The draft to release, named rather than guessed.

        Falling back to "the only one" is safe and keeps a single-request
        demonstration working from a client that sends no id. Falling back to
        "the first one" is not, and was the defect: dictionary order is not a
        decision anybody made, and the consequence is a real email to a real
        client about the wrong shipment.
        """
        held = [r for r in self.requests.values() if r.awaiting_clarification_approval]
        if not held:
            raise LiveSequenceError("No clarification draft is awaiting approval.")

        if request_id is not None:
            for candidate in held:
                if candidate.request_id == request_id:
                    return candidate
            raise LiveSequenceError(f"{request_id} has no clarification awaiting approval.")

        if len(held) > 1:
            raise LiveSequenceError(
                "Several requests are awaiting clarification; the one to approve "
                "must be named. Open the request and approve it from there."
            )
        return held[0]

    def _restore(self) -> None:
        """Rebuild the interface's view of requests an earlier session persisted.

        Only the ones this demonstration is following. The durable store is
        still seeded in full — correlation, duplicate protection and the
        already-sent record all read it, and none of them may lose history —
        but a request from another demonstration is not active work, and
        putting it back in `self.requests` would make it so: the rate-search
        pass walks that dictionary, so a restored VALIDATED request would be
        priced and pushed to the approval gate by the next background poll.

        With no demonstration active `focuses` is true of everything, so a
        session built without one restores exactly what it always did.

        Validation is recomputed rather than stored: it is a pure function of
        the record, so deriving it cannot disagree with the validator, whereas
        a stored copy could.
        """
        for thread in self._durable.all_threads():
            stored = self._durable.get_request(thread.request_id)
            if stored is None or not self.in_demonstration(thread.request_id):
                continue
            self.requests[stored.request_id] = LiveRequest(
                request_id=stored.request_id,
                client_address=stored.client_address,
                state=stored.state,
                record=stored.record,
                validation=validate_shipment(stored.record),
                last_message_id=thread.message_ids[-1] if thread.message_ids else None,
                reply_received=len(thread.message_ids) > 1,
                messages=list(thread.message_ids),
                clarification_sent_by="(an earlier session)"
                if stored.state is not RequestState.NEEDS_INFO
                and stored.state is not RequestState.RECEIVED
                else None,
                quotation_sent=stored.state is RequestState.QUOTATION_SENT,
            )


_RECORD_FIELDS: tuple[str, ...] = (
    "origin",
    "destination",
    "weight_kg",
    "dimensions_in",
    "commodity",
    "cargo_type",
    "is_chemical",
    "msds_attached",
    "pcs",
    "delivery_type",
    "delivery_address",
)


def _is_internal(email: RawEmail) -> bool:
    """Approval requests we sent ourselves are not client enquiries.

    The approver mailbox is, in this demo, the mailbox Translog reads. Matching
    on our own subject marker is safe here in a way subject matching is not
    safe for correlation: this is a refusal, not a merge — the worst case is a
    message skipped, never two conversations joined.
    """
    return email.subject.strip().startswith(INTERNAL_SUBJECT_PREFIX)


def build_live_session(settings: Settings) -> LiveSession:
    """The session the server serves, or a readable refusal.

    Configuration is checked here so a misconfigured demo fails at start-up
    with a sentence a person can act on, rather than as a 500 in front of an
    audience.

    Starting the demonstration is part of starting the server. There is no
    button for it and nothing to press: the cutoff is *now*, so the mailbox's
    history — however much of it there is — is out of scope before the first
    poll runs, and the first thing this process can process is the enquiry
    somebody sends after it came up.
    """
    if settings.openrouter.api_key is None:
        raise PermanentFailure("No OpenRouter API key. Set TRANSLOG_OPENROUTER__API_KEY in .env.")
    if not settings.gmail.send_enabled:
        raise PermanentFailure(
            "Outbound Gmail is disabled. Set TRANSLOG_GMAIL__SEND_ENABLED=true in .env."
        )
    if not settings.gmail.approver_address:
        raise PermanentFailure(
            "No internal approver address. Set TRANSLOG_GMAIL__APPROVER_ADDRESS in .env."
        )
    session = LiveSession(settings)
    session.start_demonstration()
    return session
