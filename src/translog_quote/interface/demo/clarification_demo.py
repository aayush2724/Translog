"""A two-round clarification conversation, printed for an audience.

Real extraction (live Qwen), real merge, real validator, real composer, real
state machine. The only thing simulated is the mailbox: the client's reply is a
fixture rather than something that arrived, and the clarification is written to
an outbox instead of being sent.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TextIO

from translog_quote import bootstrap
from translog_quote.config import load_settings
from translog_quote.domain.email import RawEmail
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

REQUEST_ID = "R-DEMO-CLARIFY"

#: An incomplete first enquiry, in the shape real clients actually send.
INITIAL_EMAIL = RawEmail(
    message_id="<c1@shreejiforwarder.example>",
    from_address="buyer@shreejiforwarder.example",
    subject="Rate required - Ahmedabad to Bahrain",
    body_text=(
        "Dear Ma'am,\n\n"
        "Please provide a rate for 500 Kgs cargo from Ahmedabad to Bahrain.\n"
        "Cargo dimension 24 (width) x 34 (length) x 6 (breadth) inches.\n"
        "Cargo type Non Haz.\n\n"
        "Thanks & Regards,\n"
        "Mehul\n"
    ),
    received_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
)

#: The reply supplies only what was asked for — it does not restate the shipment.
REPLY_EMAIL = RawEmail(
    message_id="<c2@shreejiforwarder.example>",
    from_address="buyer@shreejiforwarder.example",
    subject="Re: Rate required - Ahmedabad to Bahrain",
    body_text=(
        "Dear Ma'am,\n\n"
        "Commodity : POLYISOBUTYLENE ADDITIVE\n"
        "No of Pcs : 20 Bags\n"
        "This is a chemical product. MSDS is attached.\n"
        "Airport to airport is fine.\n\n"
        "Thanks & Regards,\n"
        "Mehul\n"
    ),
    received_at=datetime(2026, 9, 1, 14, 30, tzinfo=UTC),
)

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_EXTRACTION = 4
EXIT_INCOMPLETE = 5


def run_demo(*, settings: Settings | None = None, out: TextIO = sys.stdout) -> int:
    settings = settings or load_settings()

    print(RULE, file=out)
    print("TRANSLOG CLARIFICATION DEMO", file=out)
    print(RULE, file=out)
    print(f"Model: {settings.openrouter.model}   (live extraction via OpenRouter)", file=out)
    print("Deterministic: merge, validation, clarification wording, state machine\n", file=out)

    if settings.openrouter.api_key is None:
        print("  CONFIGURATION ERROR: no OpenRouter API key.", file=out)
        print("  Set TRANSLOG_OPENROUTER__API_KEY in .env (see .env.example).\n", file=out)
        return EXIT_CONFIG

    # The outbound message is read off the TurnOutcome, not off the sink: the
    # workflow already hands back exactly what it composed, and `interface` may
    # not name a concrete adapter to inspect one.
    workflow = bootstrap.build_clarification_workflow(settings)

    try:
        first = workflow.handle(REQUEST_ID, INITIAL_EMAIL)
    except TranslogError as exc:
        print(f"\n  EXTRACTION FAILED: {type(exc).__name__}\n  {exc}\n", file=out)
        return EXIT_EXTRACTION

    print("INITIAL CLIENT EMAIL", file=out)
    print(THIN, file=out)
    for line in INITIAL_EMAIL.body_text.strip().splitlines():
        print(f"  {line}", file=out)

    print(f"\nEXTRACTED SHIPMENT\n{THIN}", file=out)
    print(format_record(first.record), file=out)
    print(f"\n{format_validation(first.validation)}", file=out)
    print(f"\nUNRESOLVED\n{THIN}", file=out)
    print(format_unresolved(first.analysis.unresolved), file=out)

    if first.clarification is None:
        print("\n  (no clarification needed — nothing to demonstrate)\n", file=out)
        return EXIT_OK

    print(f"\nCLARIFICATION SENT  (round {first.round_number})\n{THIN}", file=out)
    for line in first.clarification.body_text.splitlines():
        print(f"  {line}", file=out)

    try:
        second = workflow.handle(REQUEST_ID, REPLY_EMAIL)
    except TranslogError as exc:
        print(f"\n  EXTRACTION FAILED: {type(exc).__name__}\n  {exc}\n", file=out)
        return EXIT_EXTRACTION

    print(f"\nCLIENT REPLY\n{THIN}", file=out)
    for line in REPLY_EMAIL.body_text.strip().splitlines():
        print(f"  {line}", file=out)

    print(f"\nUPDATED SHIPMENT\n{THIN}", file=out)
    print(format_record(second.record), file=out)
    kept = [f for f in ("origin", "destination", "weight_kg") if getattr(second.record, f)]
    print(f"\n  carried over from the first email: {', '.join(kept)}", file=out)
    print(f"  filled by the reply: {', '.join(f.value for f in second.merge.changed)}", file=out)

    print(f"\n{format_validation(second.validation)}", file=out)
    print(f"\nSTATE\n{THIN}", file=out)
    print(f"{first.state.value}  ->  {second.state.value}", file=out)
    rounds_used = second.round_number
    print(f"clarifications sent: {rounds_used}", file=out)
    print(RULE, file=out)

    return EXIT_OK if second.is_complete else EXIT_INCOMPLETE
