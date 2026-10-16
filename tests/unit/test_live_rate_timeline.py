"""The request timeline after a *live* (worker) rate search.

The in-process search (RateSearchStage) records rates_fetched / rate_selected
itself; the worker path did not, so after a live search the timeline stayed at
"Rate search: current" and "Rate selected: pending" even once a quotation was
sent. The session now records them when it applies a COMPLETED worker result:
search completion always, the selection only when an approval card was built,
each once per job (a restart re-applying the same job adds nothing), and
nothing at all for a FAILED job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.unit import test_live_browser_bridge as bridge
from tests.unit.test_live_browser_bridge import (
    JOB_ID,
    QueueSpy,
    _completed_status,
    _forbid_demo_provider,
    _install_queue,
    _only,
    _validated_session,
)
from tests.unit.test_web_live import APPROVER

from translog_quote.domain.rates import (
    FASTEST_ELIGIBLE,
    Rate,
    TransitTime,
    TransitUnit,
    filter_rates,
    select_rate,
)
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.jobs import JobState, JobStatus, RateSearchJobResult
from translog_quote.interface.web import live_serialize
from translog_quote.pipeline.audit import AuditEventType

if TYPE_CHECKING:
    import pytest

    from translog_quote.adapters.email import CollectingEmailSink
    from translog_quote.config import Settings
    from translog_quote.interface.web.live_session import LiveSession


#: The bridge suite's browser-mode settings and collecting sink, reused as-is.
browser_settings = bridge.browser_settings
sink = bridge.sink


def _events(session: LiveSession, kind: AuditEventType) -> list[object]:
    request_id = _only(session).request_id
    return [e for e in session.audit.events if e.request_id == request_id and e.event is kind]


def _timeline(session: LiveSession) -> dict[str, str]:
    request = _only(session)
    rows = live_serialize.timeline_json(request, session.audit.events)
    return {str(row["key"]): str(row["state"]) for row in rows}


def _status_with(rates: tuple[Rate, ...]) -> JobStatus:
    """A COMPLETED status built the way the worker builds one."""
    template = _completed_status(is_simulated=False, departure_label="16/09/2026")
    assert template.result is not None
    filtered = filter_rates(rates)
    result = RateSearchJobResult(
        adapter_id="webcargo-browser",
        is_simulated=False,
        returned=len(rates),
        query=template.result.query,
        filtered=filtered,
        selection=select_rate(filtered.eligible, FASTEST_ELIGIBLE),
        completeness=None,
    )
    return JobStatus(job_id=JOB_ID, state=JobState.COMPLETED, result=result)


def _to_search(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch, spy: QueueSpy
) -> LiveSession:
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(settings, sink)
    session.poll()  # enquiry
    session.approve_clarification(by=APPROVER)
    session.poll()  # reply -> VALIDATED -> enqueue
    return session


def _live_result() -> JobStatus:
    return _completed_status(is_simulated=False, departure_label="16/09/2026")


def test_a_completed_live_search_completes_the_rate_rows_and_then_the_whole_timeline(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _to_search(
        browser_settings, sink, monkeypatch, QueueSpy(fetch_returns=_live_result())
    )
    assert _timeline(session)["rate_search"] == "current"  # enqueued, not yet back

    session.poll()  # the worker's result is applied

    request = _only(session)
    assert request.state is RequestState.RATE_SELECTED
    fetched = _events(session, AuditEventType.RATES_FETCHED)
    selected = _events(session, AuditEventType.RATE_SELECTED)
    assert len(fetched) == 1 and len(selected) == 1
    assert fetched[0].detail == {  # type: ignore[attr-defined]
        "adapter": "webcargo-browser",
        "returned": 2,
        "job_id": JOB_ID,
    }
    detail = selected[0].detail  # type: ignore[attr-defined]
    assert detail["carrier"] == "TK"
    assert detail["rate_ref"] == request.packet.selection.rate.source_ref  # type: ignore[union-attr]
    assert detail["option"] == "fastest"
    assert detail["job_id"] == JOB_ID
    assert {"from": "validated", "to": "rate_selected", "job_id": JOB_ID} in [
        e.detail  # type: ignore[attr-defined]
        for e in _events(session, AuditEventType.STATE_CHANGED)
    ]
    timeline = _timeline(session)
    assert timeline["rate_search"] == "done"
    assert timeline["rate_selected"] == "done"
    assert timeline["approval_decided"] == "current"

    session.decide(request.request_id, choice="approve", by=APPROVER)

    assert set(_timeline(session).values()) == {"done"}


def test_more_polls_after_completion_record_nothing_twice(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _to_search(
        browser_settings, sink, monkeypatch, QueueSpy(fetch_returns=_live_result())
    )

    session.poll()
    session.poll()
    session.poll()

    for kind in (
        AuditEventType.RATES_FETCHED,
        AuditEventType.RATES_NORMALIZED,
        AuditEventType.RATES_FILTERED,
        AuditEventType.RATE_SELECTED,
    ):
        assert len(_events(session, kind)) == 1, kind


def test_the_same_job_applied_again_records_nothing_new(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart re-applies the same (cached) job; its events are already in the
    audit trail, so nothing is recorded twice."""
    session = _to_search(
        browser_settings, sink, monkeypatch, QueueSpy(fetch_returns=_live_result())
    )
    session.poll()
    request = _only(session)

    # What a restart leaves behind: the same request and job, the result to re-apply.
    request.rates = None
    request.packet = None
    request.state = RequestState.VALIDATED
    session.poll()

    assert _only(session).state is RequestState.RATE_SELECTED
    assert len(_events(session, AuditEventType.RATES_FETCHED)) == 1
    assert len(_events(session, AuditEventType.RATE_SELECTED)) == 1


def test_a_failed_live_search_records_no_search_or_selection(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy(
        fetch_returns=JobStatus(
            job_id=JOB_ID, state=JobState.FAILED, error="PermanentFailure: WebCargo refused"
        )
    )
    session = _to_search(browser_settings, sink, monkeypatch, spy)

    session.poll()

    assert _only(session).rate_failure == "WebCargo refused"
    assert _events(session, AuditEventType.RATES_FETCHED) == []
    assert _events(session, AuditEventType.RATE_SELECTED) == []
    timeline = _timeline(session)
    assert timeline["rate_search"] == "current"
    assert timeline["rate_selected"] == "pending"


def test_a_search_with_no_rows_completes_the_search_but_selects_nothing(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _to_search(
        browser_settings, sink, monkeypatch, QueueSpy(fetch_returns=_status_with(()))
    )

    session.poll()

    fetched = _events(session, AuditEventType.RATES_FETCHED)
    assert [e.detail["returned"] for e in fetched] == [0]  # type: ignore[attr-defined]
    assert _events(session, AuditEventType.RATE_SELECTED) == []
    assert {"from": "validated", "to": "no_eligible_rate", "job_id": JOB_ID} in [
        e.detail  # type: ignore[attr-defined]
        for e in _events(session, AuditEventType.STATE_CHANGED)
    ]
    timeline = _timeline(session)
    assert timeline["rate_search"] == "done"
    assert timeline.get("rate_selected") != "done"  # closed with a no-rates notice: no such row


def test_rows_that_are_all_excluded_select_nothing(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    unpriced = Rate(
        carrier_code="QR",
        carrier_name="Qatar Airways",
        product="GEN",
        total_amount=None,
        currency=None,
        transit=TransitTime(value=1200, unit=TransitUnit.MINUTES),
    )
    session = _to_search(
        browser_settings, sink, monkeypatch, QueueSpy(fetch_returns=_status_with((unpriced,)))
    )

    session.poll()

    assert _only(session).state is RequestState.MANUAL_REVIEW  # handed to a person
    filtered = _events(session, AuditEventType.RATES_FILTERED)
    assert filtered[0].detail["eligible"] == 0  # type: ignore[attr-defined]
    assert filtered[0].detail["excluded"] == [  # type: ignore[attr-defined]
        {"carrier": "QR", "reason": "incomplete_rate"}
    ]
    assert _events(session, AuditEventType.RATE_SELECTED) == []
