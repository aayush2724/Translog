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

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from translog_quote import bootstrap
from translog_quote.config import WebCargoMode
from translog_quote.domain.clarification import UnresolvedPlace
from translog_quote.domain.quotation import (
    INTERNAL_SUBJECT_PREFIX,
    ReviewPacket,
    decision_from_choice,
    no_rates_message,
)
from translog_quote.domain.rates import FASTEST_ELIGIBLE, ExclusionReason
from translog_quote.domain.shipment import DeliveryType, FieldName
from translog_quote.domain.validation import validate_shipment
from translog_quote.domain.workflow import TERMINAL_STATES, RequestState
from translog_quote.errors import (
    ContractViolation,
    ExtractionUnavailable,
    IllegalTransition,
    OutboundUnavailable,
    PermanentFailure,
    TranslogError,
    UnresolvedLocation,
)
from translog_quote.interface.demo.gmail_thread import _request_id_for
from translog_quote.interface.jobs import (
    WORKER_UNKNOWN,
    JobState,
    RateSearchJobRequest,
    enqueue_rate_search,
    fetch_job_status,
    worker_liveness,
)
from translog_quote.interface.web.audit_log import JsonFileAuditLog, build_audit_log
from translog_quote.interface.web.demonstration import build_demonstration
from translog_quote.observability import get_logger
from translog_quote.pipeline import RateSearchOutcome, RateSearchStage

if TYPE_CHECKING:
    import datetime
    from collections.abc import Callable, Collection, Sequence

    from translog_quote.config import GmailAccount, Settings
    from translog_quote.domain.clarification import ClarificationMessage
    from translog_quote.domain.conversation import Thread
    from translog_quote.domain.email import RawEmail
    from translog_quote.domain.quotation import ApprovalDecision
    from translog_quote.domain.shipment import ShipmentRecord
    from translog_quote.domain.validation import ValidationResult
    from translog_quote.domain.workflow import QuotationRequest
    from translog_quote.interface.jobs import RateSearchJobResult
    from translog_quote.interface.web.demonstration import Demonstration
    from translog_quote.interface.web.multi_account_session import MultiAccountSession
    from translog_quote.interface.web.redis_state import RedisAuditLog
    from translog_quote.pipeline import QuotationStage
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

#: How long a message whose extraction the provider refused *permanently* (a
#: spent or rejected API key) is held back before it is tried again. Long enough
#: that a poll every few seconds cannot hammer a refusing provider; short enough
#: that restoring the key is picked up without a restart.
EXTRACTION_RETRY_AFTER = timedelta(minutes=15)

#: How long a message whose automatic client email (failure notice, reminder)
#: could not be sent is held back before it is handled again. Held whether the
#: send failure was transient or permanent: the message was already extracted,
#: and handling it again means paying for that extraction again, so retrying on
#: every poll would turn one Gmail outage into a stream of paid model calls.
OUTBOUND_RETRY_AFTER = timedelta(minutes=15)

#: The operator-facing reason when a door-delivery request got rates back, but
#: every one was excluded only because it does not confirm door delivery. The
#: airport-to-airport rates are real; the door leg is not something Translog can
#: price or promise, so a person decides. Nothing is sent to the client.
DOOR_LEG_NOTE = "Port/airport rates available, door leg needs manual pricing."

#: Plain wording for each exclusion reason, for the hand-over note shown to the
#: operator (never the internal enum value).
_EXCLUSION_WORDING: dict[ExclusionReason, str] = {
    ExclusionReason.INCOMPLETE_RATE: "missing a price or currency",
    ExclusionReason.UNRANKABLE_NO_TRANSIT: "no transit time to rank by",
    ExclusionReason.CARRIER_RESTRICTED: "a carrier restriction",
    ExclusionReason.SERVICE_NOT_AVAILABLE: "the requested service is not offered",
}

_log = get_logger("interface.web.live_session")


def _namespaced_request_id_for(account_id: str) -> Callable[[RawEmail], str]:
    """A request-id factory that prefixes ``_request_id_for`` with the account id,
    so ids are globally unique across the mailboxes and route back to their owner
    (``account_id`` is a validated slug and never contains ``:``). Single-account
    sessions keep the unprefixed ``_request_id_for``, so their ids are unchanged.
    """

    def _new_id(email: RawEmail) -> str:
        return f"{account_id}:{_request_id_for(email)}"

    return _new_id


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
    rate_job_id: str | None = None
    """The queued browser rate-search job serving this request, once enqueued.

    Set on the first poll that enqueues, and reused by every later poll so the
    job is polled rather than re-submitted (idempotency is also guaranteed
    queue-side, but this avoids even asking). ``None`` in demo/mock mode, which
    search synchronously and never touch the queue."""

    rate_failure: str | None = None
    """Why rate search could not run for this request, if it could not.

    Set instead of the search result, never alongside it: a request either has
    rates or has a reason it has none. It is a *report*, not a state — the
    request stays where the state machine put it, so the next poll retries it
    for free once the cause is fixed.
    """

    awaiting_goods_type: bool = False
    """The goods-type rule could not decide General Cargo and no operator pick
    applies: the request is held (state stays VALIDATED) for an operator to pick
    an exact WebCargo Goods Type. A presentation hold, not a domain state —
    re-derived from the record on every poll, like a location clarification."""

    operator_goods_type: str | None = None
    """An operator's chosen WebCargo Goods Type label, persisted so a restart
    does not discard it. Applied only while ``operator_goods_type_fingerprint``
    still matches the record's cargo facts."""
    operator_goods_type_fingerprint: str | None = None
    """The fingerprint of (commodity, cargo_type, is_chemical) the pick was made
    against. If the record changes (e.g. a client reply) the fingerprint changes
    and the pick is discarded — never applied to a different shipment."""
    operator_goods_type_by: str | None = None
    """The operator who picked ``operator_goods_type``, recorded on the audit
    event when the search enqueues under their choice."""

    packet: ReviewPacket | None = None
    decision: ApprovalDecision | None = None
    quotation_sent: bool = False
    final_reply_sent: bool = False
    """A terminal client reply has been sent for a request that could not be
    fulfilled — currently the "no eligible rate" notice. In-memory guard against
    notifying twice within a run; the persisted ``CLOSED_NO_RATES`` state is the
    cross-restart guard (see ``_notify_no_rates``)."""
    messages: list[str] = field(default_factory=list)

    history: bool = False
    """Restored from the store in a terminal state: shown as history, hidden by
    default in the live view, and never advanced by the poll or the rate pass."""

    restored: bool = False
    """Rebuilt from the durable store on startup rather than seen live this
    session. Cleared the first time the request acts, so a re-enqueue a restart
    triggers is audited as a post-restart re-run exactly once."""

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
    def rate_search_pending(self) -> bool:
        """A queued browser rate search is in flight for this request.

        A presentation state, not a domain one: the request stays VALIDATED
        while the job runs. True only in browser mode, between enqueue and the
        job reaching a result — so the dashboard can show "Searching WebCargo…"
        instead of an empty rate panel that reads as a stall.
        """
        return (
            self.state is RequestState.VALIDATED
            and self.rate_job_id is not None
            and self.rates is None
            and self.rate_failure is None
        )

    @property
    def is_settled(self) -> bool:
        return self.state in {
            RequestState.QUOTATION_SENT,
            RequestState.MAKER_REJECTED,
            RequestState.CLOSED_NO_RATES,
        }


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
        account: GmailAccount | None = None,
        source: EmailSource | None = None,
        sink: EmailSink | None = None,
        extractor: ExtractionPort | None = None,
        durable: StorePort | None = None,
        clock: ClockPort | None = None,
        audit: CollectingAudit | None = None,
        resolver: LocationResolverPort | None = None,
    ) -> None:
        self._settings = settings
        # The mailbox this session speaks for, or None in the single-account
        # deployment. Kept so a later aggregator can tell which account owns a
        # request; nothing else here branches on it beyond choosing this
        # account's mailbox, credentials and state directory below.
        self.account = account
        account_id = account.account_id if account is not None else None
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
        self.audit: CollectingAudit | JsonFileAuditLog | RedisAuditLog = (
            audit or build_audit_log(settings, account_id=account_id)
        )

        self._durable = (
            durable
            if durable is not None
            else bootstrap.build_persistent_store(settings, account_id=account_id)
        )
        self._working = bootstrap.build_memory_store()
        bootstrap.seed_store(self._working, self._durable)

        # Built before anything is read: a broken send credential should stop
        # the session here, not after a client's mail has been processed.
        # When there is no account, call the builder exactly as before (no
        # ``account`` keyword) so existing stubs of it are unaffected; pass the
        # account only when there is one.
        if sink is not None:
            self._sink = sink
        elif account is None:
            self._sink = bootstrap.build_gmail_email_sink(settings)
        else:
            self._sink = bootstrap.build_gmail_email_sink(settings, account=account)
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
            new_request_id=(
                _request_id_for
                if account is None
                else _namespaced_request_id_for(account.account_id)
            ),
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
        self._demonstration = build_demonstration(settings, account_id=account_id)
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
        # Messages whose extraction the provider refused permanently (e.g. a spent
        # key, HTTP 402), mapped to when they may be tried again. In memory on
        # purpose, like an uncommitted draft: they stay unrecorded, so the
        # watermark holds at them and a restart simply tries each once more.
        self._extraction_hold: dict[str, datetime.datetime] = {}
        self._extraction_failed_this_poll = False
        """The class of the last failed poll, or None. Written by whatever
        drives the polling — the background poller does, and clears it on the
        next success — so an unreachable mailbox is visible, not silent."""
        # Messages whose automatic client email could not be sent, mapped to when
        # they may be handled again. In memory for the same reason as above.
        self._outbound_hold: dict[str, datetime.datetime] = {}

        self.requests: dict[str, LiveRequest] = {}
        #: Browser-worker liveness, refreshed each poll so the render path reads a
        #: cached value and never blocks on Redis. "unknown" until the first poll.
        self.worker_status: str = WORKER_UNKNOWN
        self._restore()

    # ------------------------------------------------------------- actions --

    def poll(self) -> None:
        """Read the mailbox and process whatever is new. Sends nothing.

        Stops at a held clarification rather than pushing past it: the
        transition table permits no way out of NEEDS_INFO except
        CLARIFICATION_SENT, so a reply cannot be processed until the question
        it answers has actually gone out — and only a person can send it.
        """
        # This poll's own verdict. A raise below leaves it for the caller to set;
        # an extraction the provider refused sets it at the end, so an outage the
        # poll survived is still reported rather than shown as a healthy desk.
        self.last_poll_error = None
        self._extraction_failed_this_poll = False
        received = self._fetch(since=self._mail_cutoff())
        client_mail = [email for email in received if not _is_internal(email)]
        self.skipped_internal = len(received) - len(client_mail)

        # Messages older than the demonstration are history, not this
        # presentation. Left unread rather than read-and-hidden: extracting a
        # year of newsletters to then not show them would cost a live model
        # call each and make the first poll unusable. Operations mode does not
        # filter here — its date-bounded fetch already scoped the read, and the
        # durable already-processed check below de-duplicates the overlap.
        in_scope = [e for e in client_mail if self._covers(e.received_at)]
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
            if self._extraction_held(email.message_id) or self._outbound_held(email.message_id):
                # Refused permanently a moment ago; asking again every poll would
                # only spend the key (or fail identically) until someone fixes
                # the provider side. Unrecorded, so the watermark still holds.
                continue
            try:
                self._route(email)
            except ExtractionUnavailable as exc:
                # The model could not be called for *this* message. It is left
                # unrecorded — not consumed, not a request — so the watermark
                # holds at it and a later poll retries it; everything after it in
                # this poll, the rate searches and the watermark still run.
                self._hold_after_extraction_failure(email, exc)
            except OutboundUnavailable as exc:
                # The automatic client email for *this* message could not be
                # sent. Isolated the same way: unrecorded (watermark holds), held
                # so its paid extraction is not repeated every poll, and the rest
                # of this poll still runs.
                self._hold_after_outbound_failure(email, exc)
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

        # An extraction the provider refused did not stop this poll, but it is
        # still an outage the desk must see: report it the way a failed poll was
        # always reported, for as long as any message is held back because of it.
        now = self._clock.now()
        self._extraction_hold = {mid: t for mid, t in self._extraction_hold.items() if t > now}
        if self._extraction_hold or self._extraction_failed_this_poll:
            self.last_poll_error = ExtractionUnavailable.__name__
        self._outbound_hold = {mid: t for mid, t in self._outbound_hold.items() if t > now}
        if self._outbound_hold and self.last_poll_error is None:
            self.last_poll_error = OutboundUnavailable.__name__

        # Hand over any request whose 30-minute follow-up window has lapsed with
        # no usable reply. Time-triggered, so it catches a client who sent one
        # non-answer and then went silent — no further mail would ever reach the
        # clarification loop for them. Runs every poll, before rate search, so an
        # expired request is escalated rather than left waiting.
        self._escalate_expired_followups()
        self._search_rates_for_validated()
        # Refresh the browser-worker liveness here, on the poll path, so the
        # frequent render path (GET /api/live/state) reads a cached value and
        # never makes a Redis round-trip. Browser mode only; bounded and total
        # (returns "unknown" on any Redis error, never raises).
        if self._settings.webcargo.mode is WebCargoMode.BROWSER:
            self.worker_status = worker_liveness(self._settings)
        self.last_poll_at = self._clock.now()
        # Only now, after a poll that read the mailbox and routed without
        # raising, is it safe to advance the persisted mail cutoff. A failed
        # fetch raises before here, so the watermark never moves past mail a
        # broken poll did not actually read.
        if self.operations_mode:
            self._advance_watermark(in_scope)

    def _covers(self, received_at: datetime.datetime) -> bool:
        """Whether a message that arrived then is in scope this poll.

        Operations mode trusts the date-bounded fetch and the durable
        already-processed check: everything returned is in scope, and duplicates
        are dropped downstream. Demonstration mode hides mail older than the
        cutoff, unchanged."""
        if self.operations_mode:
            return True
        cutoff = self._mail_cutoff()
        return cutoff is None or received_at >= cutoff

    def _advance_watermark(self, in_scope: list[RawEmail]) -> None:
        """Persist the mail cutoff, never past a message not durably committed.

        A message is *settled* only when its id is recorded in a durable thread
        (``commit_request``/``commit_thread`` write it); a NEEDS_INFO draft and a
        deferred reply commit nothing, so they stay uncommitted and the cutoff
        must not pass them — a restart re-reads and re-derives them. When
        everything handled this poll is settled the cutoff advances to the newest
        handled; otherwise it holds at the oldest uncommitted message.

        An unresolved NEEDS_INFO draft is now *skipped* by the fetch once it has
        been handled this run (so it stops starving newer mail), which means it
        is no longer in ``in_scope`` to pin the cutoff on its own. Its enquiry is
        added back to the uncommitted set here, from the live requests, so the
        watermark still never advances past it — the invariant is preserved by
        holding at the request, not by re-reading it every poll."""
        if not in_scope:
            return
        previous = self._demonstration.current.last_poll_watermark
        committed = {mid for thread in self._durable.all_threads() for mid in thread.message_ids}
        uncommitted = [e.received_at for e in in_scope if e.message_id not in committed]
        uncommitted += [
            request.enquiry.received_at
            for request in self.requests.values()
            if request.state is RequestState.NEEDS_INFO
            and request.enquiry is not None
            and request.enquiry.message_id not in committed
        ]
        if uncommitted:
            watermark = min(uncommitted)
        else:
            newest = max(e.received_at for e in in_scope)
            watermark = newest if previous is None else max(previous, newest)
        if watermark != previous:
            self._demonstration.record_watermark(watermark)

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

    def resume_operations(self) -> None:
        """Resume operations after a restart: no fresh demonstration.

        Requests were already restored from the store in ``__init__``. This
        settles the mail cutoff. If a watermark was persisted by an earlier
        poll, it stands. If none exists yet — a fresh disk, or the first deploy
        after this change — it is seeded from ``operations_since``, and the
        server **refuses to start** if that is missing rather than silently
        defaulting the cutoff to ``now`` and skipping every earlier message."""
        if self._demonstration.current.last_poll_watermark is not None:
            return
        since = self._settings.demo.operations_since
        if since is None:
            raise PermanentFailure(
                "Operations mode has no mail cutoff yet. Set TRANSLOG_DEMO__OPERATIONS_SINCE "
                "to an ISO datetime (e.g. 2026-09-16T00:00:00+00:00) — the instant from which "
                "mail should be read — so the first poll does not silently skip earlier mail. "
                "It is used only until the first successful poll records a watermark."
            )
        self._demonstration.record_watermark(since)

    @property
    def demonstration(self) -> Demonstration:
        return self._demonstration.current

    def in_demonstration(self, request_id: str) -> bool:
        """Whether this request is one the current demonstration follows."""
        return self._demonstration.current.focuses(request_id)

    @property
    def operations_mode(self) -> bool:
        """Whether a restart resumes rather than starting a fresh demonstration."""
        return self._settings.demo.startup_mode == "operations"

    def _mail_cutoff(self) -> datetime.datetime | None:
        """The instant before which inbound mail is out of scope.

        Demonstration mode: the demonstration's ``started_at`` (``now`` at boot),
        so history stays out of the room's view. Operations mode: the persisted
        watermark — the last successful poll — so mail that arrived during a
        deploy is still read. ``None`` means no bound (a fresh demonstration
        session that has started nothing, unchanged from before)."""
        if self.operations_mode:
            return self._demonstration.current.last_poll_watermark
        return self._demonstration.current.started_at

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

    def decide_goods_type(self, request_id: str, *, goods_type: str, by: str) -> None:
        """Record an operator's Goods Type pick for a held request.

        Takes the operator identity exactly the way ``decide`` does — a named
        person, no default. The label must be one the catalog offers (the
        configured general-cargo label is always a member); anything else is
        refused. The pick is persisted with a fingerprint of the record's cargo
        facts, so a later poll enqueues under it (auditing source=operator, by)
        and a restart applies it without asking again — unless the record has
        since changed, when it is discarded.
        """
        from translog_quote.domain.goods_type import effective_catalog, record_fingerprint

        who = by.strip()
        if not who:
            raise LiveSequenceError("A goods-type decision must be made by a named person.")
        request = self.requests.get(request_id)
        if request is None:
            raise LiveSequenceError(f"There is no request {request_id}.")
        goods = self._settings.goods_type
        if goods_type not in effective_catalog(goods.catalog, goods.general_cargo_label):
            raise LiveSequenceError(
                f"{goods_type!r} is not a Goods Type an operator may pick for this desk."
            )
        record = request.record
        fingerprint = record_fingerprint(
            record.commodity or "", record.cargo_type, record.is_chemical
        )
        request.operator_goods_type = goods_type
        request.operator_goods_type_fingerprint = fingerprint
        request.operator_goods_type_by = who
        request.awaiting_goods_type = False
        self._persist_goods_type_pick(request_id, goods_type, fingerprint, who)

    def _persist_goods_type_pick(
        self, request_id: str, label: str, fingerprint: str, by: str
    ) -> None:
        """Write an operator's pick onto the stored request and commit it, so a
        restart applies it. Best-effort on a request the store never had."""
        stored = self._working.get_request(request_id)
        if stored is None:
            return
        self._working.save_request(
            stored.model_copy(
                update={
                    "operator_goods_type": label,
                    "operator_goods_type_fingerprint": fingerprint,
                    "operator_goods_type_by": by,
                }
            )
        )
        bootstrap.commit_request(self._working, self._durable, request_id)

    def goods_type_hold_options(self) -> tuple[list[str], bool]:
        """The catalog labels an operator may pick on a hold, and whether the
        business has configured any beyond the always-present general-cargo
        label. When not configured the UI shows 'goods-type catalog not
        configured' rather than a one-option picker."""
        from translog_quote.domain.goods_type import catalog_configured, effective_catalog

        goods = self._settings.goods_type
        configured = catalog_configured(goods.catalog, goods.general_cargo_label)
        options = (
            list(effective_catalog(goods.catalog, goods.general_cargo_label)) if configured else []
        )
        return options, configured

    def _clear_goods_type_pick(self, request_id: str) -> None:
        """Drop a persisted pick (a record change invalidated it), so it does not
        re-appear — and re-discard — after a restart."""
        stored = self._working.get_request(request_id)
        if stored is None or stored.operator_goods_type is None:
            return
        self._working.save_request(
            stored.model_copy(
                update={
                    "operator_goods_type": None,
                    "operator_goods_type_fingerprint": None,
                    "operator_goods_type_by": None,
                }
            )
        )
        bootstrap.commit_request(self._working, self._durable, request_id)

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

    def _fetch(self, *, since: datetime.datetime | None = None) -> tuple[RawEmail, ...]:
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
            # No ``account`` keyword when single-account, so existing stubs of
            # this builder keep working; account-scoped only when there is one.
            if self.operations_mode:
                # Operations reads a date-bounded window oldest-first, so the
                # ceiling is the larger per-poll fetch budget, the overlap lets
                # the boundary be re-listed safely, and internal approval mail
                # never spends the client budget.
                if self.account is None:
                    self._source = bootstrap.build_gmail_email_source(
                        self._settings,
                        max_results=self._settings.gmail.fetch_cap,
                        overlap_seconds=self._settings.demo.fetch_overlap_minutes * 60.0,
                        is_internal=_is_internal,
                        sent_by_us=self._sent_provider_ids,
                        seen=self._seen_message,
                    )
                else:
                    self._source = bootstrap.build_gmail_email_source(
                        self._settings,
                        account=self.account,
                        max_results=self._settings.gmail.fetch_cap,
                        overlap_seconds=self._settings.demo.fetch_overlap_minutes * 60.0,
                        is_internal=_is_internal,
                        sent_by_us=self._sent_provider_ids,
                        seen=self._seen_message,
                    )
            elif self.account is None:
                self._source = bootstrap.build_gmail_email_source(
                    self._settings, max_results=MESSAGE_LIMIT, sent_by_us=self._sent_provider_ids
                )
            else:
                self._source = bootstrap.build_gmail_email_source(
                    self._settings,
                    account=self.account,
                    max_results=MESSAGE_LIMIT,
                    sent_by_us=self._sent_provider_ids,
                )
        return self._source.fetch_new(since=since)

    def _sent_provider_ids(self) -> Collection[str]:
        """Provider ids of everything this session has delivered.

        Empty for a sink that does not track them — the collecting sink used in
        tests, and any future one — so the filter simply does nothing rather
        than requiring every sink to implement it.
        """
        ids = getattr(self._sink, "sent_provider_ids", None)
        return ids if isinstance(ids, set | frozenset | tuple | list) else ()

    def _seen_message(self, message_id: str) -> bool:
        """Whether this session has already handled a message.

        Two sources, matching the poll's own freshness test above: durably
        committed (survives a restart) or routed earlier this run. Used by the
        operations fetch to skip an already-handled message so the oldest-first
        budget reaches newer mail. Skipping it there is safe precisely because
        an unresolved draft is still pinned by ``_advance_watermark`` — the
        message stops being *re-fetched*, not stops being *waited for*.
        """
        return message_id in self._routed or self._router.already_processed(message_id)

    def _extraction_held(self, message_id: str) -> bool:
        """Whether this message is inside its hold after a permanent refusal."""
        until = self._extraction_hold.get(message_id)
        if until is None:
            return False
        if self._clock.now() >= until:
            del self._extraction_hold[message_id]
            return False
        return True

    def _hold_after_extraction_failure(self, email: RawEmail, exc: ExtractionUnavailable) -> None:
        """Record one message's extraction failure without consuming it.

        A permanent refusal (a spent or rejected key) is held back from the model
        for `EXTRACTION_RETRY_AFTER`, so a poll every few seconds cannot turn one
        stuck message into a stream of paid or pointless calls. A transient one
        (a timeout, a 5xx the transport already retried) is simply tried again
        next poll, as before."""
        self._extraction_failed_this_poll = True
        if exc.permanent:
            self._extraction_hold[email.message_id] = self._clock.now() + EXTRACTION_RETRY_AFTER
        _log.warning(
            "Extraction unavailable for message %s (%s, retry %s): %s",
            email.message_id,
            "permanent" if exc.permanent else "transient",
            f"after {EXTRACTION_RETRY_AFTER}" if exc.permanent else "next poll",
            exc,
        )

    def _outbound_held(self, message_id: str) -> bool:
        """Whether this message is inside its hold after a failed automatic send."""
        until = self._outbound_hold.get(message_id)
        if until is None:
            return False
        if self._clock.now() >= until:
            del self._outbound_hold[message_id]
            return False
        return True

    def _hold_after_outbound_failure(self, email: RawEmail, exc: OutboundUnavailable) -> None:
        """Record one message's failed automatic send without consuming it.

        Held for `OUTBOUND_RETRY_AFTER` whatever the failure's kind: the message
        was already extracted, so handling it again pays for that extraction
        again. Unrecorded, so the watermark holds at it and a restart simply
        handles it once more."""
        self._outbound_hold[email.message_id] = self._clock.now() + OUTBOUND_RETRY_AFTER
        _log.warning(
            "Automatic client email not sent for message %s (%s, retry after %s): %s",
            email.message_id,
            "permanent" if exc.permanent else "transient",
            OUTBOUND_RETRY_AFTER,
            exc,
        )

    def _route(self, email: RawEmail) -> None:
        routed = self._router.route(email)
        if routed.ignored and routed.request_id is not None:
            # Unrelated mail (bulk/automated sender, or a first-contact message
            # that stated no shipment detail). The router recorded it as seen;
            # settle that durably so the operations watermark advances past it
            # and it is never re-examined — but create no request and show
            # nothing. This is the whole "only Translog mail enters" guarantee.
            bootstrap.commit_thread(self._working, self._durable, routed.request_id)
            return
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
        """Advance rate search for any request that has just validated.

        Not a gate and not a send: filtering and ranking are deterministic and
        the gate is the next step, and it is a person's. *How* the candidate
        rates are obtained depends on the configured provider:

        - demo / mock: searched synchronously in-process, exactly as before —
          simulated rates, disclosed as such.
        - browser: the real WebCargo search runs only inside the browser
          worker, so this session enqueues a job on the shared queue and polls
          it across successive calls. It never builds a browser provider itself
          (that owns the one authenticated session) and never falls back to
          simulated data — a browser-mode failure is reported, not papered over.
        """
        browser_mode = self._settings.webcargo.mode is WebCargoMode.BROWSER
        stage = (
            None
            if browser_mode
            else RateSearchStage(
                provider=bootstrap.build_demo_rate_provider(),
                resolver=self._resolver,
                strategy=FASTEST_ELIGIBLE,
                audit=self.audit,
                clock=self._clock,
            )
        )
        for request in self.requests.values():
            if request.rates is not None or request.state is not RequestState.VALIDATED:
                continue
            # AMB-8: the search runs for the client's stated shipment date.
            # VR-12 makes that date required to validate, so a VALIDATED
            # request has one — the guard is defensive, not expected to fire,
            # and it fails loudly rather than inventing a date.
            if request.record.ship_date is None:
                request.rate_failure = (
                    "No shipment date on a validated request; a rate search "
                    "cannot run without the client's stated shipment date."
                )
                continue
            # VR-13, enforced where it matters: a past departure date must never
            # reach WebCargo. Extraction rolls a past date forward before merge,
            # so this catches only a record that predates that fix or a date that
            # has passed while the request waited. Loud, never a silent re-date.
            # Only before enqueue: a job already in flight is left to finish.
            today = self._clock.now().date()
            if request.rate_job_id is None and request.record.ship_date < today:
                request.rate_failure = (
                    f"The shipment date {request.record.ship_date.isoformat()} is in "
                    "the past; a rate search will not run for a past date. Confirm a "
                    "current shipment date with the client."
                )
                continue
            # Resolve the stated places before any enqueue. Pure, deterministic,
            # no I/O — safe in the web process. A place that cannot be resolved
            # to an airport without guessing becomes a client clarification, not
            # a search that would only fail worker-side. Only for requests not
            # yet enqueued; an in-flight job is polled by the browser path below,
            # and a job that FAILED on an unresolved place is recovered there
            # (Change 5).
            if request.rate_job_id is None and self._draft_location_clarification_if_unresolved(
                request
            ):
                continue
            if browser_mode:
                self._advance_browser_rate_search(request)
            else:
                assert stage is not None  # noqa: S101 - non-browser branch always builds one
                self._run_sync_rate_search(request, stage)

    def _run_sync_rate_search(self, request: LiveRequest, stage: RateSearchStage) -> None:
        """Search, filter and rank in-process for demo/mock. Unchanged behaviour."""
        try:
            outcome = stage.run(
                request.request_id,
                request.record,
                on_date=request.record.ship_date,  # type: ignore[arg-type]
                cargo_is_liquid=None,  # AMB-3: stated, never derived
            )
        except TranslogError as exc:
            # One request that cannot be priced is one request that cannot be
            # priced. Caught narrowly: `UnresolvedLocation` and its kin are one
            # enquiry's problem, and the state stays VALIDATED with `rates`
            # None so the next poll retries for free. Anything outside the
            # taxonomy is a defect and still escapes loudly.
            request.rate_failure = str(exc)
            _log.warning("Rate search failed for %s: %s", request.request_id, exc)
            return
        self._apply_rate_outcome(request, outcome)

    def _advance_browser_rate_search(self, request: LiveRequest) -> None:
        """Enqueue (once) and then poll the queued WebCargo job for one request.

        Idempotent across polls: the first call submits and records the job id,
        every later call polls it. On completion the request is hydrated from
        the worker's own filtered/selected result — the domain selection is not
        re-run here. A failure is recorded on the request and never replaced by
        simulated data.
        """
        from redis.exceptions import RedisError

        if request.rate_job_id is None:
            # Decide the WebCargo Goods Type BEFORE enqueue: the reviewed General
            # Cargo rule, or an operator's persisted pick. If neither decides,
            # hold for an operator — never a silent default, and never the
            # client's free-text commodity typed into the controlled select.
            goods_type, source = self._resolve_goods_type(request)
            if goods_type is None:
                request.awaiting_goods_type = True  # held; the operator picks
                return
            request.awaiting_goods_type = False
            try:
                job = self._job_request_from_record(request, goods_type=goods_type)
            except (ContractViolation, ValueError) as exc:
                request.rate_failure = f"Could not build the rate-search request: {exc}"
                _log.warning("Rate-search request invalid for %s: %s", request.request_id, exc)
                return
            try:
                job_id, _created = enqueue_rate_search(job, self._settings)
            except RedisError as exc:
                # The queue being down is transient: leave no job id, so the
                # next poll retries the enqueue rather than polling nothing.
                request.rate_failure = "the rate-search queue is unavailable; retry shortly"
                _log.warning("Could not enqueue rate search for %s: %s", request.request_id, exc)
                return
            self._emit_goods_type(
                request.request_id, goods_type, source, by=request.operator_goods_type_by
            )
            if request.restored:
                # This request came back from the store on startup and its rate
                # search is being re-run because the result was never persisted.
                # Audited once, so the trail shows the re-enqueue was a restart
                # recovery rather than fresh client work; then cleared.
                self._emit_rerun_after_restart(request.request_id)
                request.restored = False
            request.rate_job_id = job_id
            request.rate_failure = None
            return  # the first result arrives on a later poll

        try:
            status = fetch_job_status(request.rate_job_id, self._settings)
        except RedisError as exc:
            request.rate_failure = "the rate-search queue is unavailable; retry shortly"
            _log.warning("Could not read rate-search job for %s: %s", request.request_id, exc)
            return

        if status is None:
            # The job's result TTL lapsed (or it was never created). Forget it
            # so the next poll enqueues afresh rather than polling a ghost.
            request.rate_job_id = None
            return
        if status.state is JobState.COMPLETED and status.result is not None:
            self._apply_rate_outcome(
                request, self._outcome_from_job_result(request.request_id, status.result)
            )
        elif status.state is JobState.FAILED:
            # A place that cannot be resolved to an airport without guessing is a
            # client clarification, not a seven-day dead end. Re-run the resolver
            # (pure, no I/O) on the record: if a stated place still cannot
            # resolve, draft the clarification and drop the failed job. Matching
            # is by re-resolving, not by the error text/type, because the worker
            # also raises UnresolvedLocation for a WebCargo autocomplete miss on
            # a place that DID resolve to a code — that is a genuine search
            # failure and must stay visible to the operator, never emailed.
            unresolved = self._unresolved_places(request.record)
            if unresolved:
                request.rate_job_id = None
                self._draft_location_clarification(request, unresolved)
            else:
                # Fail loudly and stay failed: the failed job is retained under
                # its own TTL and is not auto re-enqueued, so a broken search
                # does not hammer the single browser worker on every poll. The
                # operator sees a plain message, not the raw exception class.
                request.rate_failure = _plain_failure(status.error)
        else:
            # QUEUED / STARTED: in flight. Clear any stale failure so a job the
            # worker REQUEUED after a session loss (it briefly showed FAILED, and
            # is now QUEUED again) reads as pending, not failed — a request must
            # never keep a stale failure for a job that is back on the queue.
            request.rate_failure = None

    def _apply_rate_outcome(self, request: LiveRequest, outcome: RateSearchOutcome) -> None:
        """Record a finished rate outcome and build the approval packet if any.

        A selection opens the human approval gate. A search that found nothing
        usable (``NO_ELIGIBLE_RATE``) splits on whether the provider returned any
        rows at all. Zero rows is a genuinely empty market: the client is sent a
        "no rates" notice and the request moves to ``CLOSED_NO_RATES`` — see
        ``_notify_no_rates`` for the send/persist ordering and its crash window.
        Rows that came back but were all excluded are not "no rates" — see
        ``_hand_over_returned_rates``.
        """
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
        elif outcome.state is RequestState.NO_ELIGIBLE_RATE:
            if outcome.returned == 0:
                self._notify_no_rates(request)
            else:
                self._hand_over_returned_rates(request, outcome)

    def _hand_over_returned_rates(self, request: LiveRequest, outcome: RateSearchOutcome) -> None:
        """Rates came back, none is eligible: hand to a person, email nobody.

        Telling the client "we are unable to source a rate" would be untrue when
        the provider returned rates, and a filter or mapping gap would then reach
        the client as a thin market. The main case is door delivery: WebCargo's
        results do not state door capability, so every airport-to-airport rate is
        excluded for an unconfirmed door leg (``drop_service_mismatch`` runs last,
        so that exclusion means the rate was otherwise usable).

        The returned rates stay on the request for the operator to see; no
        approval packet is built, so nothing can be quoted from here. The
        request moves VALIDATED -> MANUAL_REVIEW (an existing edge) and is
        committed durably, so a restart neither re-runs the search nor emails.
        Rates are not part of the durable record, so a compact summary goes into
        the audit trail with the reason.
        """
        excluded = outcome.filtered.excluded
        door_leg_only = bool(excluded) and all(
            e.reason is ExclusionReason.SERVICE_NOT_AVAILABLE
            and e.rate.restrictions.serves_door_delivery is None
            for e in excluded
        )
        if door_leg_only:
            note = DOOR_LEG_NOTE
            reason = "door_leg_needs_manual_pricing"
        else:
            why = sorted({_EXCLUSION_WORDING[e.reason] for e in excluded})
            note = (
                f"WebCargo returned {outcome.returned} rate(s) but none could be used "
                f"automatically ({'; '.join(why) or 'no reason recorded'}). "
                "Handed to a person; no client email was sent."
            )
            reason = "returned_rates_all_excluded"

        request.state = RequestState.MANUAL_REVIEW
        request.manual_review_notes = (note,)
        request.packet = None

        from translog_quote.pipeline.audit import AuditEvent, AuditEventType

        self.audit.record(
            AuditEvent(
                request_id=request.request_id,
                event=AuditEventType.MANUAL_REVIEW_ESCALATED,
                at=self._clock.now(),
                detail={
                    "reason": reason,
                    "notes": [note],
                    "returned": outcome.returned,
                    "rates": [
                        {
                            "carrier": e.rate.carrier_name,
                            "total": None
                            if e.rate.total_amount is None
                            else str(e.rate.total_amount),
                            "currency": e.rate.currency,
                            "excluded": e.reason.value,
                        }
                        for e in excluded
                    ],
                },
            )
        )
        stored = self._working.get_request(request.request_id)
        if stored is not None:
            # The note is persisted with the state, so after a restart the
            # Active hand-over still says why. The rates themselves are not.
            self._working.save_request(
                stored.model_copy(
                    update={
                        "state": RequestState.MANUAL_REVIEW,
                        "manual_review_notes": (note,),
                    }
                )
            )
            bootstrap.commit_request(self._working, self._durable, request.request_id)

    def _escalate_expired_followups(self) -> None:
        """Move any request past its 30-minute follow-up deadline to a person.

        The decision and the durable write live in the clarification workflow
        (``sweep_followup_deadlines``, driven through the router, against this
        session's own store and clock — so in a multi-account run each mailbox
        sweeps only its own requests). This mirrors each hand-over into the
        interface's view and commits it to the durable store, the same two-step
        the no-rates notice uses, so the escalation survives a restart and shows
        the operator why.
        """
        from translog_quote.pipeline.clarification_loop import FOLLOWUP_WINDOW_EXPIRED_NOTE

        for request_id in self._router.sweep_followup_deadlines():
            live = self.requests.get(request_id)
            if live is not None:
                live.state = RequestState.MANUAL_REVIEW
                live.clarification = None
                live.manual_review_notes = (FOLLOWUP_WINDOW_EXPIRED_NOTE,)
            bootstrap.commit_request(self._working, self._durable, request_id)

    def _notify_no_rates(self, request: LiveRequest) -> None:
        """Email the client that no rate could be sourced, and close the request.

        Scoped strictly to ``NO_ELIGIBLE_RATE`` (a search that ran and found
        nothing usable). Location-resolution failures, missing fields and
        conflicts never reach here — they are still handled by the clarification
        path before any search.

        Idempotency and the crash window (deliberate, documented):
        - In-memory: ``final_reply_sent`` / already-``CLOSED_NO_RATES`` stops a
          second send when the same finished job is re-applied on a later poll.
        - Across a restart: rate outcomes are NOT persisted, so a restored
          request re-runs its search — the durable ``CLOSED_NO_RATES`` state,
          committed here right after the send, is what stops it re-notifying.
        - Residual window: the send and the commit are two writes with no
          transaction between them (the same shape ``QuotationStage`` has). A
          crash strictly between ``_sink.send`` returning and ``commit_request``
          completing leaves the notice sent but the durable state still
          VALIDATED, so the restart re-runs and sends a SECOND notice. This is
          an at-least-once guarantee, accepted here rather than re-architected:
          a duplicate apology is the benign failure, and it is not bounded by a
          human gate the way the quotation send's identical window is.
        """
        if request.final_reply_sent or request.state is RequestState.CLOSED_NO_RATES:
            return
        if not request.client_address:
            # Nothing to send to: leave it at NO_ELIGIBLE_RATE (non-terminal) so
            # the healthcheck surfaces it for a person rather than closing it
            # as "client notified" when no client was.
            _log.warning(
                "No client address on %s; leaving at NO_ELIGIBLE_RATE without a notice.",
                request.request_id,
            )
            return
        self._sink.send(
            no_rates_message(
                request.record,
                reference=request.request_id,
                to_address=request.client_address,
                in_reply_to=request.last_message_id,
            )
        )
        request.final_reply_sent = True
        request.state = RequestState.CLOSED_NO_RATES
        self._emit_no_rates_notice(request.request_id)
        self._persist_closed_no_rates(request.request_id)

    def _emit_no_rates_notice(self, request_id: str) -> None:
        """Record that a "no eligible rate" notice was sent to the client."""
        from translog_quote.pipeline.audit import AuditEvent, AuditEventType

        self.audit.record(
            AuditEvent(
                request_id=request_id,
                event=AuditEventType.NO_RATES_NOTICE_SENT,
                at=self._clock.now(),
                detail={},
            )
        )

    def _persist_closed_no_rates(self, request_id: str) -> None:
        """Commit ``CLOSED_NO_RATES`` to the durable store immediately after the
        notice is sent, so a restart does not re-run the search and re-notify."""
        stored = self._working.get_request(request_id)
        if stored is None:
            return
        self._working.save_request(
            stored.model_copy(update={"state": RequestState.CLOSED_NO_RATES})
        )
        bootstrap.commit_request(self._working, self._durable, request_id)

    def _unresolved_places(self, record: ShipmentRecord) -> tuple[UnresolvedPlace, ...]:
        """The stated origin/destination the resolver cannot turn into an airport
        code without guessing. Pure and deterministic — ``resolve`` does no I/O,
        so this is safe in the web process."""
        places: list[UnresolvedPlace] = []
        for field_name, stated in (
            (FieldName.ORIGIN, record.origin),
            (FieldName.DESTINATION, record.destination),
        ):
            if not stated:
                continue
            try:
                self._resolver.resolve(stated)
            except UnresolvedLocation:
                places.append(UnresolvedPlace(field=field_name, stated=stated))
        return tuple(places)

    def _draft_location_clarification_if_unresolved(self, request: LiveRequest) -> bool:
        """Draft a location clarification when a stated place cannot be resolved.

        Returns ``True`` when a draft was created, so the caller does not
        enqueue. In simulated modes ``StatedLocationResolver`` does not raise for
        a nameable place, so nothing is found and this returns ``False`` —
        behaviour there is unchanged.
        """
        unresolved = self._unresolved_places(request.record)
        if not unresolved:
            return False
        self._draft_location_clarification(request, unresolved)
        return True

    def _draft_location_clarification(
        self, request: LiveRequest, unresolved: Sequence[UnresolvedPlace]
    ) -> None:
        """Register a held draft asking the client for the airport(s) and mirror
        the cleared record into the interface's own view, so the UI shows the
        request as awaiting clarification approval rather than as a rate failure.

        The draft is registered in the workflow's pending set (via the router),
        so the existing ``approve_clarification`` releases it unchanged.
        """
        draft = self._router.request_location_clarification(
            request.request_id,
            unresolved,
            to_address=request.client_address,
            subject=request.subject,
            in_reply_to=request.last_message_id or "",
        )
        # The working store now holds either the cleared record + NEEDS_INFO (a
        # fresh draft) or MANUAL_REVIEW (the clarification budget was spent);
        # mirror it either way so `request` matches what the reply will merge
        # against, or shows the hand-over.
        stored = self._working.get_request(request.request_id)
        if stored is not None:
            request.record = stored.record
            request.validation = validate_shipment(stored.record, today=self._clock.now().date())
            request.state = stored.state
        if draft is None:
            # Over-budget: the place never resolved, so the router handed the
            # request to a person rather than asking again. Surface the reason and
            # clear the pending/rate-search state so the desk sees a plain hold,
            # never a silent MANUAL_REVIEW.
            if request.state is RequestState.MANUAL_REVIEW:
                named = ", ".join(p.stated for p in unresolved) or "the stated place"
                request.manual_review_notes = (
                    f"The place(s) {named} could not be resolved to an airport "
                    "after repeated clarification. Handed to a person.",
                )
                request.clarification = None
                request.rate_failure = None
                request.rate_job_id = None
            return
        request.clarification = draft
        request.rate_failure = None
        request.rate_job_id = None

    def _resolve_goods_type(self, request: LiveRequest) -> tuple[str | None, str | None]:
        """The Goods Type to search under, and its source, or ``(None, None)`` to
        hold for an operator.

        A persisted operator pick wins while its fingerprint still matches the
        record's cargo facts; if the record has since changed the pick is
        discarded (audited) and the rule re-decides. Otherwise the reviewed
        General Cargo rule decides, or holds. Pure/deterministic — no I/O.
        """
        from translog_quote.domain.goods_type import decide_goods_type, record_fingerprint

        record = request.record
        commodity = record.commodity or ""
        fingerprint = record_fingerprint(commodity, record.cargo_type, record.is_chemical)
        if request.operator_goods_type is not None:
            if request.operator_goods_type_fingerprint == fingerprint:
                return request.operator_goods_type, "operator"
            # The record changed since the operator picked: discard, re-hold.
            self._emit_goods_type(request.request_id, request.operator_goods_type, "discarded")
            request.operator_goods_type = None
            request.operator_goods_type_fingerprint = None
            request.operator_goods_type_by = None
            self._clear_goods_type_pick(request.request_id)  # so it does not re-fire on restart
        goods = self._settings.goods_type
        decision = decide_goods_type(
            commodity=commodity,
            cargo_type=record.cargo_type,
            is_chemical=record.is_chemical,
            general_cargo_label=goods.general_cargo_label,
            special_handling=goods.special_handling,
        )
        return decision.goods_type, decision.source

    def _emit_goods_type(
        self, request_id: str, goods_type: str, source: str | None, *, by: str | None = None
    ) -> None:
        """Record which Goods Type was chosen, by rule or operator, and by whom."""
        from translog_quote.pipeline.audit import AuditEvent, AuditEventType

        detail: dict[str, object] = {"goods_type": goods_type, "source": source}
        if by:
            detail["by"] = by
        self.audit.record(
            AuditEvent(
                request_id=request_id,
                event=AuditEventType.GOODS_TYPE_DECIDED,
                at=self._clock.now(),
                detail=detail,
            )
        )

    def _emit_rerun_after_restart(self, request_id: str) -> None:
        """Record that a restored request's rate search was re-run after a
        restart — the packet was never persisted, so the search re-derives it."""
        from translog_quote.pipeline.audit import AuditEvent, AuditEventType

        self.audit.record(
            AuditEvent(
                request_id=request_id,
                event=AuditEventType.RATE_SEARCH_RERUN_AFTER_RESTART,
                at=self._clock.now(),
                detail={},
            )
        )

    @staticmethod
    def _job_request_from_record(request: LiveRequest, *, goods_type: str) -> RateSearchJobRequest:
        """Build the queue request from a validated record's own fields.

        Every field is read from the canonical record — no invented value, and
        the shipment date is the client's own (AMB-8). The ``goods_type`` is the
        already-decided exact WebCargo label (never derived here). Missing any
        required field raises, which the caller turns into a reported failure
        rather than a queued job that the worker would only reject.
        """
        record = request.record
        origin = record.origin
        destination = record.destination
        weight = record.weight_kg
        dimensions = record.dimensions_in
        pieces = record.pcs
        commodity = record.commodity
        ship_date = record.ship_date
        if (
            not origin
            or not destination
            or weight is None
            or dimensions is None
            or pieces is None
            or not commodity
            or ship_date is None
        ):
            raise ContractViolation(
                "a validated record is missing a field required to search rates"
            )
        return RateSearchJobRequest(
            origin=origin,
            destination=destination,
            weight_kg=weight,
            dimensions_in=dimensions,
            pieces=pieces,
            search_date=ship_date,
            commodity=commodity,
            goods_type=goods_type,
            cargo_is_liquid=None,  # AMB-3: stated, never derived
            requires_door_delivery=record.delivery_type is DeliveryType.DOOR,
        )

    @staticmethod
    def _outcome_from_job_result(request_id: str, result: RateSearchJobResult) -> RateSearchOutcome:
        """Map a completed job result into the outcome the interface already renders.

        A pure re-shaping: the worker already filtered and selected with the
        same domain code, so this carries those results through unchanged — it
        does not re-run eligibility or fastest-eligible selection.
        """
        target = (
            RequestState.RATE_SELECTED
            if result.selection is not None
            else RequestState.NO_ELIGIBLE_RATE
        )
        return RateSearchOutcome(
            request_id=request_id,
            state=target,
            query=result.query,
            adapter_id=result.adapter_id,
            returned=result.returned,
            filtered=result.filtered,
            selection=result.selection,
            is_simulated=result.is_simulated,
            completeness=result.completeness,
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

        **Demonstration mode** restores only what this demonstration is
        following (``focuses``): a request from another demonstration is not
        active work, and putting it back in ``self.requests`` would make it so.

        **Operations mode** restores from the *store*, not ``demonstration.json``
        (whose ``request_ids`` a past restart may have reset to empty). Every
        persisted request comes back: non-terminal ones are active and advanced
        by the poll; terminal ones are history — shown, hidden by default, and
        never advanced. This is what recovers requests a deploy hid.

        Validation is recomputed rather than stored: it is a pure function of
        the record, so deriving it cannot disagree with the validator, whereas
        a stored copy could. The durable store is still seeded in full either
        way, so correlation and duplicate protection lose no history.
        """
        threads = {thread.request_id: thread for thread in self._durable.all_threads()}
        if self.operations_mode:
            for stored in self._durable.all_requests():
                self.requests[stored.request_id] = self._restored_request(
                    stored, threads.get(stored.request_id)
                )
            return
        for request_id, thread in threads.items():
            followed = self._durable.get_request(request_id)
            if followed is None or not self.in_demonstration(request_id):
                continue
            self.requests[request_id] = self._restored_request(followed, thread)

    #: Pre-send rate states whose in-memory packet is never persisted. A restart
    #: that finds one rewinds it to VALIDATED so the normal rate pass rebuilds a
    #: usable approval card through the ordinary goods-type/location checks —
    #: rather than surfacing an approval with no rates behind it.
    _REDERIVE_STATES = frozenset({RequestState.RATE_SELECTED, RequestState.PENDING_APPROVAL})

    def _restored_request(self, stored: QuotationRequest, thread: Thread | None) -> LiveRequest:
        """One persisted request as a fresh ``LiveRequest`` for this session.

        The store's furthest committed pre-send state is VALIDATED — a rate
        selection and its approval packet live only in memory — so a request
        found in a ``_REDERIVE_STATES`` state (defensively, e.g. an older store)
        is rewound to VALIDATED and its card re-derived by the next poll. Marked
        ``restored`` so a re-enqueue it triggers is audited as a post-restart
        re-run; ``history`` when terminal."""
        message_ids = list(thread.message_ids) if thread is not None else []
        state = RequestState.VALIDATED if stored.state in self._REDERIVE_STATES else stored.state
        return LiveRequest(
            request_id=stored.request_id,
            client_address=stored.client_address,
            state=state,
            record=stored.record,
            validation=validate_shipment(stored.record, today=self._clock.now().date()),
            last_message_id=message_ids[-1] if message_ids else None,
            reply_received=len(message_ids) > 1,
            messages=message_ids,
            clarification_sent_by="(an earlier session)"
            if state is not RequestState.NEEDS_INFO and state is not RequestState.RECEIVED
            else None,
            quotation_sent=stored.state is RequestState.QUOTATION_SENT,
            # An operator's goods-type pick survives the restart; the next poll
            # re-derives the decision (applying it while the fingerprint still
            # matches) or re-holds. The hold itself is never persisted.
            operator_goods_type=stored.operator_goods_type,
            operator_goods_type_fingerprint=stored.operator_goods_type_fingerprint,
            operator_goods_type_by=stored.operator_goods_type_by,
            # A persisted hand-over reason comes back with the request, so an
            # Active MANUAL_REVIEW card still explains itself after a restart.
            manual_review_notes=stored.manual_review_notes,
            # MANUAL_REVIEW is terminal for *automation* but not settled work: a
            # person still owes it a decision. So it comes back as active/needs
            # attention, not collapsed under history like the truly-finished
            # terminal states (accepted, declined, no-eligible-rate, ...).
            history=stored.state in TERMINAL_STATES
            and stored.state is not RequestState.MANUAL_REVIEW,
            restored=True,
        )


_CLASS_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_]*: ")


def _plain_failure(error: str | None) -> str:
    """A rate failure as an operator should read it: the message without the
    leading exception-class name (e.g. 'PermanentFailure: WebCargo …' -> 'WebCargo
    …'). The raw class stays in the worker logs; the dashboard shows plain text.
    """
    if not error:
        return "the rate search failed"
    return _CLASS_PREFIX.sub("", error, count=1)


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


def build_live_session(settings: Settings) -> LiveSession | MultiAccountSession:
    """The session the server serves, or a readable refusal.

    Configuration is checked here so a misconfigured demo fails at start-up
    with a sentence a person can act on, rather than as a 500 in front of an
    audience.

    With ``gmail.accounts_dir`` unset this is a single :class:`LiveSession` over
    the one configured mailbox, exactly as before. With it set, it is a
    :class:`MultiAccountSession` owning one ``LiveSession`` per configured
    account and presenting them to the dashboard as one unified session.

    **Demonstration mode** starts a fresh demonstration as part of starting the
    server: the cutoff is *now*, so the mailbox's history is out of scope and
    the first thing this process handles is the enquiry sent after it came up.

    **Operations mode** does the opposite: a restart resumes. It does *not*
    start a demonstration (that stays an explicit operator action); non-terminal
    requests are restored from the store by ``LiveSession`` and the mail cutoff
    is the persisted watermark — seeded from ``operations_since`` the first time,
    which is required so a fresh deploy cannot silently default the cutoff to
    ``now`` and skip everything earlier.
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

    session: LiveSession | MultiAccountSession
    if settings.gmail.accounts_dir is None:
        session = LiveSession(settings)
    else:
        from translog_quote.interface.web.multi_account_session import MultiAccountSession

        session = MultiAccountSession.build(settings)

    if session.operations_mode:
        session.resume_operations()
    else:
        session.start_demonstration()
    return session
