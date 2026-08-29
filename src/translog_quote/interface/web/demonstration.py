"""Which real messages belong to the demonstration being given right now.

A mailbox used for testing accumulates history, and during a presentation that
history competes with the one enquiry the room is meant to be following. The
answer here is neither to delete it nor to name it: a demonstration is simply
**everything that arrived after the presenter pressed Start**.

That definition earns its place three times over:

- Nothing is hardcoded. No subject, sender or shipment is named anywhere, so
  the scope holds for whatever enquiry is sent on the day.
- Nothing is hidden. Earlier mail is still in the mailbox, still reported, and
  still counted on screen — it is simply not the thing in focus.
- An old reply cannot attach itself to a new demonstration, because it predates
  the cutoff. That is structural rather than a rule someone has to remember.

Membership is recorded as well as inferred. The cutoff decides what to *ingest*;
the recorded request ids decide what is *in focus*, so a restarted server knows
exactly which requests belong to the demonstration rather than re-deriving it
from timestamps it may no longer hold.

Starting a demonstration writes one small file. It deletes nothing — not a
Gmail message, not a persisted request, not an audit entry.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from translog_quote.errors import ContractViolation
from translog_quote.observability import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_log = get_logger("interface.web.demonstration")

DEMONSTRATION_FILE = "demonstration.json"


class Demonstration(BaseModel):
    """The demonstration in progress, if there is one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    started_at: datetime | None = None
    """When the presenter pressed Start. Messages older than this are history.

    ``None`` means no demonstration has been started, and nothing is filtered —
    which is exactly how the interface behaved before this existed.
    """

    request_ids: tuple[str, ...] = ()
    """Requests first seen during this demonstration, in the order they arrived."""

    @property
    def is_active(self) -> bool:
        return self.started_at is not None

    def covers(self, received_at: datetime) -> bool:
        """Whether a message that arrived then belongs to this demonstration.

        Inclusive of the start instant. The presenter presses Start and *then*
        sends the enquiry, so the boundary case that matters is a message
        arriving in the same second, not one arriving before.
        """
        return self.started_at is None or received_at >= self.started_at

    def focuses(self, request_id: str) -> bool:
        """Whether this request is one the demonstration is following."""
        return not self.is_active or request_id in self.request_ids

    def including(self, request_id: str) -> Demonstration:
        """This demonstration, now also following that request."""
        if not self.is_active or request_id in self.request_ids:
            return self
        return self.model_copy(update={"request_ids": (*self.request_ids, request_id)})


class DemonstrationFile:
    """Where the demonstration is remembered between runs.

    One small JSON file beside the durable store. It is deliberately not part
    of `StorePort`: which messages a *presenter* is currently showing is a
    property of the presentation, not of the workflow, and the workflow is
    entirely unaware that any of this exists.
    """

    def __init__(self, directory: Path) -> None:
        self._path = directory / DEMONSTRATION_FILE
        self.current = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> Demonstration:
        if not self._path.exists():
            return Demonstration()
        try:
            return Demonstration.model_validate_json(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # An unreadable file must not stop a demonstration starting. The
            # safe reading is "no demonstration is in progress", which shows
            # everything rather than hiding something.
            _log.warning("Could not read %s (%s); treating as no demonstration", self._path, exc)
            return Demonstration()

    def save(self, demonstration: Demonstration) -> None:
        self.current = demonstration
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(demonstration.model_dump_json(indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - filesystem failure
            raise ContractViolation(f"Could not record the demonstration at {self._path}") from exc

    def start(self, at: datetime) -> Demonstration:
        """Begin a new demonstration. Deletes nothing.

        Earlier requests stay in the store and stay on screen; they simply stop
        being what the interface leads with.
        """
        started = Demonstration(started_at=at)
        self.save(started)
        _log.info("New demonstration started at %s", at.isoformat())
        return started

    def include(self, request_id: str) -> Demonstration:
        updated = self.current.including(request_id)
        if updated is not self.current:
            self.save(updated)
        return updated
