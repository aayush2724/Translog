"""Running client cases through the existing extraction pipeline.

    client PDF -> deterministic text -> thread split -> CLIENT messages only
        -> ExtractionPort (Phase 5, live Qwen) -> ExtractionResult
        -> compare against reviewed ground truth

The model is reached only through `bootstrap.build_extractor`, which returns the
existing `ExtractionPort`. There is no second Qwen implementation here and no
direct call to OpenRouter — this module could not bypass the contract if it
wanted to.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from translog_quote import bootstrap
from translog_quote.errors import TranslogError
from translog_quote.evaluation.pdf_text import pdf_to_clean_text
from translog_quote.evaluation.scoring import CaseScore, score_case
from translog_quote.evaluation.thread import Message, client_request_text, split_thread

if TYPE_CHECKING:
    from translog_quote.config import Settings
    from translog_quote.domain.extraction import ExtractionResult
    from translog_quote.evaluation.ground_truth import ExpectedCase
    from translog_quote.ports import ExtractionPort

DEFAULT_CORPUS = Path("evaluation/client_cases")


class CaseLoadError(RuntimeError):
    """The PDF could not be turned into client text. Never a model problem."""


@dataclass(frozen=True, slots=True)
class LoadedCase:
    case: int
    messages: tuple[Message, ...]
    client_text: str

    @property
    def client_message_count(self) -> int:
        return sum(1 for m in self.messages if m.is_client)


def load_case(case: int, corpus: Path = DEFAULT_CORPUS) -> LoadedCase:
    path = corpus / f"{case}.pdf"
    if not path.exists():
        raise CaseLoadError(f"case {case}: no such PDF at {path}")

    try:
        text = pdf_to_clean_text(path)
    except Exception as exc:  # pypdf raises a wide range on damaged files
        raise CaseLoadError(f"case {case}: PDF text extraction failed ({exc})") from exc

    messages = split_thread(text)
    if not messages:
        raise CaseLoadError(f"case {case}: no message boundaries found in thread")

    client_text = client_request_text(messages)
    if not client_text.strip():
        raise CaseLoadError(
            f"case {case}: thread contains no client-authored message "
            "(Translog-only correspondence)"
        )
    return LoadedCase(case=case, messages=messages, client_text=client_text)


@dataclass(frozen=True, slots=True)
class CaseRun:
    """One case, start to finish."""

    case: int
    loaded: LoadedCase | None
    result: ExtractionResult | None
    score: CaseScore | None
    error: str | None = None
    error_kind: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def run_case(
    case: int,
    expected: ExpectedCase,
    extractor: ExtractionPort,
    corpus: Path = DEFAULT_CORPUS,
) -> CaseRun:
    """Load, extract, score. Failures are returned, not raised — one bad case
    must not abandon a 46-case run."""
    try:
        loaded = load_case(case, corpus)
    except CaseLoadError as exc:
        return CaseRun(case, None, None, None, str(exc), "pdf_parsing")

    try:
        result = extractor.extract_shipment(loaded.client_text)
    except TranslogError as exc:
        kind = "schema" if type(exc).__name__ == "ContractViolation" else "provider"
        return CaseRun(case, loaded, None, None, f"{type(exc).__name__}: {exc}", kind)

    return CaseRun(case, loaded, result, score_case(expected, result))


def build_extractor(settings: Settings) -> ExtractionPort:
    """The live extraction port. Requires a configured API key."""
    return bootstrap.build_extractor(settings)
