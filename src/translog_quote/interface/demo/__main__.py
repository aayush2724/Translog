"""Entry point: ``python -m translog_quote.interface.demo [scenario]``."""

from __future__ import annotations

import sys

from translog_quote.interface.demo.clarification_demo import run_demo as run_clarification_demo
from translog_quote.interface.demo.extraction_demo import DEFAULT_SCENARIO, run_demo
from translog_quote.interface.demo.gmail_demo import run_gmail_auth, run_gmail_test
from translog_quote.interface.demo.poc_demo import run_demo as run_poc_demo
from translog_quote.interface.demo.rate_demo import run_demo as run_rate_demo


def main(argv: list[str] | None = None) -> int:
    """``python -m translog_quote.interface.demo
    [poc|scenario|clarification|rates|gmail-test|gmail-auth]``."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "clarification":
        return run_clarification_demo()
    if args and args[0] == "rates":
        return run_rate_demo()
    if args and args[0] == "gmail-test":
        return run_gmail_test()
    if args and args[0] == "gmail-auth":
        return run_gmail_auth()
    if args and args[0] in {"poc", "full"}:
        return run_poc_demo()
    scenario = args[0] if args else DEFAULT_SCENARIO
    return run_demo(scenario=scenario)


if __name__ == "__main__":
    raise SystemExit(main())
