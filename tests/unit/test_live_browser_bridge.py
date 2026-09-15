"""The LiveSession browser-mode rate-search bridge (Option F).

In browser mode the real WebCargo search runs only inside the browser worker, so
the session enqueues a job on the shared queue and polls it across successive
`poll()` calls rather than searching in-process. These tests pin that bridge:

- the first poll that finds a validated request enqueues exactly once and records
  the job id; the demo provider is never built;
- QUEUED / PROCESSING keeps the request pending without a second enqueue;
- COMPLETED hydrates the request from the worker's own filtered/selected result —
  not re-run here — and the serialised approval carries the provider's departure
  date and no simulated banner when the result is real;
- FAILED is reported and stays failed, never falling back to simulated data;
- a Redis outage is transient — reported, and retried on the next poll;
- a validated record with no shipment date fails clearly and enqueues nothing.

The queue is stubbed by monkeypatching the module-level names the session
imported (`enqueue_rate_search`, `fetch_job_status`); demo/mock behaviour is
covered by its own regression at the foot of the file.
"""

from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    ENQUIRY_EXTRACTION,
    REPLY,
    REPLY_EXTRACTION,
    ScriptedExtractor,
)
from tests.unit.test_web_live import APPROVER, GrowingSource

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.config import Settings, WebCargoMode
from translog_quote.domain.extraction import ExtractedValue
from translog_quote.domain.rates import (
    FASTEST_ELIGIBLE,
    Rate,
    RateQuery,
    TransitTime,
    TransitUnit,
    filter_rates,
    select_rate,
)
from translog_quote.domain.rates import LocationRef as RateLocationRef
from translog_quote.domain.shipment import CargoDimensions
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.jobs import (
    JobState,
    JobStatus,
    RateSearchJobResult,
)
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web import live_session as live_session_module
from translog_quote.interface.web.live_session import LiveSession

if TYPE_CHECKING:
    from translog_quote.interface.jobs import RateSearchJobRequest
    from translog_quote.interface.web.live_session import LiveRequest

FAKE_KEY = "test-not-a-real-credential"
APPROVER_MAILBOX = "approvals@translog.example"
JOB_ID = "rate-search-deadbeef"


@pytest.fixture
def sink() -> CollectingEmailSink:
    return CollectingEmailSink()


def _settings(mode: WebCargoMode) -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(
        update={
            "openrouter": base.openrouter.model_copy(update={"api_key": FAKE_KEY}),
            "demo": base.demo.model_copy(update={"state_dir": Path(tempfile.mkdtemp())}),
            "gmail": base.gmail.model_copy(
                update={
                    "test_address": "translog@example.com",
                    "sender_address": "translog@example.com",
                    "approver_address": APPROVER_MAILBOX,
                    "send_enabled": True,
                }
            ),
            "webcargo": base.webcargo.model_copy(update={"mode": mode}),
        }
    )


@pytest.fixture
def browser_settings() -> Settings:
    return _settings(WebCargoMode.BROWSER)


class QueueSpy:
    """Records enqueue calls and answers `fetch_job_status` from a script.

    Stands in for the Redis-backed queue by replacing the two module-level
    names the session imported. `enqueue_returns` and `fetch_returns` may be an
    exception to raise, exercising the outage paths.
    """

    def __init__(
        self,
        *,
        enqueue_returns: tuple[str, bool] | Exception = (JOB_ID, True),
        fetch_returns: JobStatus | None | Exception = None,
    ) -> None:
        self._enqueue_returns = enqueue_returns
        self._fetch_returns = fetch_returns
        self.enqueued: list[RateSearchJobRequest] = []
        self.fetched: list[str] = []

    def enqueue(self, job: RateSearchJobRequest, _settings: Settings) -> tuple[str, bool]:
        self.enqueued.append(job)
        if isinstance(self._enqueue_returns, Exception):
            raise self._enqueue_returns
        return self._enqueue_returns

    def fetch(self, job_id: str, _settings: Settings) -> JobStatus | None:
        self.fetched.append(job_id)
        if isinstance(self._fetch_returns, Exception):
            raise self._fetch_returns
        return self._fetch_returns


def _install_queue(monkeypatch: pytest.MonkeyPatch, spy: QueueSpy) -> None:
    monkeypatch.setattr(live_session_module, "enqueue_rate_search", spy.enqueue)
    monkeypatch.setattr(live_session_module, "fetch_job_status", spy.fetch)


def _forbid_demo_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser mode must never build the in-process simulated provider."""

    def explode() -> object:
        raise AssertionError("browser mode must not build the demo rate provider")

    monkeypatch.setattr(live_session_module.bootstrap, "build_demo_rate_provider", explode)


#: The enquiry, but stating places the browser-mode resolver can resolve without
#: guessing, so the real `CanonicalLocationResolver` stays under test here and
#: the bridge reaches the enqueue rather than a location clarification. The
#: reply (`REPLY_EXTRACTION`) fills the remaining fields and never touches the
#: places. Parenthesised codes are what the resolver accepts as explicit.
_RESOLVABLE_ENQUIRY = ENQUIRY_EXTRACTION.model_copy(
    update={
        "origin": ExtractedValue[str].stated("Delhi (DEL)"),
        "destination": ExtractedValue[str].stated("Singapore (SIN)"),
    }
)


def _validated_session(settings: Settings, sink: CollectingEmailSink) -> LiveSession:
    """A session whose one request has reached VALIDATED after a merged reply.

    The enquiry validates to NEEDS_INFO; approving the clarification and polling
    the reply merges it to a complete, validated record — the point at which the
    rate-search bridge takes over. The places resolve, so the real browser-mode
    resolver runs and the bridge enqueues (an unresolvable place is a location
    clarification, exercised in its own tests, not here).
    """
    return LiveSession(
        settings,
        source=GrowingSource((ENQUIRY,), (ENQUIRY, REPLY)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(_RESOLVABLE_ENQUIRY, REPLY_EXTRACTION),
    )


def _only(session: LiveSession) -> LiveRequest:
    return next(iter(session.requests.values()))


def _completed_status(
    *, is_simulated: bool, departure_label: str, completeness: str | None = None
) -> JobStatus:
    """A COMPLETED status carrying a real filtered/selected result.

    Built from the domain filter and selection exactly as the worker builds it,
    so the session's job is only to carry it through — never to re-select.
    """
    rates = (
        Rate(
            carrier_code="TK",
            carrier_name="Turkish Cargo",
            product="GEN",
            total_amount=Decimal("16900.00"),
            currency="INR",
            transit=TransitTime(value=1, unit=TransitUnit.DAYS),
            departure_date_label=departure_label,
        ),
        Rate(
            carrier_code="EK",
            carrier_name="Emirates",
            product="GEN",
            total_amount=Decimal("20762.00"),
            currency="INR",
            transit=TransitTime(value=2, unit=TransitUnit.DAYS),
            departure_date_label=departure_label,
        ),
    )
    filtered = filter_rates(rates)
    selection = select_rate(filtered.eligible, FASTEST_ELIGIBLE)
    query = RateQuery(
        origin=RateLocationRef(stated="Ahmedabad"),
        destination=RateLocationRef(stated="Bahrain"),
        weight_kg=500.0,
        dimensions_in=CargoDimensions(length=34, width=24, height=6),
        pieces=1,
        date=date(2026, 9, 15),
        commodity="Engineering components",
    )
    result = RateSearchJobResult(
        adapter_id="webcargo-browser",
        is_simulated=is_simulated,
        returned=len(rates),
        query=query,
        filtered=filtered,
        selection=selection,
        completeness=completeness,
    )
    return JobStatus(job_id=JOB_ID, state=JobState.COMPLETED, result=result)


# --- enqueue once, then pending -------------------------------------------------


def test_the_first_poll_enqueues_exactly_once_and_records_the_job(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()  # enquiry
    session.approve_clarification(by=APPROVER)
    session.poll()  # reply -> VALIDATED -> enqueue

    request = _only(session)
    assert request.state is RequestState.VALIDATED
    assert request.rate_job_id == JOB_ID
    assert request.rates is None
    assert request.rate_failure is None
    assert request.rate_search_pending is True
    assert len(spy.enqueued) == 1
    # The date searched is the client's own stated shipment date (AMB-8).
    assert spy.enqueued[0].search_date == date(2026, 9, 15)


def test_a_queued_or_processing_status_does_not_re_enqueue(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy(fetch_returns=JobStatus(job_id=JOB_ID, state=JobState.PROCESSING))
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()  # enqueues
    session.poll()  # polls, still PROCESSING

    request = _only(session)
    assert len(spy.enqueued) == 1  # never a second submission
    assert spy.fetched == [JOB_ID]
    assert request.rate_search_pending is True
    assert request.rates is None
    assert request.rate_failure is None


def test_the_serialised_summary_and_detail_report_the_pending_search(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued search in flight is reported, so the dashboard shows
    "Searching WebCargo…" rather than an empty rate section that reads as idle."""
    spy = QueueSpy(fetch_returns=JobStatus(job_id=JOB_ID, state=JobState.QUEUED))
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()  # enqueues

    request_id = _only(session).request_id
    snap = live_serialize.snapshot(session, selected=request_id)
    summary = next(r for r in snap["requests"] if r["request_id"] == request_id)  # type: ignore[index]

    assert summary["rate_search_pending"] is True
    assert snap["selected"]["rate_search_pending"] is True  # type: ignore[index]
    # No rate section yet, and no failure — the request is genuinely mid-search.
    assert snap["selected"]["rates"] is None  # type: ignore[index]
    assert snap["selected"]["rate_failure"] is None  # type: ignore[index]


# --- completion hydrates from the worker's result -------------------------------


def test_a_completed_job_hydrates_the_request_without_re_selecting(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy(
        fetch_returns=_completed_status(is_simulated=False, departure_label="16/09/2026")
    )
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()  # enqueues
    session.poll()  # completes

    request = _only(session)
    assert request.rates is not None
    assert request.state is RequestState.RATE_SELECTED
    assert request.packet is not None
    assert request.rate_failure is None
    assert request.rate_search_pending is False
    assert request.rates.selection is not None
    assert request.rates.selection.rate.carrier_code == "TK"


def test_a_completed_real_result_shows_departure_date_and_no_banner(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy(
        fetch_returns=_completed_status(is_simulated=False, departure_label="16/09/2026")
    )
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()
    session.poll()

    snap = live_serialize.snapshot(session, selected=_only(session).request_id)
    approval = snap["selected"]["approval"]  # type: ignore[index]
    rates = snap["selected"]["rates"]  # type: ignore[index]

    assert rates["simulated"] is False
    assert rates["banner"] is None
    assert approval["banner"] is None
    assert approval["departure_date"] == "16/09/2026"


def test_a_real_result_surfaces_the_provider_candidate_set_on_the_approval_card(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H5: the approver is told the selection is the fastest of the set WebCargo
    returned — its verbatim note is shown and nothing implies a global fastest."""
    note = "Showing the 18 lowest rates. Other surcharges may apply."
    spy = QueueSpy(
        fetch_returns=_completed_status(
            is_simulated=False, departure_label="16/09/2026", completeness=note
        )
    )
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()
    session.poll()

    approval = live_serialize.snapshot(session, selected=_only(session).request_id)[  # type: ignore[index]
        "selected"
    ]["approval"]

    assert approval["completeness"] == note  # provider's own words, verbatim
    scope = approval["candidate_scope"]
    assert "WebCargo returned" in scope
    assert "not necessarily the fastest that exists" in scope
    assert "fastest available" not in scope.lower()  # never a global claim


def test_a_real_result_without_a_provider_total_says_completeness_is_unconfirmed(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If WebCargo stated no total, the card says so rather than implying one."""
    spy = QueueSpy(
        fetch_returns=_completed_status(
            is_simulated=False, departure_label="16/09/2026", completeness=None
        )
    )
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()
    session.poll()

    approval = live_serialize.snapshot(session, selected=_only(session).request_id)[  # type: ignore[index]
        "selected"
    ]["approval"]

    assert approval["completeness"] is None
    assert "completeness is unconfirmed" in approval["candidate_scope"]


# --- failure: reported, and never papered over with simulated data --------------


def test_a_failed_job_is_reported_and_never_falls_back_to_simulated(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy(
        fetch_returns=JobStatus(job_id=JOB_ID, state=JobState.FAILED, error="WebCargo login failed")
    )
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()  # enqueues
    session.poll()  # sees FAILED

    request = _only(session)
    assert request.rate_failure == "WebCargo login failed"
    assert request.rates is None
    assert request.packet is None
    assert request.rate_search_pending is False
    # The job id is retained, so a broken search is not auto re-enqueued.
    assert request.rate_job_id == JOB_ID


# --- a Redis outage is transient ------------------------------------------------


def test_a_redis_error_on_enqueue_is_transient_and_clears_the_job_id(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    spy = QueueSpy(enqueue_returns=RedisError("connection refused"))
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()  # enqueue raises

    request = _only(session)
    assert request.rate_failure is not None
    assert "queue is unavailable" in request.rate_failure
    assert request.rates is None
    # No job id is left behind, so the next poll retries the enqueue.
    assert request.rate_job_id is None


def test_a_redis_error_on_fetch_is_reported_without_losing_the_job(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import RedisError

    spy = QueueSpy(fetch_returns=RedisError("connection refused"))
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()  # enqueues
    session.poll()  # fetch raises

    request = _only(session)
    assert request.rate_failure is not None
    assert "queue is unavailable" in request.rate_failure
    # The job is still ours to poll again once the queue is back.
    assert request.rate_job_id == JOB_ID


# --- a missing shipment date fails clearly and enqueues nothing -----------------


def test_a_validated_record_without_a_ship_date_fails_and_never_enqueues(
    browser_settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive: VR-12 makes ship_date required to validate, so this should not
    happen — but if it did, the bridge fails loudly rather than inventing one."""
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _validated_session(browser_settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()  # reply -> VALIDATED (with a ship_date, then removed below)

    request = _only(session)
    # Simulate a validated record that lost its date, and reset the search state
    # so the next poll re-enters the bridge from the top.
    request.record = request.record.model_copy(update={"ship_date": None})
    request.rate_job_id = None
    request.rates = None
    request.rate_failure = None
    spy.enqueued.clear()  # ignore the enqueue from before the date was removed

    session.poll()

    assert spy.enqueued == []  # the guard fires before any enqueue
    assert request.rate_job_id is None
    assert request.rate_failure is not None
    assert "shipment date" in request.rate_failure.lower()


# --- demo / mock regression: synchronous, simulated, never enqueues -------------


@pytest.mark.parametrize("mode", [WebCargoMode.DEMO, WebCargoMode.MOCK])
def test_demo_and_mock_stay_synchronous_and_simulated_and_never_enqueue(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch, mode: WebCargoMode
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    settings = _settings(mode)
    session = _validated_session(settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()  # reply -> VALIDATED -> synchronous search, in-process

    request = _only(session)
    assert spy.enqueued == []  # the queue is never touched in demo/mock
    assert request.rate_job_id is None
    assert request.rates is not None
    assert request.state is RequestState.RATE_SELECTED

    snap = live_serialize.snapshot(session, selected=request.request_id)
    rates = snap["selected"]["rates"]  # type: ignore[index]
    assert rates["simulated"] is True
    assert rates["banner"] == live_serialize.SIMULATED_BANNER


def test_the_synchronous_search_runs_on_the_clients_own_ship_date(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AMB-8, on the demo path too: the searched date is the record's ship_date,
    not a session-invented constant."""
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    settings = _settings(WebCargoMode.MOCK)
    session = _validated_session(settings, sink)

    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()

    request = _only(session)
    assert request.rates is not None
    assert request.rates.query.date == request.record.ship_date == date(2026, 9, 15)


def test_the_job_request_carries_the_records_piece_count() -> None:
    """`_job_request_from_record` threads `ShipmentRecord.pcs` into the queued
    request (and thus the WebCargo Pieces field), so the search reflects the
    client's real shipment rather than a single box."""
    from translog_quote.domain.shipment import RequestSource, ShipmentRecord
    from translog_quote.interface.web.live_session import LiveRequest

    record = ShipmentRecord(
        request_id="R-PCS",
        source=RequestSource.EMAIL,
        origin="Delhi, India",
        destination="Singapore",
        weight_kg=400.0,
        dimensions_in=CargoDimensions(length=35, width=28, height=22),
        pcs=8,
        commodity="General Cargo",
        ship_date=date(2026, 9, 30),
    )
    live_request = LiveRequest(
        request_id="R-PCS",
        client_address="client@example.com",
        state=RequestState.VALIDATED,
        record=record,
        validation=None,  # type: ignore[arg-type]  # unread by _job_request_from_record
    )

    job = LiveSession._job_request_from_record(live_request)

    assert job.pieces == 8
    assert job.origin == "Delhi, India"  # stated wording preserved, unresolved here
    assert job.to_query().pieces == 8
