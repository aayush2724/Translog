"""Phase 11 — the outbound half, end to end, over two real Gmail accounts.

    real Gmail mailbox (read-only credential)
        -> InboundRouter -> correlation -> live extraction -> merge -> validate
        -> clarification, released by a named human, SENT for real
        -> client reply, correlated, merged, re-validated -> VALIDATED
        -> DemoRateProvider (simulated rates, disclosed as such)
        -> filter -> select -> RATE_SELECTED
        -> QuotationStage
             -> review packet emailed to the INTERNAL approver
             -> ApprovalPort halts at the terminal
             -> APPROVE -> quotation emailed to the CLIENT -> QUOTATION_SENT
                DECLINE -> nothing sent at all      -> MAKER_REJECTED

What this command adds over `gmail-thread` is delivery. The clarification and
the quotation are handed to a *sending* sink built on a second, send-scoped
Gmail credential — separate token, separate scope, separate consent run from
the read-only credential that ingests client mail. Neither can do the other's
job, so "inbound is separate from outbound" is a property of the grants rather
than a convention in this file.

What it does not do, and cannot:

- It cannot approve anything. Both gates take a named person's decision: the
  clarification needs `--approved-by`, and the quotation needs the operator to
  type APPROVE at the terminal after reading the packet in their mailbox.
- It cannot send a quotation the person declined. The decline path returns
  without touching the client sink, and `Quotation` cannot be constructed
  without an `Approved`.
- It cannot send the same quotation twice. The stage refuses a request it has
  already decided.
- It cannot present simulated rates as real ones. `DemoRateProvider` flags
  every result, and the disclosure travels into the client email itself.
"""

from __future__ import annotations

import datetime
import sys
from enum import StrEnum
from typing import TYPE_CHECKING, TextIO

from translog_quote import bootstrap
from translog_quote.config import load_settings
from translog_quote.domain.quotation import INTERNAL_SUBJECT_PREFIX, ReviewPacket
from translog_quote.domain.rates import FASTEST_ELIGIBLE
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import TranslogError
from translog_quote.interface.demo.formatting import (
    RULE,
    THIN,
    format_record,
    format_validation,
    render_transit,
)
from translog_quote.interface.demo.gmail_process import _CollectingAudit
from translog_quote.interface.demo.gmail_thread import (
    DEFAULT_MESSAGE_LIMIT,
    _report_routing,
    _request_id_for,
)
from translog_quote.pipeline import RateSearchStage

if TYPE_CHECKING:
    from translog_quote.config import Settings
    from translog_quote.domain.email import RawEmail
    from translog_quote.pipeline import InboundRouter, RoutedMessage
    from translog_quote.pipeline.audit import AuditEvent
    from translog_quote.ports import StorePort

EXIT_OK = 0
"""Also returned when the clarification went out and the run is now waiting on
the client. Sending a clarification and entering the waiting state is the
workflow progressing exactly as designed, not a failure — the printed STATUS
line is what distinguishes it from a delivered quotation."""

EXIT_CONFIG = 2
EXIT_GMAIL = 3
EXIT_EXTRACTION = 4
EXIT_NO_MESSAGE = 6
EXIT_AWAITING_APPROVAL = 7
EXIT_NOT_VALIDATED = 8
EXIT_NO_RATE = 9
EXIT_DECLINED = 10
EXIT_NO_DECISION = 11


class DemoStatus(StrEnum):
    """Where one run of this command finished.

    Presentation vocabulary only. The workflow's own `RequestState` remains the
    authority on the request — this names what *the run* did, which is not the
    same question. `AWAITING_CLIENT_REPLY` is the run-level reading of a request
    sitting at `RequestState.CLARIFICATION_SENT`: the email is gone, the process
    can exit, and the next move belongs to the client.

    Deliberately not a thirteenth `RequestState`. The twelve-state table is an
    approved artefact (docs/architecture.md §10) and `CLARIFICATION_SENT`
    already means precisely this; adding a state that duplicates one would make
    the table describe the demo's console output rather than the business.
    """

    AWAITING_CLIENT_REPLY = "clarification_sent / awaiting_client_reply"
    AWAITING_CLARIFICATION_APPROVAL = "awaiting_clarification_approval"
    QUOTATION_SENT = "quotation_sent"
    QUOTATION_DECLINED = "quotation_declined"
    NOTHING_NEW = "nothing_new"


#: AMB-8: no approved source for a rate-search date exists, so the command
#: states one rather than letting anything downstream invent it.
DEFAULT_SEARCH_DATE = datetime.date(2026, 9, 2)

#: Stated by the operator, never derived. The canonical record cannot express
#: physical form, and guessing "liquid" from a commodity name would put a wrong
#: carrier on a real quotation (AMB-3).
DEFAULT_CARGO_IS_LIQUID: bool | None = None

_BANNER = f"""{RULE}
  TRANSLOG — PHASE 11: APPROVED QUOTATION, SENT BY REAL EMAIL
{RULE}
  GMAIL INBOUND:     REAL (read-only credential)
  GMAIL OUTBOUND:    REAL (separate send-only credential)
  CORRELATION:       REAL (RFC In-Reply-To / References)
  AI EXTRACTION:     LIVE
  MERGE + VALIDATION:REAL / DETERMINISTIC
  RATES:             SIMULATED (DemoRateProvider) — no provider is contacted
  RATE SELECTION:    REAL / DETERMINISTIC (fastest eligible, BR-1)
  APPROVAL:          HUMAN — explicit, named, no default, no timeout
  WEBCARGO:          NOT CONTACTED
{RULE}"""


def _fail(out: TextIO, heading: str, detail: str) -> None:
    print(f"\n  {heading}", file=out)
    print(f"  {detail}\n", file=out)


def _is_our_own_review_mail(email: RawEmail) -> bool:
    """Approval requests we sent ourselves are not client enquiries.

    The internal approver mailbox is, in this demo, the same mailbox Translog
    reads. Without this the run would ingest its own review packet on the next
    pass and try to extract a shipment from it. Matching on our own subject
    marker is safe here in a way subject matching is *not* safe for
    correlation: this is a refusal, not a merge — the worst case is that a
    message is skipped, never that two conversations are joined.
    """
    return email.subject.strip().startswith(INTERNAL_SUBJECT_PREFIX)


def _audit_block(events: list[AuditEvent]) -> str:
    return "\n".join(
        f"  {event.event.value:<24} "
        + ", ".join(f"{k}={v}" for k, v in sorted(event.detail.items()))
        for event in events
    )


def _release_clarification(
    router: InboundRouter,
    held: RoutedMessage,
    approved_by: str | None,
    out: TextIO,
) -> int | None:
    """Release a held draft on a named person's authority, or stop.

    Returns an exit code when the run must stop, and ``None`` when the draft
    was released. There is no third outcome: without ``--approved-by`` this
    returns the stop code, and nothing here can supply a name of its own.
    """
    if approved_by is None:
        print(f"\n{RULE}\nHUMAN APPROVAL REQUIRED (CLARIFICATION)\n{THIN}", file=out)
        print(f"  request {held.request_id} is holding a clarification draft.", file=out)
        print("  Nothing has been sent, and nothing has been written to the", file=out)
        print("  demo state directory — this run changed nothing.", file=out)
        print("\n  This command will not approve on your behalf. Re-run with:", file=out)
        print(
            "\n    python -m translog_quote.interface.demo gmail-quote "
            '--approved-by "your.name@company"\n',
            file=out,
        )
        print(f"  STATUS: {DemoStatus.AWAITING_CLARIFICATION_APPROVAL.value}", file=out)
        print(RULE, file=out)
        return EXIT_AWAITING_APPROVAL

    assert held.request_id is not None  # a draft implies a routed request
    try:
        router.approve(held.request_id, by=approved_by)
    except TranslogError as exc:
        _fail(out, f"CLARIFICATION APPROVAL FAILED: {type(exc).__name__}", str(exc))
        return EXIT_EXTRACTION

    print(f"\n  clarification for {held.request_id} approved by {approved_by}", file=out)
    print("  and SENT to the client by real email.", file=out)
    return None


def _report_awaiting_reply(
    out: TextIO, request_id: str, store: StorePort, audit: _CollectingAudit
) -> int:
    """The run ends here, successfully, waiting on a human who is not us.

    Exit 0: sending the clarification and entering the waiting state is the
    workflow progressing as designed. The request's own state is read back from
    the store rather than asserted from memory, because that is the fact the
    next invocation will load.
    """
    stored = store.get_request(request_id)
    state = stored.state.value if stored else "unknown"

    print(f"\n{RULE}\nCLARIFICATION SENT — AWAITING THE CLIENT'S REPLY\n{THIN}", file=out)
    print(f"  request        : {request_id}", file=out)
    print(f"  request state  : {state}", file=out)
    print(f"  STATUS         : {DemoStatus.AWAITING_CLIENT_REPLY.value}", file=out)
    print("  persisted      : yes — this run's progress survives the process", file=out)
    print("\n  The process can now exit safely. When the client replies, run the", file=out)
    print("  same command again: it will load this state, skip the messages it", file=out)
    print("  has already handled, and process only the new reply.", file=out)
    print(f"\nAUDIT TRAIL\n{THIN}", file=out)
    print(_audit_block(audit.events), file=out)
    print(RULE, file=out)
    return EXIT_OK


def _report_nothing_new(out: TextIO, durable: StorePort, seen: list[RawEmail]) -> int:
    """Every message in the mailbox was handled by an earlier run.

    Not an error, and specifically not a reason to do anything again: the whole
    point of the persisted state is that a repeated run is a no-op. Where a
    quotation has already gone out, saying so plainly is more useful than a
    bare "nothing to do".
    """
    print(f"\n{RULE}\nNOTHING NEW\n{THIN}", file=out)
    print(f"  All {len(seen)} message(s) in the mailbox were processed in an", file=out)
    print("  earlier run. No model was called and no email was sent.", file=out)

    for thread in durable.all_threads():
        request = durable.get_request(thread.request_id)
        if request is not None:
            print(f"\n  {thread.request_id}: {request.state.value}", file=out)
            if request.state is RequestState.QUOTATION_SENT:
                print("    A quotation has already been sent for this request.", file=out)
                print("    It will not be sent again.", file=out)
            elif request.state is RequestState.MAKER_REJECTED:
                print("    This quotation was declined. Nothing was sent, and the", file=out)
                print("    decision is terminal.", file=out)
            elif request.state is RequestState.CLARIFICATION_SENT:
                print("    Waiting on the client's reply.", file=out)

    print(f"\n  STATUS: {DemoStatus.NOTHING_NEW.value}", file=out)
    print(RULE, file=out)
    return EXIT_OK


def _preflight(settings: Settings, out: TextIO) -> int | None:
    """Everything that must be configured, checked before any mail moves."""
    if settings.openrouter.api_key is None:
        _fail(
            out,
            "CONFIGURATION ERROR: no OpenRouter API key.",
            "Set TRANSLOG_OPENROUTER__API_KEY in .env (see .env.example).",
        )
        return EXIT_CONFIG
    if not settings.gmail.send_enabled:
        _fail(
            out,
            "CONFIGURATION ERROR: outbound Gmail is disabled.",
            "Set TRANSLOG_GMAIL__SEND_ENABLED=true and run gmail-auth-send first.",
        )
        return EXIT_CONFIG
    if not settings.gmail.approver_address:
        _fail(
            out,
            "CONFIGURATION ERROR: no internal approver address.",
            "Set TRANSLOG_GMAIL__APPROVER_ADDRESS in .env. The review must reach a person.",
        )
        return EXIT_CONFIG
    return None


def run_gmail_quote(  # noqa: C901 - a linear demo script; splitting it hides the order
    *,
    settings: Settings | None = None,
    approved_by: str | None = None,
    cargo_is_liquid: bool | None = DEFAULT_CARGO_IS_LIQUID,
    on_date: datetime.date | None = None,
    limit: int = DEFAULT_MESSAGE_LIMIT,
    out: TextIO = sys.stdout,
) -> int:
    """Run the conversation, then the rate pipeline, then the human gate."""
    settings = settings or load_settings()
    search_date = on_date or DEFAULT_SEARCH_DATE

    print(_BANNER, file=out)

    failure = _preflight(settings, out)
    if failure is not None:
        return failure

    # --- 1. the outbound sink, built once and shared --------------------------
    # Built before anything is read, so a misconfigured send credential stops
    # the run before a client email is processed rather than after.
    try:
        sink = bootstrap.build_gmail_email_sink(settings)
    except TranslogError as exc:
        _fail(out, f"OUTBOUND GMAIL UNAVAILABLE: {type(exc).__name__}", str(exc))
        return EXIT_CONFIG

    # --- 2. receive -----------------------------------------------------------
    print("\n  ... reading the Gmail mailbox (read-only credential) ...", file=out, flush=True)
    try:
        source = bootstrap.build_gmail_email_source(settings, max_results=limit)
        received = source.fetch_new()
    except TranslogError as exc:
        _fail(out, f"GMAIL RECEIVE FAILED: {type(exc).__name__}", str(exc))
        return EXIT_GMAIL

    client_mail = [e for e in received if not _is_our_own_review_mail(e)]
    skipped = len(received) - len(client_mail)
    if not client_mail:
        _fail(
            out,
            "NO CLIENT MESSAGE FOUND.",
            f"Nothing in the mailbox matched query {settings.gmail.query!r}"
            + (f" ({skipped} internal approval mail(s) skipped)." if skipped else "."),
        )
        return EXIT_NO_MESSAGE

    # Gmail lists newest first; a conversation only makes sense forwards.
    conversation = sorted(client_mail, key=lambda e: e.received_at)
    print(f"  {len(conversation)} client message(s), oldest first.", file=out)
    if skipped:
        print(f"  {skipped} internal approval mail(s) skipped.", file=out)

    # --- 3. load what earlier runs already did --------------------------------
    # The durable store is what makes this demo survivable across invocations:
    # a clarification goes out today and the client replies tomorrow. The loop
    # runs against a scratch copy, and only committed work goes back to disk.
    audit = _CollectingAudit()
    durable = bootstrap.build_persistent_store(settings)
    store = bootstrap.build_memory_store()
    bootstrap.seed_store(store, durable)

    # The sending sink is passed in explicitly. `build_inbound_router` defaults
    # to the outbox sink that delivers nothing, so a router whose approved
    # clarifications reach a real client is something a caller has to ask for.
    router = bootstrap.build_inbound_router(
        settings, new_request_id=_request_id_for, store=store, audit=audit, sink=sink
    )

    # Messages an earlier run already handled are not handled again. This is
    # what stops a second run re-calling the model on the enquiry, redrafting a
    # clarification that has already gone out, and mailing the client a
    # duplicate of a question they have answered.
    fresh = [e for e in conversation if not router.already_processed(e.message_id)]
    seen_before = len(conversation) - len(fresh)
    if seen_before:
        print(f"  {seen_before} message(s) already processed in an earlier run; skipped.", file=out)

    if not fresh:
        return _report_nothing_new(out, durable, conversation)

    # --- 4. route, clarify, merge, validate -----------------------------------
    last: RoutedMessage | None = None
    last_email: RawEmail | None = None

    for index, email in enumerate(fresh, start=1):
        # A held draft blocks the next message: the table permits no way out of
        # NEEDS_INFO except CLARIFICATION_SENT, so a reply cannot be processed
        # until the clarification it answers has actually been sent.
        if last is not None and last.outcome is not None and last.outcome.awaiting_approval:
            released = _release_clarification(router, last, approved_by, out)
            if released is not None:
                return released
            bootstrap.commit_request(store, durable, last.request_id or "")

        try:
            routed = router.route(email)
        except TranslogError as exc:
            _fail(out, f"WORKFLOW FAILED: {type(exc).__name__}", str(exc))
            return EXIT_EXTRACTION

        _report_routing(out, index, email, routed)
        if routed.was_refused:
            print(f"\n  REFUSED — MANUAL REVIEW\n  {routed.reason}", file=out)
            continue

        assert routed.outcome is not None
        changed = routed.outcome.merge.changed
        filled = ", ".join(f.value for f in changed) if changed else "— nothing new"
        print(f"\n  this message filled: {filled}", file=out)
        last, last_email = routed, email

    if last is None or last.outcome is None or last.request_id is None:
        _fail(out, "NOTHING WAS PROCESSED.", "Every message was refused correlation.")
        return EXIT_NO_MESSAGE

    request_id = last.request_id
    outcome = last.outcome

    # --- 5. a draft still held after the last message -------------------------
    # The fix for the one-message case. Previously the release only ever ran
    # *ahead of a subsequent message*, so an enquiry sitting alone in the
    # mailbox could never have its clarification sent — and the client could
    # never produce the reply that would have triggered it. A deadlock.
    if outcome.awaiting_approval:
        released = _release_clarification(router, last, approved_by, out)
        if released is not None:
            return released
        bootstrap.commit_request(store, durable, request_id)
        return _report_awaiting_reply(out, request_id, store, audit)

    # Everything below here is reached only by a shipment whose clarification
    # round is over. The reply has been merged, so this turn is worth keeping
    # whatever the gate later decides.
    bootstrap.commit_request(store, durable, request_id)

    print(f"\n{RULE}\nMERGED SHIPMENT  ({request_id})\n{THIN}", file=out)
    print(format_record(outcome.record), file=out)
    print(f"\n{format_validation(outcome.validation)}", file=out)

    if not outcome.is_complete:
        print(f"\n  state: {outcome.state.value}", file=out)
        print("  The shipment is not yet VALID, so no rate search is run and no", file=out)
        print("  quotation is produced. An incomplete shipment is never quoted.", file=out)
        print(f"{RULE}", file=out)
        return EXIT_NOT_VALIDATED

    # --- 6. rates: simulated, filtered, ranked, selected -----------------------
    print(f"\n{RULE}\nRATE SEARCH  (SIMULATED — DemoRateProvider)\n{THIN}", file=out)
    stage = RateSearchStage(
        provider=bootstrap.build_demo_rate_provider(),
        strategy=FASTEST_ELIGIBLE,
        audit=audit,
        clock=bootstrap.build_fixed_clock(),
    )
    try:
        rates = stage.run(
            request_id,
            outcome.record,
            on_date=search_date,
            cargo_is_liquid=cargo_is_liquid,
        )
    except TranslogError as exc:
        _fail(out, f"RATE SEARCH FAILED: {type(exc).__name__}", str(exc))
        return EXIT_NO_RATE

    provenance = (
        "SIMULATED WEBCARGO DATA — DEMO ONLY. No WebCargo request was made."
        if rates.uses_mock_data
        else f"LIVE PROVIDER DATA — {rates.adapter_id}"
    )
    print(f"  {provenance}", file=out)
    print(
        f"  query: {rates.query.origin_iata} -> {rates.query.destination_iata}  "
        f"{rates.query.weight_kg:g} kg  on {rates.query.date}",
        file=out,
    )
    print(f"  cargo declared liquid: {cargo_is_liquid}  (stated, never derived — AMB-3)", file=out)
    print(
        f"\n  returned {rates.returned}, eligible {len(rates.filtered.eligible)}, "
        f"excluded {len(rates.filtered.excluded)}",
        file=out,
    )
    for excluded in rates.filtered.excluded:
        print(
            f"    - {excluded.rate.carrier_code:<4} {excluded.reason.value:<24} {excluded.detail}",
            file=out,
        )

    if rates.selection is None:
        print(f"\n  NO ELIGIBLE RATE  ({rates.state.value})", file=out)
        print("  Stopping before a quotation rather than producing a misleading one.", file=out)
        print(RULE, file=out)
        return EXIT_NO_RATE

    chosen = rates.selection.rate
    print(f"\nSELECTED — FASTEST ELIGIBLE\n{THIN}", file=out)
    print(f"  carrier : {chosen.carrier_name} ({chosen.carrier_code}) {chosen.product}", file=out)
    print(f"  transit : {render_transit(chosen.transit)}", file=out)
    print(f"  price   : {chosen.total_amount} {chosen.currency}", file=out)
    print(f"  why     : {rates.selection.reason}", file=out)

    # --- 7. the human gate ----------------------------------------------------
    # The stored request is the authority on where a quotation goes: it is
    # what the workflow recorded when it processed the client's own mail. The
    # last email's sender is a fallback, never a preference.
    stored = store.get_request(request_id)
    client_address = stored.client_address if stored else ""
    if not client_address and last_email is not None:
        client_address = last_email.from_address
    if not client_address:  # pragma: no cover - a routed message always has a sender
        _fail(out, "NO CLIENT ADDRESS.", "Refusing to send a quotation with no recipient.")
        return EXIT_CONFIG

    packet = ReviewPacket(
        request_id=request_id,
        record=outcome.record,
        validation=outcome.validation,
        clarification_sent=True,
        rates=rates.filtered,
        selection=rates.selection,
    )

    try:
        gate = bootstrap.build_quotation_stage(
            settings,
            sink=sink,
            approval=bootstrap.build_console_approval(approver=approved_by),
            store=store,
            audit=audit,
        )
    except TranslogError as exc:
        _fail(out, f"APPROVAL GATE UNAVAILABLE: {type(exc).__name__}", str(exc))
        return EXIT_CONFIG

    print(
        f"\n  ... emailing the review packet to {settings.gmail.approver_address} ...",
        file=out,
        flush=True,
    )
    try:
        decided = gate.run(
            packet,
            client_address=client_address,
            is_simulated=rates.uses_mock_data,
            in_reply_to=last_email.message_id if last_email else None,
        )
    except TranslogError as exc:
        # An abandoned decision lands here. The request stays at
        # PENDING_APPROVAL and nothing has reached the client.
        _fail(out, f"NO DECISION RECORDED: {type(exc).__name__}", str(exc))
        print(f"\nAUDIT TRAIL\n{THIN}", file=out)
        print(_audit_block(audit.events), file=out)
        print(RULE, file=out)
        return EXIT_NO_DECISION

    # --- 8. what actually happened -------------------------------------------
    # The decision is committed before it is reported. Whatever happens to this
    # process next, a later run loads a request that is already settled and the
    # gate refuses to ask anyone to decide it a second time.
    bootstrap.commit_request(store, durable, request_id)

    status = DemoStatus.QUOTATION_SENT if decided.was_approved else DemoStatus.QUOTATION_DECLINED
    print(f"\n{RULE}\nOUTCOME\n{THIN}", file=out)
    print(f"  request state    : {decided.state.value}", file=out)
    print(f"  decided by       : {decided.decision.by}", file=out)
    print(f"  decision         : {'APPROVED' if decided.was_approved else 'DECLINED'}", file=out)
    print(f"  quotation sent   : {'yes' if decided.sent else 'no'}", file=out)
    print(f"  sent to          : {client_address if decided.sent else '— nobody'}", file=out)
    print(
        f"  rate provenance  : {'SIMULATED' if rates.uses_mock_data else 'live provider'}",
        file=out,
    )
    print(f"  STATUS           : {status.value}", file=out)

    print(f"\nAUDIT TRAIL\n{THIN}", file=out)
    print(_audit_block(audit.events), file=out)
    print(RULE, file=out)

    if not decided.was_approved:
        print("\n  Declined. No quotation was sent, and the request is terminal at", file=out)
        print("  MAKER_REJECTED — there is no transition out of it.\n", file=out)
        return EXIT_DECLINED

    return EXIT_OK
