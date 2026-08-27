"""CLI: ``python -m translog_quote.evaluation``.

Live model calls cost money, so they never happen by accident: every run that
would contact OpenRouter requires an explicit ``--live``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from translog_quote.config import load_settings
from translog_quote.evaluation.ground_truth import load_ground_truth
from translog_quote.evaluation.report import (
    render_single_case,
    render_summary,
    results_as_json,
)
from translog_quote.evaluation.runner import DEFAULT_CORPUS, CaseRun, build_extractor, run_case


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m translog_quote.evaluation",
        description="Evaluate extraction against the real client case corpus.",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--case", type=int, help="run one case by number")
    group.add_argument("--all", action="store_true", help="run every case with ground truth")
    p.add_argument(
        "--live",
        action="store_true",
        help="required: confirms real OpenRouter calls (and real spend)",
    )
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--ground-truth", type=Path, default=None)
    p.add_argument("--no-email", action="store_true", help="omit client email text from output")
    p.add_argument("--json", type=Path, default=None, help="write field-level results here")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if not args.live:
        print(
            "Refusing to run: this evaluation calls Qwen 3.7 Flash through OpenRouter\n"
            "and spends real credit. Re-run with --live to confirm.",
            file=sys.stderr,
        )
        return 2

    settings = load_settings()
    if settings.openrouter.api_key is None:
        print(
            "No OpenRouter API key. Set TRANSLOG_OPENROUTER__API_KEY in .env.",
            file=sys.stderr,
        )
        return 2

    try:
        truth = load_ground_truth(args.ground_truth)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    extractor = build_extractor(settings)
    cases = [args.case] if args.case is not None else sorted(truth)

    missing = [c for c in cases if c not in truth]
    if missing:
        print(f"No reviewed ground truth for case(s): {missing}", file=sys.stderr)
        return 2

    runs: list[CaseRun] = []
    for case in cases:
        run = run_case(case, truth[case], extractor, args.corpus)
        runs.append(run)
        if args.case is not None:
            print(render_single_case(run, truth[case], show_email=not args.no_email))
        else:
            assert run.score is not None or run.error is not None
            status = run.score.verdict if run.score else f"ERROR({run.error_kind})"
            detail = f"{run.score.passed}/{run.score.total}" if run.score else ""
            print(f"  case {case:>3}  {status:<8} {detail}", flush=True)

    if args.all:
        print()
        print(render_summary(runs))

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results_as_json(runs), indent=1), encoding="utf-8")
        print(f"\nfield-level results written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
