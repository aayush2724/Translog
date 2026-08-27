"""Entry point: ``python -m translog_quote.interface.demo [scenario]``."""

from __future__ import annotations

import sys

from translog_quote.interface.demo.clarification_demo import run_demo as run_clarification_demo
from translog_quote.interface.demo.extraction_demo import DEFAULT_SCENARIO, run_demo


def main(argv: list[str] | None = None) -> int:
    """``python -m translog_quote.interface.demo [scenario|clarification]``."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "clarification":
        return run_clarification_demo()
    scenario = args[0] if args else DEFAULT_SCENARIO
    return run_demo(scenario=scenario)


if __name__ == "__main__":
    raise SystemExit(main())
