"""Entry point: ``python -m translog_quote.interface.demo [scenario]``."""

from __future__ import annotations

import sys

from translog_quote.interface.demo.extraction_demo import DEFAULT_SCENARIO, run_demo


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    scenario = args[0] if args else DEFAULT_SCENARIO
    return run_demo(scenario=scenario)


if __name__ == "__main__":
    raise SystemExit(main())
