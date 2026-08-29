"""An `AuditSink` that keeps the trail, and keeps it across restarts.

The pipeline's audit trail is the evidence the workflow ran as designed, and in
the live demonstration it is also the activity timeline a presenter narrates
from. Held only in memory it would be empty every time the server restarts,
leaving a request that genuinely progressed looking as though nothing had
happened to it.

It lives in `interface/` rather than `adapters/` for the same reason
`_CollectingAudit` does: `AuditSink` is declared in `pipeline`, which adapters
may not import, and collecting events for display is a presentation concern.
The file is a companion to the durable store's directory, not part of it —
`StorePort` has no audit method and gains none here.

One JSON object per line, appended. Append-only matches what an audit trail
is: entries are never edited or removed, and a partially written last line is
skipped on load rather than failing the whole history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.observability import get_logger
from translog_quote.pipeline.audit import AuditEvent

if TYPE_CHECKING:
    from pathlib import Path

_log = get_logger("interface.web.audit_log")

AUDIT_FILE = "audit.jsonl"


class JsonFileAuditLog:
    """Collects audit events in memory and appends them to a file.

    Both, deliberately: the in-memory list is what the snapshot renders from,
    and the file is what makes it survive the process. A write that fails is
    logged and does not raise — losing a line of the display trail must never
    abort a workflow step that has already happened.
    """

    def __init__(self, directory: Path) -> None:
        self._path = directory / AUDIT_FILE
        self.events: list[AuditEvent] = list(self._load())

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> list[AuditEvent]:
        if not self._path.exists():
            return []
        events: list[AuditEvent] = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            text = line.strip()
            if not text:
                continue
            try:
                events.append(AuditEvent.model_validate_json(text))
            except ValueError:
                # A torn final line from an interrupted write, or an entry from
                # an older shape. Skipped rather than fatal: a demonstration
                # must not refuse to start because one line of its display
                # history is unreadable.
                _log.warning("Skipping unreadable audit line %d in %s", number, self._path)
        return events

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(event.model_dump_json() + "\n")
        except OSError as exc:  # pragma: no cover - filesystem failure
            _log.warning("Could not append to the audit log at %s: %s", self._path, exc)

    def for_request(self, request_id: str) -> list[AuditEvent]:
        """Every event recorded against one request, in order."""
        return [event for event in self.events if event.request_id == request_id]
