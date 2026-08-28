"""Phase 10.5 — a real Gmail reply, correlated back to its enquiry and merged.

    real Gmail mailbox (read-only)
        -> RawEmail x N, oldest first
        -> InboundRouter
             -> HeaderChainCorrelation   (In-Reply-To / References)
             -> ClarificationWorkflow    (live extraction, merge, re-validation)
        -> the enquiry's record, now completed by its reply

The proof this command exists to produce is the merge: the reply says nothing
about origin, destination, weight or dimensions, and the shipment must still
carry all four afterwards. Those values come from the first email, through
`merge_shipment`, which was written for exactly this and is not touched here.

The approval gate is not bypassed and not automated. A drafted clarification
leaves its request at NEEDS_INFO, and the transition table permits only
NEEDS_INFO -> CLARIFICATION_SENT — so a reply cannot be processed until a named
person approves the draft. This command therefore refuses to continue past a
pending draft unless the operator supplies their own identity with
`--approved-by`. Approving still sends nothing: the sink collects in memory and
performs no I/O, and no Gmail send path exists anywhere in this codebase.
"""

from __future__ import annotations

import hashlib
import sys
from typing import TYPE_CHECKING, TextIO

from translog_quote import bootstrap
from translog_quote.config import load_settings
from translog_quote.errors import TranslogError
from translog_quote.interface.demo.formatting import (
    RULE,
    THIN,
    format_record,
    format_unresolved,
    format_validation,
)

if TYPE_CHECKING:
    from translog_quote.config import Settings
    from translog_quote.domain.email import RawEmail
    from translog_quote.pipeline import InboundRouter, RoutedMessage

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_GMAIL = 3
EXIT_EXTRACTION = 4
EXIT_NO_MESSAGE = 6
EXIT_AWAITING_APPROVAL = 7
EXIT_NOT_VALIDATED = 8

#: How many inbox messages one run may read. A conversation is an enquiry plus
#: its replies; this is a small ceiling, not a mailbox scan.
DEFAULT_MESSAGE_LIMIT = 5

_BANNER = f"""{RULE}
  TRANSLOG — PHASE 10.5: REAL GMAIL REPLY, CORRELATED AND MERGED
{RULE}
  GMAIL CONNECTION:  REAL (read-only)
  MESSAGES RECEIVED: REAL
  CORRELATION:       REAL (RFC In-Reply-To / References)
  AI EXTRACTION:     LIVE
  MERGE:             REAL / DETERMINISTIC
  VALIDATION:        REAL / DETERMINISTIC
  EMAIL SENDING:     NONE
  WEBCARGO:          MOCK / NOT CONTACTED
{RULE}"""


def _fail(out: TextIO, heading: str, detail: str) -> None:
    print(f"\n  {heading}", file=out)
    print(f"  {detail}\n", file=out)


def _request_id_for(email: RawEmail) -> str:
    """The id a first-contact enquiry gets. Derived from its ``Message-ID``, so
    a re-run addresses the same request instead of inventing a new one.

    Only ever consulted for a message the policy called ``NewRequest``: a reply
    takes the id of the request it was correlated to, never one derived from
    its own headers.
    """
    digest = hashlib.sha256(email.message_id.encode("utf-8")).hexdigest()[:10]
    return f"R-GMAIL-{digest}"


def _looks_like_a_reply_but_is_not_threaded(email: RawEmail) -> bool:
    """A "Re:" subject with no threading headers at all.

    Purely diagnostic — correlation never reads the subject, and this changes
    no decision. It exists because the failure it describes is invisible
    otherwise: a message composed fresh with "Re:" typed into the subject looks
    like a reply to a person and is correctly *not* one to the policy, and
    without saying so the run just reports an unexplained second enquiry.
    """
    return (
        email.subject.strip().lower().startswith("re:")
        and not email.in_reply_to
        and not email.references
    )


def _report_routing(out: TextIO, index: int, email: RawEmail, routed: RoutedMessage) -> None:
    kind = "REPLY -> correlated" if routed.is_reply else "NEW ENQUIRY"
    print(f"\n{THIN}", file=out)
    print(f"MESSAGE {index}  ({kind})", file=out)
    print(THIN, file=out)
    print(f"  subject     : {email.subject}", file=out)
    print(f"  received    : {email.received_at:%Y-%m-%d %H:%M %z}", file=out)
    print(f"  in-reply-to : {email.in_reply_to or '— none'}", file=out)
    print(f"  references  : {len(email.references)} id(s)", file=out)
    print(f"  request     : {routed.request_id or '— refused'}", file=out)

    if not routed.is_reply and _looks_like_a_reply_but_is_not_threaded(email):
        print(
            '\n  NOTE: the subject starts with "Re:" but the message carries no\n'
            "  In-Reply-To and no References, so it is not a reply — it was\n"
            "  composed as a new message. Correlation refuses to match on a\n"
            "  subject, so this correctly became its own enquiry. To test the\n"
            "  reply path, use Gmail's Reply button on the original enquiry.",
            file=out,
        )


def _report_pending(out: TextIO, pending: RoutedMessage) -> None:
    """Stop at the gate and say exactly what would release it."""
    print(f"\n{RULE}\nHUMAN APPROVAL REQUIRED\n{THIN}", file=out)
    print(f"  request {pending.request_id} is holding a clarification draft.", file=out)
    print("  A reply cannot be processed until a person approves it, and this", file=out)
    print("  command will not approve on your behalf. Re-run with:", file=out)
    print(
        "\n    python -m translog_quote.interface.demo gmail-thread "
        '--approved-by "your.name@company"\n',
        file=out,
    )
    print("  Approving records who approved it. It still sends no email:", file=out)
    print("  no Gmail send path exists in this codebase.", file=out)
    print(RULE, file=out)


def _approve(router: InboundRouter, pending: RoutedMessage, by: str, out: TextIO) -> None:
    """Release the held draft on the operator's explicit, named authority."""
    assert pending.request_id is not None  # a draft implies a routed request
    router.approve(pending.request_id, by=by)
    print(f"\n  clarification for {pending.request_id} approved by {by}", file=out)
    print("  (recorded; nothing was emailed — no sink can send)", file=out)


def run_gmail_thread(
    *,
    settings: Settings | None = None,
    approved_by: str | None = None,
    limit: int = DEFAULT_MESSAGE_LIMIT,
    out: TextIO = sys.stdout,
) -> int:
    """Read a real conversation, correlate each message, merge, re-validate."""
    settings = settings or load_settings()

    print(_BANNER, file=out)

    if settings.openrouter.api_key is None:
        _fail(
            out,
            "CONFIGURATION ERROR: no OpenRouter API key.",
            "Set TRANSLOG_OPENROUTER__API_KEY in .env (see .env.example).",
        )
        return EXIT_CONFIG

    # --- 1. receive ------------------------------------------------------------
    print("\n  ... reading the Gmail test mailbox (read-only) ...", file=out, flush=True)
    try:
        source = bootstrap.build_gmail_email_source(settings, max_results=limit)
        received = source.fetch_new()
    except TranslogError as exc:
        _fail(out, f"GMAIL RECEIVE FAILED: {type(exc).__name__}", str(exc))
        return EXIT_GMAIL

    if not received:
        _fail(
            out,
            "NO MESSAGE FOUND.",
            f"Nothing in the test mailbox matched query {settings.gmail.query!r}.",
        )
        return EXIT_NO_MESSAGE

    # Gmail lists newest first; a conversation only makes sense forwards. The
    # enquiry has to be processed before its reply can correlate to it.
    conversation = sorted(received, key=lambda e: e.received_at)
    print(f"  {len(conversation)} message(s), oldest first.", file=out)

    # --- 2. route each message through correlation + the workflow -------------
    router = bootstrap.build_inbound_router(settings, new_request_id=_request_id_for)
    last: RoutedMessage | None = None

    for index, email in enumerate(conversation, start=1):
        # A drafted clarification holds its request at NEEDS_INFO, and only a
        # named human moves it on. Without one, the run stops here rather than
        # quietly approving on the operator's behalf.
        if last is not None and last.outcome is not None and last.outcome.awaiting_approval:
            if approved_by is None:
                _report_pending(out, last)
                return EXIT_AWAITING_APPROVAL
            try:
                _approve(router, last, approved_by, out)
            except TranslogError as exc:
                _fail(out, f"APPROVAL FAILED: {type(exc).__name__}", str(exc))
                return EXIT_EXTRACTION

        try:
            routed = router.route(email)
        except TranslogError as exc:
            _fail(out, f"WORKFLOW FAILED: {type(exc).__name__}", str(exc))
            return EXIT_EXTRACTION

        _report_routing(out, index, email, routed)

        if routed.was_refused:
            print(f"\n  REFUSED — MANUAL REVIEW\n  {routed.reason}", file=out)
            continue

        assert routed.outcome is not None  # was_refused is exactly outcome is None
        stated = routed.outcome.merge.changed
        filled = ", ".join(f.value for f in stated) if stated else "— nothing new"
        print(f"\n  this message filled: {filled}", file=out)
        last = routed

    if last is None or last.outcome is None:
        _fail(out, "NOTHING WAS PROCESSED.", "Every message was refused correlation.")
        return EXIT_NO_MESSAGE

    # --- 3. the merged shipment, and what it proves ---------------------------
    outcome = last.outcome
    print(f"\n{RULE}\nMERGED SHIPMENT  ({last.request_id})\n{THIN}", file=out)
    print(format_record(outcome.record), file=out)
    print(f"\n{format_validation(outcome.validation)}", file=out)
    print(f"\nUNRESOLVED\n{THIN}", file=out)
    print(format_unresolved(outcome.analysis.unresolved), file=out)

    correlated = "yes — merged into the existing enquiry" if last.is_reply else "no — first message"
    print(f"\nOUTCOME\n{THIN}", file=out)
    print(f"  request state : {outcome.state.value}", file=out)
    print(f"  correlated    : {correlated}", file=out)
    print("  email sent    : no", file=out)
    print(f"{RULE}", file=out)

    return EXIT_OK if outcome.is_complete else EXIT_NOT_VALIDATED
