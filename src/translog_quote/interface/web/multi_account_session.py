"""MultiAccountSession — many LiveSessions presented as one to the dashboard.

Owns one :class:`LiveSession` per configured Gmail account and routes every
per-request action to the session that owns the mailbox the request arrived on,
so a reply always leaves from the account that received it. It presents the same
surface a single ``LiveSession`` does (``poll``, ``requests``, ``audit``,
``decide``, …) so the server and the serializer treat one mailbox and sixteen
identically.

Requests are identified by their namespaced id (``"<account_id>:<id>"``), which
is globally unique and carries its owner, so routing is a prefix split.

The WebCargo worker and the Redis/RQ queue stay shared and account-blind: each
``LiveSession`` enqueues to the one queue and polls its own request's job;
nothing here touches either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from translog_quote.config import resolve_gmail_accounts
from translog_quote.errors import PermanentFailure
from translog_quote.interface.jobs import WORKER_UNKNOWN
from translog_quote.interface.web.live_session import LiveSequenceError, LiveSession
from translog_quote.observability import get_logger

if TYPE_CHECKING:
    import datetime

    from translog_quote.config import Settings
    from translog_quote.interface.web.live_session import LiveRequest
    from translog_quote.pipeline.audit import AuditEvent

_log = get_logger("interface.web.multi_account_session")


class _MergedAudit:
    """The audit trails of every account, concatenated and time-ordered.

    Only ``events`` is read (the serializer's top-level audit block); per-request
    timelines are rendered from each owning session's own audit."""

    def __init__(self, sessions: dict[str, LiveSession]) -> None:
        self._sessions = sessions

    @property
    def events(self) -> list[AuditEvent]:
        merged: list[AuditEvent] = []
        for session in self._sessions.values():
            merged.extend(session.audit.events)
        merged.sort(key=lambda event: event.at)
        return merged


class MultiAccountSession:
    """Sixteen mailboxes, one session surface for the dashboard."""

    def __init__(self, settings: Settings, sessions: dict[str, LiveSession]) -> None:
        if not sessions:
            raise PermanentFailure(
                "No enabled Gmail accounts configured; a multi-account session needs at least one."
            )
        self._settings = settings
        self.sessions = sessions
        self.audit = _MergedAudit(sessions)
        self.approver_address = settings.gmail.approver_address or ""
        # Aggregated poll telemetry, recomputed on every poll.
        self.last_poll_new = 0
        self.skipped_internal = 0
        self.blocked_messages = 0
        self.outside_demonstration = 0
        self.last_poll_at: datetime.datetime | None = None
        self.last_poll_error: str | None = None
        self.worker_status: str = WORKER_UNKNOWN

    @classmethod
    def build(cls, settings: Settings) -> MultiAccountSession:
        """One :class:`LiveSession` per ENABLED configured account."""
        sessions = {
            account.account_id: LiveSession(settings, account=account)
            for account in resolve_gmail_accounts(settings)
            if account.enabled
        }
        return cls(settings, sessions)

    # ------------------------------------------------------------- routing --

    def _owner(self, request_id: str) -> LiveSession:
        """The session that owns a request, by its ``<account_id>:`` prefix."""
        prefix = request_id.split(":", 1)[0] if ":" in request_id else None
        if prefix is not None and prefix in self.sessions:
            return self.sessions[prefix]
        if len(self.sessions) == 1:
            return next(iter(self.sessions.values()))
        for session in self.sessions.values():
            if request_id in session.requests:
                return session
        raise LiveSequenceError(f"There is no request {request_id}.")

    def account_of(self, request_id: str) -> str:
        """The account id that owns a request — its namespaced prefix."""
        for account_id, session in self.sessions.items():
            if request_id in session.requests:
                return account_id
        if ":" in request_id:
            return request_id.split(":", 1)[0]
        return next(iter(self.sessions))

    # ----------------------------------------------------- unified surface --

    @property
    def requests(self) -> dict[str, LiveRequest]:
        merged: dict[str, LiveRequest] = {}
        for session in self.sessions.values():
            merged.update(session.requests)
        return merged

    @property
    def operations_mode(self) -> bool:
        return next(iter(self.sessions.values())).operations_mode

    def in_demonstration(self, request_id: str) -> bool:
        return self._owner(request_id).in_demonstration(request_id)

    def goods_type_hold_options(self) -> tuple[list[str], bool]:
        # Same configuration for every account, so any session answers.
        return next(iter(self.sessions.values())).goods_type_hold_options()

    def start_demonstration(self) -> None:
        for session in self.sessions.values():
            session.start_demonstration()

    def resume_operations(self) -> None:
        for session in self.sessions.values():
            session.resume_operations()

    def poll(self) -> None:
        """Poll every account, each isolated: one account's failed poll records
        its own error and never stops the others. Telemetry is aggregated."""
        new = internal = blocked = outside = 0
        last_at: datetime.datetime | None = None
        first_error: str | None = None
        worker = WORKER_UNKNOWN
        for account_id, session in self.sessions.items():
            try:
                session.poll()
            except Exception as exc:  # noqa: BLE001 - one account must not stop the rest
                session.last_poll_error = type(exc).__name__
                _log.warning("Mailbox poll failed for account %s: %s", account_id, exc)
            new += session.last_poll_new
            internal += session.skipped_internal
            blocked += session.blocked_messages
            outside += session.outside_demonstration
            if session.last_poll_at is not None and (
                last_at is None or session.last_poll_at > last_at
            ):
                last_at = session.last_poll_at
            if first_error is None and session.last_poll_error is not None:
                first_error = session.last_poll_error
            worker = session.worker_status  # shared worker; every session agrees
        self.last_poll_new = new
        self.skipped_internal = internal
        self.blocked_messages = blocked
        self.outside_demonstration = outside
        self.last_poll_at = last_at
        self.last_poll_error = first_error
        self.worker_status = worker

    def approve_clarification(self, *, by: str, request_id: str | None = None) -> None:
        if request_id is None:
            raise LiveSequenceError(
                "A clarification approval must name a request in multi-account mode."
            )
        self._owner(request_id).approve_clarification(by=by, request_id=request_id)

    def decide(self, request_id: str, *, choice: str, by: str, reason: str = "") -> LiveRequest:
        return self._owner(request_id).decide(request_id, choice=choice, by=by, reason=reason)

    def decide_goods_type(self, request_id: str, *, goods_type: str, by: str) -> None:
        self._owner(request_id).decide_goods_type(request_id, goods_type=goods_type, by=by)

    def close(self) -> None:
        """Release every account session's pooled connections."""
        for session in self.sessions.values():
            session.close()
