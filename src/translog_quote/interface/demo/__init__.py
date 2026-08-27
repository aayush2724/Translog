"""The four demonstration scenarios.

Each is a script *and* an assertion over the resulting state and audit trail — an
executable specification, not a manual walkthrough. A scenario that stops passing
is a failing test, which is what keeps the demo honest as the code changes.

    S1  complete email -> quotation -> accept
    S2  incomplete -> clarification -> reply -> quotation
    S3  many rates -> filtering -> fastest eligible
    S4  quotation -> rejection

None of those four exist yet; they need the whole pipeline (Phase 13).

What does exist is a live extraction demo covering the part that is built --
fixture email, real Qwen extraction, deterministic validation:

    python -m translog_quote.interface.demo [scenario]
"""

from translog_quote.interface.demo.extraction_demo import DEFAULT_SCENARIO, run_demo

__all__ = ["DEFAULT_SCENARIO", "run_demo"]
