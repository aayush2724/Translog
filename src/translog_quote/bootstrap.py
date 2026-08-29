"""Composition root — the only module permitted to name a concrete adapter.

Read this file to learn how the system is assembled. That it is the single
exception to the dependency rule is what makes the rest of the rule enforceable:
without it, "swap the adapter" degrades into a flag check somewhere in the middle
of the pipeline.

Every function here returns a **port or a domain type**, never an adapter type.
That is what lets `interface/` drive the real system while remaining unable to
name — or accidentally depend on — the implementation behind it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.config import Settings, load_settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from translog_quote.domain.conversation import CorrelationPolicy
    from translog_quote.domain.email import RawEmail
    from translog_quote.pipeline import (
        ClarificationWorkflow,
        InboundRouter,
        QuotationStage,
    )
    from translog_quote.pipeline.audit import AuditSink
    from translog_quote.ports import (
        ApprovalPort,
        ClockPort,
        DeferredApprovalPort,
        EmailSink,
        EmailSource,
        ExtractionPort,
        RateSearchPort,
        StorePort,
    )

__all__ = [
    "Settings",
    "authorize_gmail",
    "authorize_gmail_send",
    "build_clarification_workflow",
    "build_console_approval",
    "build_recorded_approval",
    "build_correlation_policy",
    "build_demo_rate_provider",
    "build_extractor",
    "build_gmail_email_sink",
    "build_gmail_email_source",
    "build_inbound_router",
    "build_rate_provider",
    "build_fixed_clock",
    "build_system_clock",
    "build_fixture_email_source",
    "build_memory_store",
    "build_persistent_store",
    "build_outbox_sink",
    "build_quotation_stage",
    "commit_request",
    "commit_thread",
    "load_settings",
    "persistent_state_files",
    "seed_store",
]


def build_extractor(settings: Settings) -> ExtractionPort:
    """The live extraction port, backed by OpenRouter.

    Raises `PermanentFailure` at construction when the API key or model is
    missing, rather than at first use — a missing key found halfway through a
    client's email is a worse place to discover it.
    """
    from translog_quote.adapters.extraction import build_openrouter_extractor

    return build_openrouter_extractor(settings)


def build_fixture_email_source(settings: Settings, scenario: str) -> EmailSource:
    """An `EmailSource` over one fixture scenario directory.

    Returns the port, not the fixture-scenario object: callers get `RawEmail`
    values and nothing that would tie them to how those emails were stored.
    """
    from translog_quote.adapters.email import FixtureEmailSource

    return FixtureEmailSource(settings.demo.email_fixtures_dir / scenario)


def build_gmail_email_source(settings: Settings, *, max_results: int | None = None) -> EmailSource:
    """An `EmailSource` over the configured Gmail **test** mailbox (Phase 10.3).

    Receive-only, and never built implicitly: the fixture source stays the
    default everywhere, and only the explicit Gmail commands ask for this one.
    Refuses to build without a configured test address, and the transport
    refuses without the git-ignored OAuth token file.

    `max_results` raises the configured one-message ceiling for a caller that
    genuinely needs a conversation rather than a message — reading an enquiry
    and its reply takes two. It stays an explicit argument so the narrow
    default is what every other caller gets.
    """
    from translog_quote.adapters.email import GmailEmailSource, HttpxGmailTransport
    from translog_quote.errors import PermanentFailure

    gmail = settings.gmail
    if not gmail.test_address:
        raise PermanentFailure(
            "No Gmail test mailbox configured. Set TRANSLOG_GMAIL__TEST_ADDRESS "
            "in .env (see .env.example)."
        )

    transport = HttpxGmailTransport(
        token_path=gmail.token_path,
        timeout_seconds=gmail.timeout_seconds,
        max_retries=gmail.max_retries,
        backoff_seconds=gmail.retry_backoff_seconds,
    )
    return GmailEmailSource(
        transport,
        mailbox_address=gmail.test_address,
        query=gmail.query,
        max_results=gmail.max_results if max_results is None else max_results,
    )


def authorize_gmail(settings: Settings) -> Path:
    """Run the one-time interactive Gmail OAuth consent; returns the token path.

    Only ever called by the explicit `gmail-auth` command — nothing authorizes
    automatically.
    """
    from translog_quote.adapters.email.gmail_auth import run_consent_flow

    return run_consent_flow(
        client_secret_path=settings.gmail.client_secret_path,
        token_path=settings.gmail.token_path,
    )


def authorize_gmail_send(settings: Settings) -> Path:
    """Run the one-time Gmail **send** consent; returns the send token path.

    A second, separate grant into a second, separate file. The same Desktop
    OAuth client is reused, but the scope and the token are not: after this the
    project holds one credential that can only read and one that can only send.
    """
    from translog_quote.adapters.email.gmail_auth import run_consent_flow
    from translog_quote.adapters.email.gmail_send import GMAIL_SEND_SCOPE

    return run_consent_flow(
        client_secret_path=settings.gmail.client_secret_path,
        token_path=settings.gmail.send_token_path,
        scope=GMAIL_SEND_SCOPE,
    )


def build_correlation_policy() -> CorrelationPolicy:
    """How a reply is matched to its enquiry: RFC header chains only.

    Returns the policy contract, not the class, so no caller can come to
    depend on which rule is behind it.
    """
    from translog_quote.domain.conversation import HeaderChainCorrelation

    return HeaderChainCorrelation()


def build_inbound_router(
    settings: Settings,
    *,
    new_request_id: Callable[[RawEmail], str],
    store: StorePort | None = None,
    extractor: ExtractionPort | None = None,
    audit: AuditSink | None = None,
    sink: EmailSink | None = None,
    clock: ClockPort | None = None,
) -> InboundRouter:
    """Correlation plus the clarification loop, over **one shared store**.

    Sharing matters: the router reads threads from the same store the workflow
    writes requests to, so a reply correlated to R-123 merges into the record
    R-123 actually has. Two stores would correlate correctly and merge into
    nothing.

    `sink` is where an *approved* clarification is released. It defaults to the
    outbox sink, which delivers nothing — so a caller has to ask, explicitly
    and by argument, for a router whose drafts can reach a real client. The
    approval gate is unchanged either way: the sink is what happens after a
    person says yes, never a substitute for one.
    """
    from translog_quote.pipeline import InboundRouter

    shared = store or build_memory_store()
    return InboundRouter(
        policy=build_correlation_policy(),
        workflow=build_clarification_workflow(
            settings, store=shared, extractor=extractor, audit=audit, sink=sink, clock=clock
        ),
        store=shared,
        new_request_id=new_request_id,
    )


def build_memory_store() -> StorePort:
    """The demo's request store. In memory; nothing survives the process."""
    from translog_quote.adapters.store import InMemoryStore

    return InMemoryStore()


def build_persistent_store(settings: Settings) -> StorePort:
    """A store that outlives the process, under the git-ignored state directory.

    Asked for explicitly. The in-memory store stays the default everywhere, so
    no test and no other demo gains a file on disk by accident.
    """
    from translog_quote.adapters.store import JsonFileStore

    return JsonFileStore(settings.demo.state_dir)


def persistent_state_files() -> tuple[str, ...]:
    """The file names the durable store writes, for a caller that clears them.

    Named here because `bootstrap` is the only module allowed to know which
    store implementation is behind the port, and a reset command has no
    business importing an adapter to learn what to delete.
    """
    from translog_quote.adapters.store import REQUESTS_FILE, THREADS_FILE

    return (REQUESTS_FILE, THREADS_FILE)


def seed_store(target: StorePort, source: StorePort) -> None:
    """Copy everything `source` knows into `target`.

    Half of the commit-point model: a run works against a scratch store seeded
    from the durable one, so the workflow's own writes — which happen turn by
    turn, before anyone has approved anything — land somewhere discardable.

    Threads are the index. Every request that was ever committed was committed
    together with its thread, so walking the threads reaches every request
    without `StorePort` needing an `all_requests` it has no other use for.
    """
    for thread in source.all_threads():
        target.save_thread(thread)
        request = source.get_request(thread.request_id)
        if request is not None:
            target.save_request(request)


def commit_thread(source: StorePort, target: StorePort, request_id: str) -> None:
    """Persist only that a request's messages were seen, not the request itself.

    For a message the workflow processed and nothing wants to advance — an
    ordinary inbox email that turned out to carry no shipment at all. Recording
    the thread stops it being extracted again on every poll; deliberately *not*
    recording the request keeps a dead `NEEDS_INFO` row out of the store, where
    it could neither advance nor be explained.
    """
    thread = next((t for t in source.all_threads() if t.request_id == request_id), None)
    if thread is not None:
        target.save_thread(thread)


def commit_request(source: StorePort, target: StorePort, request_id: str) -> None:
    """Persist one request and its thread. The other half of the model.

    Called only after something irreversible has happened — a clarification was
    actually emailed, a reply was merged, the quotation gate decided. A run
    that stops at a gate having sent nothing commits nothing, so the next run
    starts from the last state a person actually authorised and redoes only
    work that can be redone safely.

    That ordering matters for more than tidiness: the transition table permits
    no way out of NEEDS_INFO except CLARIFICATION_SENT, so persisting a request
    that is merely *awaiting* approval would leave it unable to advance in any
    later process.
    """
    request = source.get_request(request_id)
    if request is not None:
        target.save_request(request)
    thread = next((t for t in source.all_threads() if t.request_id == request_id), None)
    if thread is not None:
        target.save_thread(thread)


def build_outbox_sink(directory: Path | None = None) -> EmailSink:
    """Where clarifications go. Collected in memory, and written to a directory
    when one is given. No mail is sent."""
    from translog_quote.adapters.email import CollectingEmailSink, FileOutboxSink

    return FileOutboxSink(directory) if directory is not None else CollectingEmailSink()


def build_gmail_email_sink(settings: Settings) -> EmailSink:
    """The **sending** `EmailSink`, over the send-scoped Gmail credential.

    Three independent things must be true before this returns a sink that can
    reach a real inbox, and each is checked here rather than at first send:

    1. `TRANSLOG_GMAIL__SEND_ENABLED` is on. Off is the default, so a token
       file lying around is not enough to make anything send.
    2. A sender address is known. It becomes the `From` header, which Gmail
       validates against the authenticated account.
    3. The send token file exists — enforced by the transport's constructor.

    The sender address falls back to the configured test mailbox because in
    this demo Translog reads and sends from one account. The fallback lives
    here, in the composition root, rather than in the settings model: it is a
    wiring decision, and `GmailSettings` has no business knowing that its two
    address fields usually name the same mailbox.
    """
    from translog_quote.adapters.email import GmailEmailSink, HttpxGmailSendTransport
    from translog_quote.errors import PermanentFailure

    gmail = settings.gmail
    if not gmail.send_enabled:
        raise PermanentFailure(
            "Outbound Gmail is disabled. Set TRANSLOG_GMAIL__SEND_ENABLED=true in .env "
            "to allow this process to send real email."
        )

    sender = gmail.sender_address or gmail.test_address
    if not sender:
        raise PermanentFailure(
            "No Gmail sender address configured. Set TRANSLOG_GMAIL__SENDER_ADDRESS "
            "(or TRANSLOG_GMAIL__TEST_ADDRESS) in .env (see .env.example)."
        )

    transport = HttpxGmailSendTransport(
        token_path=gmail.send_token_path,
        timeout_seconds=gmail.timeout_seconds,
        max_retries=gmail.max_retries,
        backoff_seconds=gmail.retry_backoff_seconds,
    )
    return GmailEmailSink(transport, sender_address=sender)


def build_console_approval(
    *,
    approver: str | None = None,
    read_line: Callable[[str], str] | None = None,
    out: object | None = None,
) -> ApprovalPort:
    """The human approval gate, as a terminal prompt.

    Returns the port, not the class, so no caller can come to depend on the
    decision being taken at a console rather than anywhere else. Whatever is
    behind it, the contract is the same halt: it blocks, and it never returns a
    default (BR-11).
    """
    from translog_quote.adapters.approval import ConsoleApprovalGate

    kwargs: dict[str, object] = {"clock": build_fixed_clock(), "approver": approver}
    if read_line is not None:
        kwargs["read_line"] = read_line
    if out is not None:
        kwargs["out"] = out
    return ConsoleApprovalGate(**kwargs)  # type: ignore[arg-type]


def build_recorded_approval() -> DeferredApprovalPort:
    """A gate whose decision is placed from outside, then consumed by the halt.

    What a user interface needs: the person clicks on one request, and the
    pipeline runs on that same request, so the decision has to be recorded
    before `QuotationStage` consults it. Returns the port, so no caller comes
    to depend on the decision having arrived over HTTP rather than from
    anywhere else.

    It is not a weaker gate. Consulted with nothing recorded it raises, and a
    recorded decision is single-use — so a second run without a fresh decision
    fails rather than silently reapplying the last person's answer.
    """
    from translog_quote.adapters.approval import RecordedDecisionGate

    return RecordedDecisionGate()


def build_quotation_stage(
    settings: Settings,
    *,
    sink: EmailSink,
    approval: ApprovalPort,
    store: StorePort | None = None,
    audit: AuditSink | None = None,
    clock: ClockPort | None = None,
) -> QuotationStage:
    """The approval gate and the only path to a client quotation.

    `sink` and `approval` are required arguments with no defaults. There is no
    convenience wiring that quietly supplies an auto-approving gate, because a
    default here is exactly the thing the gate exists to prevent.

    Refuses to build without an internal approver address: a review packet that
    goes nowhere is indistinguishable, from the outside, from a system that
    approved on its own.
    """
    from translog_quote.errors import PermanentFailure
    from translog_quote.pipeline import QuotationStage

    approver_address = settings.gmail.approver_address
    if not approver_address:
        raise PermanentFailure(
            "No internal approver address configured. Set TRANSLOG_GMAIL__APPROVER_ADDRESS "
            "in .env (see .env.example). The quotation review must reach a person."
        )

    return QuotationStage(
        sink=sink,
        approval=approval,
        clock=clock or build_fixed_clock(),
        approver_address=approver_address,
        store=store,
        audit=audit,
    )


def build_system_clock() -> ClockPort:
    """The wall clock. What the live demonstration runs on.

    The fixed clock exists so an audit trail is diffable across runs, which is
    right for a scripted demo and wrong for a real one: a live run's evidence
    trail is a record of when things actually happened, and freezing it would
    stamp every event with the same invented moment. Nothing about the pipeline
    changes — only which `ClockPort` the composition root hands it.
    """
    from translog_quote.adapters.clock import SystemClock

    return SystemClock()


def build_fixed_clock(moment: object | None = None) -> ClockPort:
    """A clock that does not move, so an audit trail is diffable across runs."""
    from translog_quote.adapters.clock import FixedClock

    return FixedClock(moment)  # type: ignore[arg-type]


def build_clarification_workflow(
    settings: Settings,
    *,
    sink: EmailSink | None = None,
    store: StorePort | None = None,
    extractor: ExtractionPort | None = None,
    audit: AuditSink | None = None,
    clock: ClockPort | None = None,
) -> ClarificationWorkflow:
    """The clarification loop, wired to live extraction unless told otherwise.

    Every collaborator is injectable so a test can drive the real workflow with
    a stub extractor and no network. `audit` is optional: pass one to collect
    the evidence trail, which is how a caller shows that a draft was recorded
    as drafted-and-not-sent.
    """
    from translog_quote.pipeline import ClarificationWorkflow

    return ClarificationWorkflow(
        extractor=extractor or build_extractor(settings),
        sink=sink or build_outbox_sink(),
        store=store or build_memory_store(),
        # Fixed unless a caller says otherwise, so a scripted demo stays
        # diffable. A live run passes the wall clock, because its audit trail
        # is a record of when things actually happened.
        clock=clock or build_fixed_clock(),
        audit=audit,
    )


def build_rate_provider(settings: Settings) -> RateSearchPort:
    """The configured rate provider.

    `mock` yields fixture rates and labels them as such; `real` yields an adapter
    that refuses with the reason, because no WebCargo API contract has been
    provided. This is the only place either concrete class is named.
    """
    from translog_quote.config import WebCargoMode

    if settings.webcargo.mode is WebCargoMode.REAL:
        from translog_quote.adapters.webcargo import RealWebCargoAdapter

        return RealWebCargoAdapter(base_url=settings.webcargo.base_url)

    if settings.webcargo.mode is WebCargoMode.DEMO:
        return build_demo_rate_provider()

    from translog_quote.adapters.webcargo import MockWebCargoAdapter

    return MockWebCargoAdapter()


def build_demo_rate_provider() -> RateSearchPort:
    """Simulated WebCargo-shaped rates, priced from the shipment being quoted.

    For the client-facing demo. Every result it returns is flagged
    `is_simulated`, so nothing downstream can present these as live rates.
    Requested explicitly by the demo entry points rather than reached by
    default, which keeps the tests on the fixture adapter.
    """
    from translog_quote.adapters.webcargo import DemoRateProvider

    return DemoRateProvider()
