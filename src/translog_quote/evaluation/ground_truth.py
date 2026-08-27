"""Manually reviewed expectations for the client cases.

Every entry here was written by reading the client's own messages in the PDF —
never by running the model and recording what it said. That direction matters:
ground truth derived from output measures nothing.

The expectations live in a JSON file beside the corpus rather than in this
module, because they quote real client correspondence and must stay untracked.
This module is the schema and the loader, and is safe to version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.extraction import FieldStatus

DEFAULT_GROUND_TRUTH = Path("evaluation/ground_truth.json")

CANONICAL_FIELDS: tuple[str, ...] = (
    "origin",
    "destination",
    "weight_kg",
    "dimensions_in",
    "commodity",
    "cargo_type",
    "is_chemical",
    "msds_attached",
    "pcs",
    "delivery_type",
    "delivery_address",
)


class ExpectedField(BaseModel):
    """What the client actually stated about one field, and where."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: FieldStatus
    value: Any = None
    evidence: str = ""
    source_message: int | None = None
    """Which client message (0 = first) this came from. Null when not stated."""


class ExpectedCase(BaseModel):
    """One client case's reviewed expectations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case: int
    note: str = ""
    conflict_fields: tuple[str, ...] = ()
    """Fields where client messages contradict each other across the thread."""

    fields: dict[str, ExpectedField]

    def expected(self, field: str) -> ExpectedField:
        return self.fields.get(field, ExpectedField(status=FieldStatus.NOT_STATED))


def load_ground_truth(path: Path | None = None) -> dict[int, ExpectedCase]:
    """Load reviewed expectations. Missing file is an error, not an empty set."""
    source = path or DEFAULT_GROUND_TRUTH
    if not source.exists():
        raise FileNotFoundError(
            f"No ground truth at {source}. Expectations are reviewed by hand; "
            "the evaluation cannot score anything without them."
        )
    raw = json.loads(source.read_text(encoding="utf-8"))
    cases = [ExpectedCase.model_validate(entry) for entry in raw["cases"]]
    return {case.case: case for case in cases}
