"""The goods-type operator hold: when the rule cannot decide General Cargo, the
request holds (state stays VALIDATED) for an operator to pick an exact WebCargo
label; the pick is validated, audited, persisted, and survives a restart.

Reuses the browser-bridge harness. A hold is forced by leaving the
special-handling list EMPTY (the rule is then OFF), while the catalog is
configured so an operator can pick.
"""

from __future__ import annotations

import pytest
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    REPLY,
    REPLY_EXTRACTION,
    ScriptedExtractor,
)
from tests.unit.test_live_browser_bridge import (
    _RESOLVABLE_ENQUIRY,
    APPROVER,
    QueueSpy,
    _forbid_demo_provider,
    _install_queue,
    _only,
    _settings,
)
from tests.unit.test_web_live import GrowingSource

from translog_quote import bootstrap
from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.config import Settings, WebCargoMode
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_session import (
    CollectingAudit,
    LiveSequenceError,
    LiveSession,
)
from translog_quote.pipeline.audit import AuditEventType
from translog_quote.ports import StorePort

_CATALOG = ("0000 - General Cargo", "1234 - Machinery", "5678 - Perishables")


def _hold_settings(
    *, catalog: tuple[str, ...] = _CATALOG, general_cargo_label: str | None = None
) -> Settings:
    """Browser mode with the General Cargo rule OFF (empty special-handling) so
    every request holds, and a configured catalog to pick from."""
    base = _settings(WebCargoMode.BROWSER)
    update: dict[str, object] = {"special_handling": (), "catalog": catalog}
    if general_cargo_label is not None:
        update["general_cargo_label"] = general_cargo_label
    return base.model_copy(update={"goods_type": base.goods_type.model_copy(update=update)})


def _build_session(
    settings: Settings,
    sink: CollectingEmailSink,
    *,
    durable: StorePort | None = None,
    audit: CollectingAudit | None = None,
) -> LiveSession:
    return LiveSession(
        settings,
        source=GrowingSource((ENQUIRY,), (ENQUIRY, REPLY)),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(_RESOLVABLE_ENQUIRY, REPLY_EXTRACTION),
        durable=durable,
        audit=audit,
    )


def _drive_to_validated(session: LiveSession) -> None:
    session.poll()  # enquiry -> NEEDS_INFO (a clarification is drafted)
    session.approve_clarification(by=APPROVER)
    session.poll()  # reply merges -> VALIDATED -> goods-type decision


@pytest.fixture
def sink() -> CollectingEmailSink:
    return CollectingEmailSink()


def _goods_type_audits(audit: CollectingAudit) -> list[dict[str, object]]:
    return [
        dict(e.detail)
        for e in audit.events
        if e.event is AuditEventType.GOODS_TYPE_DECIDED
    ]


# --- the hold --------------------------------------------------------------------


def test_a_request_the_rule_cannot_decide_holds_and_does_not_enqueue(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _build_session(_hold_settings(), sink)

    _drive_to_validated(session)

    request = _only(session)
    assert request.state is RequestState.VALIDATED
    assert request.awaiting_goods_type is True
    assert request.rate_job_id is None
    assert spy.enqueued == []  # nothing queued until an operator picks


def test_the_hold_shows_the_cargo_facts_and_the_catalog(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_queue(monkeypatch, QueueSpy())
    _forbid_demo_provider(monkeypatch)
    session = _build_session(_hold_settings(), sink)
    _drive_to_validated(session)

    hold = live_serialize.request_detail(session, _only(session))["goods_type_hold"]

    assert hold is not None
    assert hold["catalog_configured"] is True
    assert hold["catalog"] == list(_CATALOG)
    # the client's own cargo facts, to judge by:
    assert "commodity" in hold and "cargo_type" in hold and "is_chemical" in hold
    # ...including the MSDS status, so an operator sees a chemical-with-no-MSDS:
    assert "msds" in hold


def test_msds_note_puts_a_client_stated_no_into_words() -> None:
    """The operator sees WHY there is no MSDS on the hold card: an explicit
    client "no" (however the model shaped it) reads as 'not available (client
    stated)', an attached one as 'attached', and an unknown as nothing."""
    assert live_serialize._msds_note(True) == "attached"
    assert live_serialize._msds_note(False) == "not available (client stated)"
    assert live_serialize._msds_note(None) is None


def test_default_config_offers_general_cargo_and_is_configured(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the default (valid) general-cargo label and an empty catalog, the
    effective catalog still contains the General Cargo label, so the picker is
    'configured' — never an empty picker, never 'not configured'."""
    _install_queue(monkeypatch, QueueSpy())
    _forbid_demo_provider(monkeypatch)
    session = _build_session(_hold_settings(catalog=()), sink)  # default label
    _drive_to_validated(session)

    hold = live_serialize.request_detail(session, _only(session))["goods_type_hold"]

    assert hold["catalog_configured"] is True
    assert hold["catalog"] == ["0000 - General Cargo"]


def test_not_configured_only_when_the_label_is_blank(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Goods-type catalog not configured' appears only in the degenerate case:
    a blank general-cargo label AND no catalog — nothing valid to offer."""
    _install_queue(monkeypatch, QueueSpy())
    _forbid_demo_provider(monkeypatch)
    session = _build_session(_hold_settings(catalog=(), general_cargo_label=""), sink)
    _drive_to_validated(session)

    hold = live_serialize.request_detail(session, _only(session))["goods_type_hold"]

    assert hold["catalog_configured"] is False
    assert hold["catalog"] == []


# --- the operator pick -----------------------------------------------------------


def test_an_operator_pick_enqueues_under_it_and_audits_the_operator(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    audit = CollectingAudit()
    session = _build_session(_hold_settings(), sink, audit=audit)
    _drive_to_validated(session)
    request = _only(session)

    session.decide_goods_type(request.request_id, goods_type="1234 - Machinery", by="Dana")
    assert request.awaiting_goods_type is False
    session.poll()  # now enqueues under the pick

    assert len(spy.enqueued) == 1
    assert spy.enqueued[0].goods_type == "1234 - Machinery"
    picked = _goods_type_audits(audit)
    assert picked == [{"goods_type": "1234 - Machinery", "source": "operator", "by": "Dana"}]


def test_an_unnamed_operator_is_refused(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_queue(monkeypatch, QueueSpy())
    _forbid_demo_provider(monkeypatch)
    session = _build_session(_hold_settings(), sink)
    _drive_to_validated(session)
    request = _only(session)

    with pytest.raises(LiveSequenceError, match="named person"):
        session.decide_goods_type(request.request_id, goods_type="1234 - Machinery", by="  ")


def test_a_label_outside_the_catalog_is_refused(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    session = _build_session(_hold_settings(), sink)
    _drive_to_validated(session)
    request = _only(session)

    with pytest.raises(LiveSequenceError, match="not a Goods Type"):
        session.decide_goods_type(request.request_id, goods_type="9999 - Not Offered", by="Dana")
    assert request.operator_goods_type is None
    assert spy.enqueued == []


# --- fingerprint discard ---------------------------------------------------------


def test_a_record_change_discards_the_pick_and_holds_again(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    _forbid_demo_provider(monkeypatch)
    audit = CollectingAudit()
    session = _build_session(_hold_settings(), sink, audit=audit)
    _drive_to_validated(session)
    request = _only(session)
    session.decide_goods_type(request.request_id, goods_type="1234 - Machinery", by="Dana")

    # The record changes (e.g. a client reply restated the commodity).
    request.record = request.record.model_copy(
        update={"commodity": "Something entirely different"}
    )
    session.poll()

    assert request.operator_goods_type is None  # the pick was discarded
    assert request.awaiting_goods_type is True  # held again
    assert spy.enqueued == []
    assert any(a["source"] == "discarded" for a in _goods_type_audits(audit))


# --- persistence across restart --------------------------------------------------


def test_restart_after_a_pick_enqueues_under_it_without_asking_again(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _hold_settings()
    durable = bootstrap.build_memory_store()

    # First run: reach the hold, operator picks (persisted to `durable`).
    _install_queue(monkeypatch, QueueSpy())
    _forbid_demo_provider(monkeypatch)
    session1 = _build_session(settings, sink, durable=durable)
    _drive_to_validated(session1)
    request_id = _only(session1).request_id
    session1.decide_goods_type(request_id, goods_type="5678 - Perishables", by="Dana")

    # Restart: a fresh session over the SAME durable store.
    spy2 = QueueSpy()
    _install_queue(monkeypatch, spy2)
    session2 = _build_session(settings, CollectingEmailSink(), durable=durable)
    restored = session2.requests[request_id]
    assert restored.operator_goods_type == "5678 - Perishables"  # pick restored
    assert restored.operator_goods_type_by == "Dana"

    session2.poll()  # applies the pick, enqueues — no re-ask
    assert restored.awaiting_goods_type is False
    assert len(spy2.enqueued) == 1
    assert spy2.enqueued[0].goods_type == "5678 - Perishables"


def test_restart_without_a_pick_holds_again_with_no_duplicate_audit(
    sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _hold_settings()
    durable = bootstrap.build_memory_store()
    audit = CollectingAudit()

    _install_queue(monkeypatch, QueueSpy())
    _forbid_demo_provider(monkeypatch)
    session1 = _build_session(settings, sink, durable=durable, audit=audit)
    _drive_to_validated(session1)
    request_id = _only(session1).request_id
    assert session1.requests[request_id].awaiting_goods_type is True

    # Restart with no pick: it holds again, and nothing is audited (a hold never
    # emits a goods-type decision, so a restart cannot duplicate one).
    spy2 = QueueSpy()
    _install_queue(monkeypatch, spy2)
    session2 = _build_session(settings, CollectingEmailSink(), durable=durable, audit=audit)
    session2.poll()

    assert session2.requests[request_id].awaiting_goods_type is True
    assert spy2.enqueued == []
    assert _goods_type_audits(audit) == []  # no decision, no duplicate


# --- backward-compatible load ----------------------------------------------------


def test_a_requests_record_written_before_this_change_loads() -> None:
    """An existing durable record with no operator_goods_type fields loads with
    them defaulting to None — the new fields are optional."""
    from translog_quote.domain.shipment import RequestSource, ShipmentRecord
    from translog_quote.domain.workflow import QuotationRequest

    store = bootstrap.build_memory_store()
    # Simulate an old record: dump WITHOUT the new fields, then re-validate.
    old = QuotationRequest(
        request_id="R-OLD",
        state=RequestState.VALIDATED,
        record=ShipmentRecord(request_id="R-OLD", source=RequestSource.EMAIL),
        client_address="c@example.com",
    ).model_dump()
    del old["operator_goods_type"]
    del old["operator_goods_type_fingerprint"]
    del old["operator_goods_type_by"]
    store.save_request(QuotationRequest.model_validate(old))

    loaded = store.get_request("R-OLD")
    assert loaded is not None
    assert loaded.operator_goods_type is None
    assert loaded.operator_goods_type_fingerprint is None
    assert loaded.operator_goods_type_by is None
