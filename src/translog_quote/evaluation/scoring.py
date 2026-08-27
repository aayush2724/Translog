"""Comparing what the model extracted against what the client actually said.

The comparison is deliberately unforgiving about *status*: claiming a value the
client never gave is the failure this whole exercise exists to detect, and it is
counted separately from getting a stated value wrong.

It is a little forgiving about *free-text specificity* — "Bahrain" against
"Bahrain (Hidd Industrial Area)" is the same destination described at different
lengths, and both strings came from the same email. Those are recorded as
`contained` rather than `exact` so the report shows how many passes leaned on
it, and nobody has to take the leniency on trust.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from translog_quote.domain.extraction import ExtractionResult, FieldStatus
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.evaluation.ground_truth import CANONICAL_FIELDS, ExpectedCase, ExpectedField

_FREE_TEXT = frozenset({"origin", "destination", "commodity", "cargo_type", "delivery_address"})
_NUMERIC_TOLERANCE = 0.01


class Outcome(StrEnum):
    """Why a field passed or failed. The failure kinds are the interesting part."""

    CORRECT = "correct"
    CORRECT_CONTAINED = "correct_contained"
    HALLUCINATED = "hallucinated"
    """Client said nothing; the model stated a value."""

    MISSED = "missed"
    """Client stated a value; the model reported nothing."""

    WRONG_VALUE = "wrong_value"
    """Both stated a value; they disagree."""

    STATUS_MISMATCH = "status_mismatch"
    """Neither of the above — e.g. expected ambiguous, got stated."""

    @property
    def is_pass(self) -> bool:
        return self in (Outcome.CORRECT, Outcome.CORRECT_CONTAINED)


@dataclass(frozen=True, slots=True)
class FieldScore:
    field: str
    outcome: Outcome
    expected_repr: str
    actual_repr: str

    @property
    def is_pass(self) -> bool:
        return self.outcome.is_pass


@dataclass(frozen=True, slots=True)
class CaseScore:
    case: int
    fields: tuple[FieldScore, ...]
    error: str | None = None

    @property
    def passed(self) -> int:
        return sum(1 for f in self.fields if f.is_pass)

    @property
    def total(self) -> int:
        return len(self.fields)

    @property
    def verdict(self) -> str:
        if self.error:
            return "ERROR"
        if self.passed == self.total:
            return "PASS"
        return "FAIL" if self.passed == 0 else "PARTIAL"


#: Dash characters that mean the same thing as a plain hyphen in an address or a
#: lane description. Clients write "TX - 77041"; a PDF export, an editor's
#: autocorrect, or the model itself renders it "TX – 77041". Scoring one wrong
#: against the other measures the font, not the extraction.
#:
#: These are UNIFIED to a hyphen, never removed. Deleting them would make
#: "500-700" and "500700" compare equal, which is a genuinely different value.
_DASH_VARIANTS = {
    0x2010: "-",  # HYPHEN
    0x2011: "-",  # NON-BREAKING HYPHEN
    0x2012: "-",  # FIGURE DASH
    0x2013: "-",  # EN DASH
    0x2014: "-",  # EM DASH
    0x2015: "-",  # HORIZONTAL BAR
    0x2212: "-",  # MINUS SIGN
}


def _norm_text(value: object) -> str:
    text = str(value).translate(_DASH_VARIANTS)
    text = re.sub(r"[\s,]+", " ", text).strip().lower()
    return text.rstrip(" .;:")


def _values_match(field: str, expected: object, actual: object) -> Outcome | None:
    """Compare two stated values. Returns None when they do not match."""
    if isinstance(expected, CargoDimensions) or isinstance(actual, CargoDimensions):
        exp = expected if isinstance(expected, CargoDimensions) else None
        act = actual if isinstance(actual, CargoDimensions) else None
        if exp is None or act is None:
            return None
        same = all(
            abs(getattr(exp, axis) - getattr(act, axis)) <= _NUMERIC_TOLERANCE
            for axis in ("length", "width", "height")
        )
        return Outcome.CORRECT if same else None

    if isinstance(expected, bool) or isinstance(actual, bool):
        return Outcome.CORRECT if expected == actual else None

    if isinstance(expected, int | float) and isinstance(actual, int | float):
        close = abs(float(expected) - float(actual)) <= _NUMERIC_TOLERANCE
        return Outcome.CORRECT if close else None

    exp_text, act_text = _norm_text(expected), _norm_text(actual)
    if exp_text == act_text:
        return Outcome.CORRECT
    if (
        field in _FREE_TEXT
        and exp_text
        and act_text
        and (exp_text in act_text or act_text in exp_text)
    ):
        return Outcome.CORRECT_CONTAINED
    return None


def _coerce_expected(field: str, expected: ExpectedField) -> object:
    """Turn JSON-shaped expectations into the domain types they compare against."""
    if field == "dimensions_in" and isinstance(expected.value, dict):
        return CargoDimensions(**expected.value)
    return expected.value


def score_field(field: str, expected: ExpectedField, result: ExtractionResult) -> FieldScore:
    actual = getattr(result, field)
    expected_value = _coerce_expected(field, expected)

    expected_repr = (
        f"{expected.status.value}"
        if expected.status is not FieldStatus.STATED
        else f"{expected_value}"
    )
    actual_repr = (
        f"{actual.status.value}" if actual.status is not FieldStatus.STATED else f"{actual.value}"
    )

    def score(outcome: Outcome) -> FieldScore:
        return FieldScore(field, outcome, expected_repr, actual_repr)

    if expected.status is actual.status:
        if expected.status is not FieldStatus.STATED:
            return score(Outcome.CORRECT)
        matched = _values_match(field, expected_value, actual.value)
        return score(matched or Outcome.WRONG_VALUE)

    if expected.status is FieldStatus.NOT_STATED and actual.status is FieldStatus.STATED:
        return score(Outcome.HALLUCINATED)
    if expected.status is FieldStatus.STATED and actual.status is FieldStatus.NOT_STATED:
        return score(Outcome.MISSED)
    return score(Outcome.STATUS_MISMATCH)


def score_case(expected: ExpectedCase, result: ExtractionResult) -> CaseScore:
    return CaseScore(
        case=expected.case,
        fields=tuple(
            score_field(field, expected.expected(field), result) for field in CANONICAL_FIELDS
        ),
    )
