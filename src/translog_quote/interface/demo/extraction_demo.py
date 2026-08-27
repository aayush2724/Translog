"""One fixture email, through the real pipeline, printed for an audience.

    fixture .eml
        -> FixtureEmailSource        (Phase 3, via bootstrap)
        -> ExtractionPort            (Phase 5, live Qwen 3.7 Flash)
        -> ExtractionResult          (Phase 4 contract, strictly validated)
        -> to_extracted_fields       (Phase 4 mapping)
        -> ShipmentRecord            (Phase 2)
        -> validate_shipment         (Phase 2, deterministic, no model)

Every step is the real one. This module adds presentation and nothing else — it
holds no extraction logic, no HTTP, no business rule, and no second
implementation of anything. It reaches the adapters only through `bootstrap`,
because `interface` may not name them.
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
    format_email,
    format_extraction,
    format_footer,
    format_header,
    format_validation,
)

if TYPE_CHECKING:
    from translog_quote.config import Settings

DEFAULT_SCENARIO = "a_complete_request"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_FIXTURE = 3
EXIT_EXTRACTION = 4
EXIT_INVALID_SHIPMENT = 5


def _fail(out: TextIO, heading: str, detail: str) -> None:
    print(f"\n  {heading}", file=out)
    print(f"  {detail}\n", file=out)


def run_demo(
    *,
    settings: Settings | None = None,
    scenario: str = DEFAULT_SCENARIO,
    out: TextIO = sys.stdout,
) -> int:
    """Run the demo. Returns a process exit code; never raises for an expected
    failure, so the reason is always something a viewer can read."""
    settings = settings or load_settings()

    print(format_header(settings.openrouter.model), file=out)

    # --- 1. configuration -----------------------------------------------------
    if settings.openrouter.api_key is None:
        _fail(
            out,
            "CONFIGURATION ERROR: no OpenRouter API key.",
            "Set TRANSLOG_OPENROUTER__API_KEY in .env (see .env.example).",
        )
        return EXIT_CONFIG

    # --- 2. the fixture email -------------------------------------------------
    try:
        source = bootstrap.build_fixture_email_source(settings, scenario)
        emails = source.fetch_new()
    except (OSError, ValueError) as exc:
        _fail(out, f"FIXTURE ERROR: could not load scenario '{scenario}'.", str(exc))
        return EXIT_FIXTURE

    if not emails:
        _fail(out, f"FIXTURE ERROR: scenario '{scenario}' contains no emails.", str(scenario))
        return EXIT_FIXTURE

    email = emails[0]
    print(f"\n{format_email(email)}", file=out)

    # --- 3. live extraction ---------------------------------------------------
    print(f"\n  ... calling {settings.openrouter.model} ...", file=out, flush=True)
    try:
        extractor = bootstrap.build_extractor(settings)
        result = extractor.extract_shipment(email.body_text)
    except TranslogError as exc:
        _fail(out, f"EXTRACTION FAILED: {type(exc).__name__}", str(exc))
        return EXIT_EXTRACTION

    print(f"\n{format_extraction(result)}", file=out)

    # --- 4. deterministic validation ------------------------------------------
    record = build_initial_record(
        f"R-DEMO-{scenario[:1].upper()}", RequestSource.EMAIL, to_extracted_fields(result)
    )
    validation = validate_shipment(record)

    print(f"\n{format_validation(validation)}", file=out)
    print(f"\n{format_footer(is_valid=validation.is_valid)}", file=out)

    return EXIT_OK if validation.is_valid else EXIT_INVALID_SHIPMENT
