"""Approver notes, the client email preview, and the extra rate facts.

- A note to the client goes into the quotation after the figures, exactly as
  typed; an internal note reaches only the approver email and the audit.
- The preview is the email APPROVE would send — same composer, same choice,
  same note — and it records nothing and sends nothing.
- Arrival and per-kg rate are carried verbatim from WebCargo for display.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.unit import test_web_live as web
from tests.unit.test_rate_strategy_options import (
    APPROVER,
    CHEAP,
    FAST,
    _quotation_body,
    _review,
    _session,
    only_request,
)
from tests.unit.test_webcargo_extraction import record

from translog_quote.adapters.webcargo.browser.mapper import map_record
from translog_quote.domain.quotation import NOTE_MAX_CHARS
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_session import LiveSequenceError
from translog_quote.interface.web.server import _LIVE_ACTIONS, _LIVE_READS
from translog_quote.pipeline.audit import AuditEventType

if TYPE_CHECKING:
    from translog_quote.adapters.email import CollectingEmailSink
    from translog_quote.config import Settings
    from translog_quote.interface.web.live_session import LiveSession

#: The live-view suite's offline settings and collecting sink, reused as-is.
settings = web.settings
sink = web.sink

NOTE = "Space is limited on this departure.\nPlease confirm by Friday."


def _ready(settings: Settings, sink: CollectingEmailSink, mp: pytest.MonkeyPatch) -> LiveSession:
    return _session(settings, sink, mp, rates=(FAST, CHEAP))


def _rid(session: LiveSession) -> str:
    return only_request(session).request_id  # type: ignore[attr-defined,no-any-return]


# --- notes ------------------------------------------------------------------------


def test_the_client_note_is_sent_after_the_figures_exactly_as_typed(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _ready(settings, sink, monkeypatch)

    session.decide(
        _rid(session),
        choice="approve",
        by=APPROVER,
        option="cheapest",
        client_note=NOTE,
        internal_note="Client prefers Etihad; checked with ops.",
    )
    body = _quotation_body(sink)

    assert (
        body.index("Air freight:") < body.index("NOTE FROM TRANSLOG") < body.index("Kind regards")
    )
    assert "  Space is limited on this departure." in body
    assert "  Please confirm by Friday." in body
    assert "checked with ops" not in body  # the internal note never reaches the client


def test_the_approver_email_and_audit_carry_both_notes(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _ready(settings, sink, monkeypatch)

    session.decide(
        _rid(session), choice="approve", by=APPROVER, client_note=NOTE, internal_note="ops ok"
    )

    review = _review(sink)
    assert "NOTE TO THE CLIENT (sent with the quotation)" in review
    assert "INTERNAL NOTE (not sent to the client)" in review
    assert "ops ok" in review
    notes = [e for e in session.audit.events if e.event is AuditEventType.QUOTATION_NOTE]  # type: ignore[union-attr]
    assert len(notes) == 1
    assert notes[0].detail == {"by": APPROVER, "client_note": NOTE, "internal_note": "ops ok"}
    record_json = live_serialize.decision_json(only_request(session))  # type: ignore[arg-type]
    assert record_json is not None
    assert record_json["client_note"] == NOTE
    assert record_json["internal_note"] == "ops ok"


def test_no_note_sends_the_quotation_as_before(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _ready(settings, sink, monkeypatch)

    session.decide(_rid(session), choice="approve", by=APPROVER, client_note="   ")

    assert "NOTE FROM TRANSLOG" not in _quotation_body(sink)
    assert not [e for e in session.audit.events if e.event is AuditEventType.QUOTATION_NOTE]  # type: ignore[union-attr]


def test_a_too_long_note_is_refused_before_anything_happens(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _ready(settings, sink, monkeypatch)
    before = len(sink.sent)

    with pytest.raises(LiveSequenceError, match=f"at most {NOTE_MAX_CHARS} characters"):
        session.decide(
            _rid(session), choice="approve", by=APPROVER, client_note="x" * (NOTE_MAX_CHARS + 1)
        )

    assert len(sink.sent) == before
    assert only_request(session).decision is None  # type: ignore[attr-defined]


def test_a_decline_ignores_the_notes_and_sends_nothing(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _ready(settings, sink, monkeypatch)
    to_client_before = len(web.to_client(sink))

    session.decide(_rid(session), choice="decline", by=APPROVER, reason="no", client_note=NOTE)

    assert len(web.to_client(sink)) == to_client_before
    assert not [e for e in session.audit.events if e.event is AuditEventType.QUOTATION_NOTE]  # type: ignore[union-attr]


# --- preview ----------------------------------------------------------------------


def test_the_preview_is_exactly_the_email_approve_sends(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _ready(settings, sink, monkeypatch)
    rid = _rid(session)

    preview = session.preview_quotation(rid, option="cheapest", client_note=NOTE)
    session.decide(rid, choice="approve", by=APPROVER, option="cheapest", client_note=NOTE)

    sent = web.to_client(sink)[-1]
    assert preview["body"] == sent.body_text  # type: ignore[attr-defined]
    assert preview["subject"] == sent.subject  # type: ignore[attr-defined]
    assert preview["to"] == sent.to_address  # type: ignore[attr-defined]


def test_the_preview_records_nothing_and_sends_nothing(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _ready(settings, sink, monkeypatch)
    events_before, sent_before = len(session.audit.events), len(sink.sent)  # type: ignore[union-attr]

    session.preview_quotation(_rid(session), option="fastest", client_note=NOTE)
    session.preview_quotation(_rid(session), rate_ref="ref:EY")

    assert len(session.audit.events) == events_before  # type: ignore[union-attr]
    assert len(sink.sent) == sent_before
    assert only_request(session).decision is None  # type: ignore[attr-defined]


def test_the_preview_refuses_what_approve_would_refuse(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _ready(settings, sink, monkeypatch)
    rid = _rid(session)

    with pytest.raises(LiveSequenceError, match="not one of this search's eligible rates"):
        session.preview_quotation(rid, rate_ref="ref:not-a-rate")
    with pytest.raises(LiveSequenceError, match="at most"):
        session.preview_quotation(rid, client_note="x" * (NOTE_MAX_CHARS + 1))
    session.decide(rid, choice="approve", by=APPROVER)
    with pytest.raises(LiveSequenceError, match="no quotation awaiting a decision"):
        session.preview_quotation(rid)


def test_the_preview_route_is_read_only_and_separate_from_the_actions() -> None:
    assert {"quotation/preview"} == _LIVE_READS
    assert not _LIVE_READS & set(_LIVE_ACTIONS)


# --- arrival and per-kg, verbatim -------------------------------------------------


def test_arrival_and_per_kg_are_carried_as_webcargo_printed_them() -> None:
    rate = map_record(record())

    assert rate.arrival_label == "Wed 16 Sep - 16:30"
    assert rate.rate_per_kg_label == "167.65 Rs/kg"


@pytest.mark.parametrize(
    ("rate_cell", "price_block", "per_kg"),
    [
        ("Rate 167.65 Rs/kg", "167.65 Rs/kg/ 83,825 Rs", "167.65 Rs/kg"),
        ("", "400.00 Rs/kg/ 40,000 Rs", "400.00 Rs/kg"),
        ("", "40,000 Rs", ""),
    ],
)
def test_the_per_kg_rate_falls_back_to_the_price_block_or_is_absent(
    rate_cell: str, price_block: str, per_kg: str
) -> None:
    assert map_record(record(rate=rate_cell, price=price_block)).rate_per_kg_label == per_kg


def test_the_payload_and_review_email_show_arrival_and_per_kg(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    fast = FAST.model_copy(
        update={"arrival_label": "Thu 21 Sep - 16:30", "rate_per_kg_label": "60.00 INR/kg"}
    )
    session = _session(settings, sink, monkeypatch, rates=(fast, CHEAP))
    request = only_request(session)
    card = live_serialize.approval_json(
        request.packet,  # type: ignore[attr-defined]
        request.rates,  # type: ignore[attr-defined]
        approver="x",
        options=request.rate_options,  # type: ignore[attr-defined]
        recommended=request.recommended_option,  # type: ignore[attr-defined]
    )
    fastest = next(o for o in card["options"] if o["key"] == "fastest")  # type: ignore[attr-defined]
    cheapest = next(o for o in card["options"] if o["key"] == "cheapest")  # type: ignore[attr-defined]
    assert (fastest["arrival"], fastest["per_kg"]) == ("Thu 21 Sep - 16:30", "60.00 INR/kg")
    assert (cheapest["arrival"], cheapest["per_kg"]) == (None, None)
    assert card["note_max_chars"] == NOTE_MAX_CHARS

    session.decide(request.request_id, choice="approve", by=APPROVER)  # type: ignore[attr-defined]

    review = _review(sink)
    assert "Arrives : Thu 21 Sep - 16:30" in review
    assert "Price   : 30000 INR (60.00 INR/kg)" in review


def test_a_request_with_no_subject_is_named_so() -> None:
    from types import SimpleNamespace

    request = SimpleNamespace(subject="", request_id="account-a:R-GMAIL-9c8378463d")
    assert live_serialize._headline(request) == "(no subject) · R-GMAIL-9c8378463d"  # type: ignore[arg-type]
