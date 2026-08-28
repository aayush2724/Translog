"""Phase 10.4 — a real Gmail enquiry into the human-gated clarification loop.

    real Gmail test mailbox
        -> GmailEmailSource         (Phase 10.3, receive-only, unchanged)
        -> RawEmail                 (existing domain type, unchanged)
        -> ClarificationWorkflow    (existing pipeline, unchanged)
             -> live Qwen extraction
             -> deterministic merge + validation
             -> deterministic clarification draft
        -> STOP: the draft waits for a named human approver

The command's whole job is presentation and wiring; every decision it displays
was made by code that already existed. It calls `workflow.handle` and never
`workflow.approve_clarification` — releasing a draft requires a person, and
this command has no way to be that person.

What it deliberately does not do: no `EmailSink` is constructed here, no Gmail
write of any kind is possible (the OAuth scope is read-only and the transport
issues GETs), no rate provider is built, and no reply correlation is attempted.
Correlation is the next phase; see `_request_id_for`.
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
    format_email,
    format_record,
    format_unresolved,
    format_validation,
)
from translog_quote.pipeline.audit import AuditEvent, AuditEventType

if TYPE_CHECKING:
    from translog_quote.config import Settings
    from translog_quote.domain.email import RawEmail

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_GMAIL = 3
EXIT_EXTRACTION = 4
EXIT_NO_MESSAGE = 6

_BANNER = f"""{RULE}
  TRANSLOG — PHASE 10.4: REAL INBOUND MAIL INTO THE CLARIFICATION LOOP
{RULE}
  GMAIL CONNECTION:  REAL (read-only)
  MESSAGE RECEIVED:  REAL
  AI EXTRACTION:     LIVE
  VALIDATION:        REAL / DETERMINISTIC
  CLARIFICATION:     DRAFTED, NOT SENT
  EMAIL SENDING:     NONE
  WEBCARGO:          MOCK / NOT CONTACTED
{RULE}"""


class _CollectingAudit:
    """Keeps the workflow's evidence trail so the command can print it.

    A presentation concern, which is why it lives here rather than in
    `adapters/`: the events are the argument that the approval gate held, and
    an argument nobody can read proves nothing.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def _fail(out: TextIO, heading: str, detail: str) -> None:
    print(f"\n  {heading}", file=out)
    print(f"  {detail}\n", file=out)


def _request_id_for(email: RawEmail) -> str:
    """One request per inbound message, derived from its RFC ``Message-ID``.

    Stable across runs, so re-running the command on the same email addresses
    the same request rather than inventing a new one. It is **not** thread
    correlation: a reply to an earlier enquiry gets its own id here, because
    matching a reply to its request is `CorrelationPolicy`'s job and no
    concrete policy exists yet (AMB-11). Guessing one here would put a
    correlation rule in a presentation module, which is precisely where it
    must never live.
    """
    digest = hashlib.sha256(email.message_id.encode("utf-8")).hexdigest()[:10]
    return f"R-GMAIL-{digest}"


def _audit_block(events: list[AuditEvent]) -> str:
    lines = []
    for event in events:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(event.detail.items()))
        lines.append(f"  {event.event.value:<24} {detail}")
    return "\n".join(lines)


def run_gmail_process(*, settings: Settings | None = None, out: TextIO = sys.stdout) -> int:
    """Receive one real enquiry and run it through the real clarification loop,
    stopping at the approval boundary. Returns a process exit code."""
    settings = settings or load_settings()

    print(_BANNER, file=out)

    # --- 1. configuration -----------------------------------------------------
    if settings.openrouter.api_key is None:
        _fail(
            out,
            "CONFIGURATION ERROR: no OpenRouter API key.",
            "Set TRANSLOG_OPENROUTER__API_KEY in .env (see .env.example).",
        )
        return EXIT_CONFIG

    # --- 2. receive one real message ------------------------------------------
    print("\n  ... reading the Gmail test mailbox (read-only) ...", file=out, flush=True)
    try:
        source = bootstrap.build_gmail_email_source(settings)
        emails = source.fetch_new()
    except TranslogError as exc:
        _fail(out, f"GMAIL RECEIVE FAILED: {type(exc).__name__}", str(exc))
        return EXIT_GMAIL

    if not emails:
        _fail(
            out,
            "NO MESSAGE FOUND.",
            f"Nothing in the test mailbox matched query {settings.gmail.query!r}. "
            "Send the test enquiry, wait a moment, and run this command again.",
        )
        return EXIT_NO_MESSAGE

    email = emails[0]
    request_id = _request_id_for(email)
    print(f"\n{format_email(email)}", file=out)

    # --- 3. the existing workflow, unchanged ----------------------------------
    print(f"\n  ... running the clarification workflow ({request_id}) ...", file=out, flush=True)
    audit = _CollectingAudit()
    try:
        workflow = bootstrap.build_clarification_workflow(settings, audit=audit)
        outcome = workflow.handle(request_id, email)
    except TranslogError as exc:
        _fail(out, f"WORKFLOW FAILED: {type(exc).__name__}", str(exc))
        return EXIT_EXTRACTION

    print(f"\nEXTRACTED SHIPMENT\n{THIN}", file=out)
    print(format_record(outcome.record), file=out)
    print(f"\n{format_validation(outcome.validation)}", file=out)
    print(f"\nUNRESOLVED\n{THIN}", file=out)
    print(format_unresolved(outcome.analysis.unresolved), file=out)

    # --- 4. the draft, and the boundary it stops at ---------------------------
    if outcome.clarification is None:
        print(f"\nOUTCOME\n{THIN}", file=out)
        print(f"  state: {outcome.state.value}", file=out)
        print("  No clarification was needed — the enquiry is complete.", file=out)
        print("  Nothing was sent; the next step is a person's decision.", file=out)
        print(f"\nAUDIT TRAIL\n{THIN}", file=out)
        print(_audit_block(audit.events), file=out)
        print(f"\n{RULE}", file=out)
        return EXIT_OK

    print(f"\nCLARIFICATION DRAFT  (round {outcome.round_number})\n{THIN}", file=out)
    for line in outcome.clarification.body_text.splitlines():
        print(f"  {line}", file=out)

    print(f"\nAUDIT TRAIL\n{THIN}", file=out)
    print(_audit_block(audit.events), file=out)

    # Read back from the workflow rather than asserting from memory: this is
    # the state a reviewer cares about, so it is reported as observed.
    still_pending = workflow.pending_draft(request_id) is not None
    was_sent = any(e.event is AuditEventType.CLARIFICATION_SENT for e in audit.events)

    print(f"\nHUMAN APPROVAL REQUIRED\n{THIN}", file=out)
    print(f"  request state       : {outcome.state.value}", file=out)
    print(f"  draft pending       : {'yes' if still_pending else 'no'}", file=out)
    print(f"  email sent          : {'yes' if was_sent else 'no'}", file=out)
    print(f"  awaiting approval   : {'yes' if outcome.awaiting_approval else 'no'}", file=out)
    print(
        "\n  The draft is held. Releasing it requires an explicit, named human\n"
        "  approval, and this command has no path to give one.",
        file=out,
    )
    print(f"{RULE}", file=out)

    return EXIT_OK
