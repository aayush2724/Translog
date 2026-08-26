"""Clarification types."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from translog_quote.domain.validation import FieldName


class ClarificationMessage(BaseModel):
    """One message asking for every missing field at once.

    ``asked_for`` is the whole missing set. There is no single-field constructor,
    so "ask one thing at a time" is not expressible — which is the point. In the
    reference thread this back-and-forth took four separate emails over three days.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asked_for: tuple[FieldName, ...]
    body_text: str

    # Phase 2 adds `compose(missing, record) -> ClarificationMessage`:
    # deterministic templating, no model call.
