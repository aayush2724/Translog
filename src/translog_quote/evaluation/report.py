"""Rendering evaluation results. Pure functions over scores."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from translog_quote.domain.extraction import ExtractionResult, FieldStatus
from translog_quote.evaluation.ground_truth import CANONICAL_FIELDS, ExpectedCase
from translog_quote.evaluation.scoring import Outcome

if TYPE_CHECKING:
    from translog_quote.evaluation.runner import CaseRun

RULE = "=" * 72
THIN = "-" * 72
_W = max(len(f) for f in CANONICAL_FIELDS) + 2


def _value_of(result: ExtractionResult, field: str) -> str:
    extracted = getattr(result, field)
    if extracted.status is FieldStatus.STATED:
        return str(extracted.value)
    return f"<{extracted.status.value}>"


def render_single_case(run: CaseRun, expected: ExpectedCase, *, show_email: bool = True) -> str:
    lines = [RULE, f"CLIENT CASE {run.case}", RULE]

    if run.loaded is not None:
        lines += [
            "",
            "THREAD STRUCTURE",
            THIN,
            f"messages: {len(run.loaded.messages)}   "
            f"client: {run.loaded.client_message_count}   "
            f"translog: {len(run.loaded.messages) - run.loaded.client_message_count}",
        ]
        if show_email:
            lines += ["", "CLIENT EMAIL CONTENT (Translog messages excluded)", THIN]
            lines += [f"  {line}" for line in run.loaded.client_text.splitlines()]

    if run.error is not None:
        lines += ["", f"FAILED ({run.error_kind}): {run.error}", RULE]
        return "\n".join(lines)

    assert run.result is not None and run.score is not None

    lines += ["", "QWEN EXTRACTION", THIN]
    for field in CANONICAL_FIELDS:
        lines.append(f"{field.ljust(_W)} {_value_of(run.result, field)}")

    lines += ["", "EXPECTED (manually reviewed from the client's own messages)", THIN]
    for field in CANONICAL_FIELDS:
        exp = expected.expected(field)
        shown = str(exp.value) if exp.status is FieldStatus.STATED else f"<{exp.status.value}>"
        lines.append(f"{field.ljust(_W)} {shown}")

    lines += ["", "FIELD RESULTS", THIN]
    for score in run.score.fields:
        mark = "PASS" if score.is_pass else "FAIL"
        detail = "" if score.outcome is Outcome.CORRECT else f"   [{score.outcome.value}]"
        lines.append(f"{score.field.ljust(_W)} {mark}{detail}")
        if not score.is_pass:
            lines.append(f"{' ' * _W}      expected: {score.expected_repr}")
            lines.append(f"{' ' * _W}      actual:   {score.actual_repr}")

    lines += [
        "",
        "OVERALL",
        THIN,
        f"{run.score.verdict}   ({run.score.passed}/{run.score.total} fields correct)",
        RULE,
    ]
    return "\n".join(lines)


def render_summary(runs: list[CaseRun]) -> str:
    """Aggregate metrics. Every number is counted from actual comparisons."""
    scored = [r for r in runs if r.score is not None]
    failed = [r for r in runs if r.score is None]

    verdicts = Counter(r.score.verdict for r in scored if r.score)
    error_kinds = Counter(r.error_kind for r in failed)

    lines = [RULE, "EVALUATION SUMMARY", RULE, "", "CASES", THIN]
    lines += [
        f"attempted             {len(runs)}",
        f"scored                {len(scored)}",
        f"completely correct    {verdicts['PASS']}",
        f"partially correct     {verdicts['PARTIAL']}",
        f"completely wrong      {verdicts['FAIL']}",
        f"not scored (errors)   {len(failed)}",
    ]
    for kind, count in sorted(error_kinds.items()):
        lines.append(f"   {str(kind):<18} {count}")

    field_stats: dict[str, Counter[str]] = {f: Counter() for f in CANONICAL_FIELDS}
    for run in scored:
        assert run.score is not None
        for score in run.score.fields:
            field_stats[score.field][score.outcome.value] += 1

    total_fields = sum(sum(c.values()) for c in field_stats.values())
    total_pass = sum(
        c[Outcome.CORRECT.value] + c[Outcome.CORRECT_CONTAINED.value] for c in field_stats.values()
    )

    lines += ["", "FIELD ACCURACY", THIN]
    lines.append(f"{'field'.ljust(_W)} {'acc':>7}  {'pass':>5} {'of':>4}   breakdown")
    for field in CANONICAL_FIELDS:
        counts = field_stats[field]
        n = sum(counts.values())
        ok = counts[Outcome.CORRECT.value] + counts[Outcome.CORRECT_CONTAINED.value]
        pct = f"{(ok / n * 100):.1f}%" if n else "n/a"
        breakdown = " ".join(
            f"{k}={v}" for k, v in sorted(counts.items()) if k != Outcome.CORRECT.value and v
        )
        lines.append(f"{field.ljust(_W)} {pct:>7}  {ok:>5} {n:>4}   {breakdown}")

    overall = f"{(total_pass / total_fields * 100):.1f}%" if total_fields else "n/a"
    lines += ["", f"{'OVERALL FIELD ACCURACY'.ljust(_W)} {overall:>7}  {total_pass}/{total_fields}"]

    all_outcomes: Counter[str] = Counter()
    for counts in field_stats.values():
        all_outcomes.update(counts)

    lines += ["", "ERROR MODES", THIN]
    for outcome in Outcome:
        lines.append(f"{outcome.value.ljust(_W)} {all_outcomes[outcome.value]}")

    lines.append(RULE)
    return "\n".join(lines)


def results_as_json(runs: list[CaseRun]) -> dict[str, object]:
    """Field-level results, for comparing two runs against each other.

    The aggregate percentages in a text report cannot say *which* field of
    *which* case changed between two prompts. This can, and it is what makes a
    before/after claim checkable rather than asserted.
    """
    cases: list[dict[str, object]] = []
    for run in runs:
        entry: dict[str, object] = {"case": run.case}
        if run.score is None:
            entry["error"] = run.error
            entry["error_kind"] = run.error_kind
        else:
            entry["verdict"] = run.score.verdict
            entry["passed"] = run.score.passed
            entry["fields"] = {
                f.field: {
                    "outcome": f.outcome.value,
                    "expected": f.expected_repr,
                    "actual": f.actual_repr,
                }
                for f in run.score.fields
            }
        cases.append(entry)
    return {"cases": cases}
