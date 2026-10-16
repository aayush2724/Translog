"""Edge cases found by the 16 Oct 2026 sweep of ~90 emails through the real model.

Each was either a silently wrong value reaching a quote, or a reply the system
could not read. Pinned here at the deterministic layer the fix lives in.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from tests.unit.test_clarification_loop import REQ, approve, complete, email, workflow
from tests.unit.test_location_clarification import (
    QueueSpy,
    _complete,
    _email,
    _install_queue,
    _ScriptedExtractor,
    _session,
    _Source,
    browser,  # noqa: F401  (fixture)
)
from tests.unit.test_openrouter_adapter import adapter_for, envelope

from translog_quote.adapters.email import CollectingEmailSink
from translog_quote.adapters.routing import CanonicalLocationResolver
from translog_quote.config import Settings
from translog_quote.domain.clarification import location_question
from translog_quote.domain.extraction import ExtractedValue, ExtractionResult, FieldStatus
from translog_quote.domain.extraction.prompt import with_reply_context
from translog_quote.domain.extraction.ship_date import (
    ambiguous_day_month,
    either_way_date,
    weekday_mismatch,
)
from translog_quote.domain.shipment import CargoDimensions, FieldName
from translog_quote.domain.shipment.units import (
    is_chargeable_only,
    is_per_piece,
    kilo_suffix_in,
    parse_number,
)
from translog_quote.domain.workflow import RequestState
from translog_quote.errors import UnresolvedLocation
from translog_quote.interface.web.live_serialize import request_detail, request_summary


def _extract(payload: dict[str, object]) -> ExtractionResult:
    adapter, _ = adapter_for(envelope(json.dumps(payload)))
    return adapter.extract_shipment("body")


# --- weight ----------------------------------------------------------------------


def test_a_per_piece_weight_is_multiplied_by_the_stated_pieces() -> None:
    result = _extract(
        {
            "pcs": {"status": "stated", "value": 2, "evidence": "2 pcs"},
            "weight_kg": {
                "status": "stated",
                "value": 50,
                "unit": "kg",
                "evidence": "50 kg each",
            },
        }
    )
    assert result.weight_kg.value == 100.0


def test_a_per_piece_weight_in_pounds_is_converted_then_multiplied() -> None:
    result = _extract(
        {
            "pcs": {"status": "stated", "value": 2},
            "weight_kg": {
                "status": "stated",
                "value": 110,
                "unit": "lbs",
                "evidence": "110 lbs each",
            },
        }
    )
    assert result.weight_kg.value == 99.8


def test_a_per_piece_weight_without_pieces_asks_for_the_total() -> None:
    result = _extract(
        {
            "weight_kg": {
                "status": "stated",
                "value": 50,
                "unit": "kg",
                "evidence": "50 kg per carton",
            }
        }
    )
    assert result.weight_kg.status is FieldStatus.INVALID
    wf, _ = workflow(complete(weight_kg=result.weight_kg))
    out = wf.handle(REQ, email("50 kg per carton"))
    assert out.clarification is not None
    assert "you gave 50 kg per piece" in out.clarification.body_text


@pytest.mark.parametrize(
    ("text", "per_piece"),
    [
        ("50 kg each", True),
        ("20 kg per carton", True),
        ("15 kg/pc", True),
        ("100 kg total, 50 kg each", False),
        ("Weight: 100 kg", False),
    ],
)
def test_per_piece_wording(text: str, per_piece: bool) -> None:
    assert is_per_piece(text) is per_piece


def test_a_thousands_k_is_restored() -> None:
    result = _extract(
        {"weight_kg": {"status": "stated", "value": 1.5, "unit": "kg", "evidence": "1.5k kg"}}
    )
    assert result.weight_kg.value == 1500.0
    assert not kilo_suffix_in("1.5kg", 1.5)


def test_a_chargeable_weight_alone_asks_for_the_actual_weight() -> None:
    result = _extract(
        {
            "weight_kg": {
                "status": "stated",
                "value": 120,
                "unit": "kg",
                "evidence": "Chargeable weight: 120 kg",
            }
        }
    )
    assert result.weight_kg.status is FieldStatus.INVALID
    wf, _ = workflow(complete(weight_kg=result.weight_kg))
    out = wf.handle(REQ, email("Chargeable weight: 120 kg"))
    assert out.clarification is not None
    assert "you gave the chargeable weight (120 kg)" in out.clarification.body_text
    assert not is_chargeable_only("Gross 100 kg, chargeable 120 kg")


# --- dimensions ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [("1 1/2", 1.5), ("1½", 1.5), ("3/4", 0.75), ("2.5", 2.5), ("1/0", None), ("abc", None)],
)
def test_numbers_with_fractions(text: str, value: float | None) -> None:
    assert parse_number(text) == value


def test_fractional_feet_convert() -> None:
    result = _extract(
        {
            "dimensions_in": {
                "status": "stated",
                "value": {"length": "1 1/2 ft", "width": 1, "height": 1},
                "unit": "ft",
            }
        }
    )
    assert result.dimensions_in.value == CargoDimensions(length=18, width=12, height=12)


# --- cargo type ------------------------------------------------------------------


def test_a_cargo_type_named_as_the_commodity_moves_to_cargo_type() -> None:
    result = _extract({"commodity": {"status": "stated", "value": "general cargo"}})
    assert result.commodity.status is FieldStatus.NOT_STATED
    assert result.cargo_type.value == "general cargo"


def test_a_real_commodity_is_untouched() -> None:
    result = _extract({"commodity": {"status": "stated", "value": "General cargo spare parts"}})
    assert result.commodity.value == "General cargo spare parts"


# --- dates -----------------------------------------------------------------------


def test_a_two_digit_year_date_that_reads_either_way_is_confirmed() -> None:
    assert ambiguous_day_month("05/11/26", date(2026, 11, 5)) is not None
    result = _extract(
        {"ship_date": {"status": "stated", "value": "2026-11-05", "evidence": "05/11/26"}}
    )
    wf, _ = workflow(complete(ship_date=result.ship_date))
    out = wf.handle(REQ, email("05/11/26"))
    assert out.clarification is not None
    assert "5 November 2026 or 11 May 2026" in out.clarification.body_text


def test_a_date_the_model_called_ambiguous_still_gets_both_readings() -> None:
    result = _extract(
        {"ship_date": {"status": "ambiguous", "evidence": "05/11/26", "note": "order unclear"}}
    )
    assert either_way_date("05/11/26") == "05/11/26"
    wf, _ = workflow(complete(ship_date=result.ship_date))
    out = wf.handle(REQ, email("05/11/26"))
    assert out.clarification is not None
    assert "5 November 2026 or 11 May 2026" in out.clarification.body_text


def test_a_weekday_that_does_not_match_the_date_is_confirmed() -> None:
    result = _extract(
        {
            "ship_date": {
                "status": "stated",
                "value": "2026-10-29",
                "evidence": "Monday, 29 October 2026",
            }
        }
    )
    assert result.ship_date.status is FieldStatus.AMBIGUOUS
    wf, _ = workflow(complete(ship_date=result.ship_date))
    out = wf.handle(REQ, email("Monday, 29 October 2026"))
    assert out.clarification is not None
    assert "29 October 2026 is a Thursday, not a Monday" in out.clarification.body_text


@pytest.mark.parametrize(
    "text",
    ["Thursday, 29 October 2026", "Monday 29th October", "29 October 2026"],
)
def test_matching_or_yearless_weekdays_are_untouched(text: str) -> None:
    assert weekday_mismatch(text, date(2026, 10, 29)) is None


# --- places ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "place", ["USA", "Dubai (UAE)", "Riyadh (KSA)", "Delhi (IND)", "Mumbai (DEL)"]
)
def test_country_codes_and_contradictions_are_never_sent_as_airports(place: str) -> None:
    with pytest.raises(UnresolvedLocation):
        CanonicalLocationResolver().resolve(place)


@pytest.mark.parametrize(
    ("place", "code"),
    [("Delhi (UAE)", "DEL"), ("Mumbai (BOM)", "BOM"), ("Nairobi (NBO)", "NBO"), ("DEL", "DEL")],
)
def test_genuine_codes_still_resolve(place: str, code: str) -> None:
    assert CanonicalLocationResolver().resolve(place).code == code


def test_same_airport_question_names_the_end() -> None:
    text = location_question(FieldName.DESTINATION, "Mumbai", same_as_other_end=True)
    assert "both point to the same airport" in text
    assert "destination airport" in text


def test_origin_and_destination_on_one_airport_are_asked_not_searched(
    browser: Settings,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    session = _session(
        browser,
        source=_Source((_email("<a@x>", "Mumbai to BOM", 0),)),
        extractor=_ScriptedExtractor(_complete("Mumbai", "BOM")),
        sink=CollectingEmailSink(),
    )
    session.poll()
    assert spy.enqueued == []
    held = next(r for r in session.requests.values() if r.clarification is not None)
    assert {u.field for u in held.clarification.unresolved} == {  # type: ignore[union-attr]
        FieldName.ORIGIN,
        FieldName.DESTINATION,
    }


# --- replies ---------------------------------------------------------------------


def test_the_next_reply_is_read_with_what_was_asked() -> None:
    no_unit = complete(dimensions_in=ExtractedValue[CargoDimensions].not_stated())
    answer = ExtractionResult(
        dimensions_in=ExtractedValue[CargoDimensions].stated(
            CargoDimensions(length=19.7, width=15.8, height=11.9)
        )
    )
    wf, _ = workflow(no_unit, answer)
    wf.handle(REQ, email("rate please", n=1))
    approve(wf)
    out = wf.handle(REQ, email("cm", n=2))
    extractor = wf._extractor  # type: ignore[attr-defined]
    assert extractor.calls == ["rate please", "cm"]  # only the reply's own text
    assert extractor.asked[0] == ()
    assert extractor.asked[1] == ("Dimensions",)
    assert out.state is RequestState.VALIDATED


def test_the_reply_context_carries_no_example_values() -> None:
    text = with_reply_context(("Destination: Frankfurt",), "yes")
    assert text.startswith("QUESTIONS THE DESK ASKED\n- Destination: Frankfurt")
    assert text.endswith("CLIENT REPLY\nyes")
    assert "DEL" not in text
    assert with_reply_context((), "hello") == "hello"


def test_a_message_after_validation_is_shown_to_the_desk(
    browser: Settings,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = QueueSpy()
    _install_queue(monkeypatch, spy)
    first = _email("<a@x>", "Delhi to Singapore", 0)
    late = _email("<b@x>", "Re: Delhi to Singapore", 5, in_reply_to="<a@x>")
    session = _session(
        browser,
        source=_Source((first,), (first, late)),
        extractor=_ScriptedExtractor(_complete("Delhi, India", "Singapore")),
        sink=CollectingEmailSink(),
    )
    session.poll()
    session.poll()
    (request,) = session.requests.values()
    assert [m.message_id for m in request.late_messages] == ["<b@x>"]
    assert request_summary(session, request)["late_messages"] == 1
    assert len(request_detail(session, request)["late_messages"]) == 1
