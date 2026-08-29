"""Phase 10.3 — one real Gmail message through the existing pipeline.

    real Gmail test mailbox
        -> GmailEmailSource          (via bootstrap; receive-only)
        -> RawEmail                  (existing domain type, unchanged)
        -> ExtractionPort            (existing live Qwen extraction)
        -> ExtractionResult          (existing contract)
        -> validate_shipment         (existing deterministic validation)

Presentation only, like every demo module: no extraction logic, no HTTP, no
business rule, adapters reached only through `bootstrap`. Nothing here can
send email — no sink is even constructed — and WebCargo is never contacted.

`gmail-auth` is the companion one-time consent command. It runs only when a
person invokes it; nothing in this project authenticates on its own.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TextIO

from translog_quote import bootstrap
from translog_quote.config import load_settings
from translog_quote.domain.extraction import to_extracted_fields
from translog_quote.domain.shipment import RequestSource, build_initial_record
from translog_quote.domain.validation import validate_shipment
from translog_quote.errors import TranslogError
from translog_quote.interface.demo.formatting import (
    RULE,
    format_email,
    format_extraction,
    format_footer,
    format_validation,
)

if TYPE_CHECKING:
    from translog_quote.config import Settings

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_GMAIL = 3
EXIT_EXTRACTION = 4
EXIT_INVALID_SHIPMENT = 5
EXIT_NO_MESSAGE = 6

_REQUEST_ID = "R-GMAIL-TEST-1"

_BANNER = f"""{RULE}
  TRANSLOG — PHASE 10.3: ONE REAL EMAIL INTO THE EXISTING PIPELINE
{RULE}
  GMAIL CONNECTION: REAL
  MESSAGE RECEIVED: REAL
  AI EXTRACTION:    LIVE
  VALIDATION:       REAL
  EMAIL SENDING:    NONE
  WEBCARGO:         MOCK / NOT CONTACTED
{RULE}"""


def _fail(out: TextIO, heading: str, detail: str) -> None:
    print(f"\n  {heading}", file=out)
    print(f"  {detail}\n", file=out)


def run_gmail_test(*, settings: Settings | None = None, out: TextIO = sys.stdout) -> int:
    """Receive one real email, extract, validate, display. Returns an exit
    code; expected failures print a readable reason rather than raising."""
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
    print("\n  ... connecting to the Gmail test mailbox (read-only) ...", file=out, flush=True)
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
            "Send the test email, wait a moment, and run this command again.",
        )
        return EXIT_NO_MESSAGE

    email = emails[0]
    print(f"\n{format_email(email)}", file=out)

    # --- 3. existing live extraction -------------------------------------------
    print(f"\n  ... calling {settings.openrouter.model} ...", file=out, flush=True)
    try:
        extractor = bootstrap.build_extractor(settings)
        result = extractor.extract_shipment(email.body_text)
    except TranslogError as exc:
        _fail(out, f"EXTRACTION FAILED: {type(exc).__name__}", str(exc))
        return EXIT_EXTRACTION

    print(f"\n{format_extraction(result)}", file=out)

    # --- 4. existing deterministic validation ----------------------------------
    record = build_initial_record(_REQUEST_ID, RequestSource.EMAIL, to_extracted_fields(result))
    validation = validate_shipment(record)

    print(f"\n{format_validation(validation)}", file=out)
    print(f"\n{format_footer(is_valid=validation.is_valid)}", file=out)

    return EXIT_OK if validation.is_valid else EXIT_INVALID_SHIPMENT


def run_gmail_auth(*, settings: Settings | None = None, out: TextIO = sys.stdout) -> int:
    """One-time interactive OAuth consent for the test mailbox."""
    settings = settings or load_settings()

    print(f"{RULE}\n  GMAIL ONE-TIME AUTHORIZATION (scope: gmail.readonly)\n{RULE}", file=out)
    print(
        "\n  A browser will open on Google's consent page. Sign in with the\n"
        "  TEST account only. No password ever passes through this program.\n",
        file=out,
        flush=True,
    )
    try:
        token_path = bootstrap.authorize_gmail(settings)
    except TranslogError as exc:
        _fail(out, f"AUTHORIZATION FAILED: {type(exc).__name__}", str(exc))
        return EXIT_CONFIG

    print(f"  Authorization complete. Token stored at {token_path} (git-ignored).", file=out)
    print("  Next: python -m translog_quote.interface.demo gmail-test\n", file=out)
    return EXIT_OK


def run_gmail_auth_send(*, settings: Settings | None = None, out: TextIO = sys.stdout) -> int:
    """One-time interactive OAuth consent for **sending** (Phase 11).

    A second consent run, granting a second scope into a second token file. The
    read-only credential this project already holds is untouched: after this
    there is one credential that can only read the mailbox and one that can
    only send from it, and no single token can do both.
    """
    settings = settings or load_settings()

    print(f"{RULE}\n  GMAIL ONE-TIME AUTHORIZATION (scope: gmail.send)\n{RULE}", file=out)
    print(
        "\n  A browser will open on Google's consent page. Sign in with the\n"
        "  TRANSLOG account — the mailbox quotations are sent FROM. No password\n"
        "  ever passes through this program.\n"
        "\n  This grant can send mail and cannot read any. It is stored\n"
        "  separately from the read-only token used to ingest client mail.\n",
        file=out,
        flush=True,
    )
    try:
        token_path = bootstrap.authorize_gmail_send(settings)
    except TranslogError as exc:
        _fail(out, f"AUTHORIZATION FAILED: {type(exc).__name__}", str(exc))
        return EXIT_CONFIG

    print(f"  Authorization complete. Token stored at {token_path} (git-ignored).", file=out)
    print("  Next: set TRANSLOG_GMAIL__SEND_ENABLED=true and", file=out)
    print("        TRANSLOG_GMAIL__APPROVER_ADDRESS=<internal mailbox> in .env,", file=out)
    print("        then: python -m translog_quote.interface.demo gmail-quote\n", file=out)
    return EXIT_OK
