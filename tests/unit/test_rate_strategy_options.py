"""Fastest vs cheapest: both options from one search, chosen by a person.

Domain: `rate_options` picks the fastest and the cheapest from the *same*
eligible set (so both obey the same eligibility rules), never shows one rate
twice, and withholds the cheapest when prices cannot be compared.
`detect_rate_preference` reads only explicit, un-negated wording.

Live session: the client's wording only preselects an option; the operator's
choice (or the recommendation when they make none) is what the approved
quotation is built from, and nothing reaches the client before approval.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from tests.unit import test_web_live as web
from tests.unit.test_gmail_thread import (
    ENQUIRY,
    ENQUIRY_EXTRACTION,
    REPLY,
    REPLY_EXTRACTION,
    ScriptedExtractor,
)
from tests.unit.test_quotation_compose import packet_for
from tests.unit.test_web_live import (
    APPROVER,
    CLIENT,
    GrowingSource,
    only_request,
    to_approver,
    to_client,
)

from translog_quote import bootstrap
from translog_quote.domain.quotation import compose_review_body
from translog_quote.domain.rates import (
    RatePreference,
    RateRestrictions,
    RateSearchResult,
    RateStrategy,
    TransitTime,
    TransitUnit,
    detect_rate_preference,
    filter_rates,
    rate_options,
    recommended_strategy,
)
from translog_quote.domain.rates.model import Rate
from translog_quote.domain.workflow import RequestState
from translog_quote.interface.web import live_serialize
from translog_quote.interface.web.live_session import LiveSequenceError, LiveSession
from translog_quote.interface.web.server import _LIVE_ACTIONS
from translog_quote.pipeline.audit import AuditEventType

if TYPE_CHECKING:
    from translog_quote.adapters.email import CollectingEmailSink
    from translog_quote.config import Settings
    from translog_quote.domain.rates import RateQuery


def _rate(
    code: str,
    amount: str | None,
    hours: int | None,
    *,
    currency: str | None = "INR",
    door: bool | None = None,
) -> Rate:
    return Rate(
        carrier_code=code,
        carrier_name=f"Carrier {code}",
        product="GEN",
        total_amount=Decimal(amount) if amount is not None else None,
        currency=currency,
        transit=TransitTime(value=hours, unit=TransitUnit.HOURS) if hours is not None else None,
        restrictions=RateRestrictions(serves_door_delivery=door),
        source_ref=f"ref:{code}",
        departure_date_label="20/09/2026",
    )


#: The live-view suite's offline settings and collecting sink, reused as-is.
settings = web.settings
sink = web.sink

FAST = _rate("QR", "30000", 20)  # fastest, dearer
CHEAP = _rate("EY", "18000", 50)  # cheapest, slower
MIDDLE = _rate("EK", "25000", 30)


# --- domain: choosing both options from one eligible set -----------------------


def test_both_options_come_from_the_same_eligible_set() -> None:
    options = rate_options((MIDDLE, CHEAP, FAST))

    assert options is not None
    assert options.fastest.rate == FAST
    assert options.cheapest is not None
    assert options.cheapest.rate == CHEAP
    assert options.same_rate is False
    assert "lowest eligible total" in options.cheapest.reason
    assert "fastest eligible transit" in options.fastest.reason


def test_the_same_rate_is_one_option_not_two() -> None:
    both = _rate("TK", "15000", 18)
    options = rate_options((FAST, both, CHEAP))

    assert options is not None
    assert options.same_rate is True
    assert options.fastest.rate == options.cheapest.rate == both  # type: ignore[union-attr]


def test_ties_break_on_the_other_key_then_carrier() -> None:
    """Equal price: the faster wins cheapest. Equal transit: the cheaper wins
    fastest. Fully equal: the carrier code keeps the result deterministic."""
    slow_cheap = _rate("AA", "10000", 40)
    fast_cheap = _rate("BB", "10000", 22)
    dear_fast = _rate("CC", "12000", 22)

    options = rate_options((slow_cheap, dear_fast, fast_cheap))

    assert options is not None
    assert options.cheapest is not None
    assert options.cheapest.rate == fast_cheap
    assert options.fastest.rate == fast_cheap
    assert options.same_rate is True


def test_no_eligible_rate_means_no_options() -> None:
    assert rate_options(()) is None


def test_prices_in_different_currencies_are_never_compared() -> None:
    usd = _rate("UA", "100", 30, currency="USD")
    options = rate_options((FAST, CHEAP, usd))

    assert options is not None
    assert options.fastest.rate == FAST  # transit still compares
    assert options.cheapest is None
    assert "different currencies (INR, USD)" in (options.cheapest_unavailable or "")
    assert options.selection_for(RateStrategy.CHEAPEST) is None


def test_an_ineligible_cheaper_rate_is_never_the_cheapest_option() -> None:
    """Both options run through the same filter: a rate with no price, no
    transit, or no door service for a door shipment cannot be chosen, however
    cheap it looks."""
    no_transit = _rate("ZZ", "1000", None)
    no_price = _rate("YY", None, 10)
    no_door = _rate("XX", "500", 12, door=None)
    door_cheap = _rate("WW", "20000", 40, door=True)
    door_fast = _rate("VV", "28000", 15, door=True)

    filtered = filter_rates(
        (no_transit, no_price, no_door, door_cheap, door_fast), requires_door_delivery=True
    )
    options = rate_options(filtered.eligible)

    assert options is not None
    assert options.fastest.rate == door_fast
    assert options.cheapest is not None
    assert options.cheapest.rate == door_cheap
    excluded = {e.rate.carrier_code for e in filtered.excluded}
    assert excluded == {"ZZ", "YY", "XX"}


def test_a_missing_price_withholds_the_cheapest_rather_than_ranking_it_first() -> None:
    """Defensive: filtering already drops price-less rates, but if one ever
    reached options it must not win "cheapest" by sorting as zero/negative."""
    options = rate_options((FAST, _rate("YY", None, 10)))

    assert options is not None
    assert options.cheapest is None
    assert "no stated price" in (options.cheapest_unavailable or "")


# --- domain: reading the client's preference ----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Please send the fastest option.",
        "This is URGENT, cargo must leave this week.",
        "We need it ASAP.",
        "Time-critical shipment for a production line.",
        "Please quote the shortest transit time.",
        "Earliest available flight please.",
        "No problem with the paperwork, we need the fastest flight.",
    ],
)
def test_speed_wording_reads_as_fastest(text: str) -> None:
    assert detect_rate_preference("Quote request", text) is RatePreference.FASTEST


@pytest.mark.parametrize(
    "text",
    [
        "Please quote the cheapest option.",
        "We are looking for the lowest price.",
        "Best rate please, budget is tight.",
        "Price is our priority on this one.",
        "Most economical routing please.",
        "No rush, but the cheapest option please.",
    ],
)
def test_price_wording_reads_as_cheapest(text: str) -> None:
    assert detect_rate_preference("Quote request", text) is RatePreference.CHEAPEST


@pytest.mark.parametrize(
    "text",
    [
        "500 kg, Delhi to Singapore, general cargo.",
        "Not urgent, any day next week is fine.",
        "No rush on this one.",
        "It doesn't need to be the fastest.",
        "We don't need express service.",
        "Please send a good rate soon.",
    ],
)
def test_no_clear_preference_reads_as_none(text: str) -> None:
    """Negated or vague wording states nothing — and "not urgent" is not read
    as a request for the cheapest rate either."""
    assert detect_rate_preference("Quote request", text) is None


def test_speed_and_price_together_are_unclear() -> None:
    text = "Urgent shipment, but please also keep it as cheap as possible — cheapest preferred."
    assert detect_rate_preference("", text) is RatePreference.UNCLEAR


def test_the_subject_line_counts() -> None:
    assert detect_rate_preference("URGENT: DEL-SIN quote", "500 kg") is RatePreference.FASTEST


def test_quoted_earlier_mail_is_ignored() -> None:
    body = (
        "Shipment date is 5 October.\n\n"
        "On Mon, 1 Oct 2026 at 10:00, Translog <quotes@translog.example> wrote:\n"
        "> Please confirm whether you need the fastest or the cheapest option.\n"
    )
    assert detect_rate_preference("Re: quote", body) is None
    assert detect_rate_preference("Re: quote", "> cheapest please\nShip date 5 Oct") is None


def test_a_from_line_in_the_body_does_not_hide_the_rest() -> None:
    body = "From: Delhi\nTo: Singapore\nPlease quote the cheapest option."
    assert detect_rate_preference("", body) is RatePreference.CHEAPEST


@pytest.mark.parametrize(
    ("preference", "expected"),
    [
        (RatePreference.CHEAPEST, RateStrategy.CHEAPEST),
        (RatePreference.FASTEST, RateStrategy.FASTEST),
        (RatePreference.UNCLEAR, RateStrategy.FASTEST),
        (None, RateStrategy.FASTEST),
    ],
)
def test_only_a_clear_price_request_changes_the_default(
    preference: RatePreference | None, expected: RateStrategy
) -> None:
    assert recommended_strategy(preference) is expected


# --- live session: recommendation, operator choice, quotation ------------------


class _FixedRates:
    """A rate provider returning fixed, comparable rates (fastest != cheapest)."""

    def __init__(self, rates: tuple[Rate, ...]) -> None:
        self._rates = rates

    def search(self, query: RateQuery) -> RateSearchResult:
        return RateSearchResult(rates=self._rates, adapter_id="fixed-test", is_simulated=True)


def _with_words(email: object, words: str) -> object:
    return email.model_copy(update={"body_text": f"{email.body_text}\n{words}"})  # type: ignore[attr-defined]


def _session(
    settings: Settings,
    sink: CollectingEmailSink,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enquiry_words: str = "",
    reply_words: str = "",
    late_words: str = "",
    rates: tuple[Rate, ...] = (MIDDLE, CHEAP, FAST),
) -> LiveSession:
    """The real enquiry → clarification → reply → rate-selected flow, with the
    client's wording appended to their own emails and fixed rates searched."""
    monkeypatch.setattr(bootstrap, "build_demo_rate_provider", lambda: _FixedRates(rates))
    enquiry = _with_words(ENQUIRY, enquiry_words) if enquiry_words else ENQUIRY
    reply = _with_words(REPLY, reply_words) if reply_words else REPLY
    states: list[tuple[object, ...]] = [(enquiry,), (enquiry, reply)]
    if late_words:
        # Delivered on a later poll (the test calls poll() once more): a client
        # message after the rates are on the approval card.
        states.append((enquiry, reply, _late(late_words)))
    session = LiveSession(
        settings,
        source=GrowingSource(*states),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(ENQUIRY_EXTRACTION, REPLY_EXTRACTION),
    )
    session.poll()
    session.approve_clarification(by=APPROVER)
    session.poll()
    assert only_request(session).state is RequestState.RATE_SELECTED  # type: ignore[attr-defined]
    return session


def _late(words: str) -> object:
    """A client message in the same thread, after the request validated."""
    return REPLY.model_copy(
        update={
            "message_id": "<late-1@client.example>",
            "in_reply_to": REPLY.message_id,
            "references": (ENQUIRY.message_id, REPLY.message_id),
            "body_text": words,
            "received_at": REPLY.received_at + timedelta(hours=2),
        }
    )


def _approval(session: LiveSession) -> dict[str, object]:
    request = only_request(session)
    return live_serialize.approval_json(
        request.packet,  # type: ignore[attr-defined]
        request.rates,  # type: ignore[attr-defined]
        approver="x",
        options=request.rate_options,  # type: ignore[attr-defined]
        preference=request.rate_preference,  # type: ignore[attr-defined]
        recommended=request.recommended_option,  # type: ignore[attr-defined]
        late_preference=request.late_preference,  # type: ignore[attr-defined]
    )


def _quotation_body(sink: CollectingEmailSink) -> str:
    return to_client(sink)[-1].body_text  # type: ignore[attr-defined]


def test_unclear_preference_keeps_fastest_as_the_default(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    request = only_request(session)

    assert request.rate_preference is None  # type: ignore[attr-defined]
    assert request.recommended_option is RateStrategy.FASTEST  # type: ignore[attr-defined]
    assert request.packet.selection.rate == FAST  # type: ignore[attr-defined]
    card = _approval(session)
    assert card["recommended_option"] == "fastest"
    assert [o["key"] for o in card["options"]] == ["fastest", "cheapest"]  # type: ignore[attr-defined]
    assert card["client_preference"] is None


def test_a_client_asking_for_the_cheapest_gets_cheapest_recommended(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, enquiry_words="Please quote the cheapest.")
    request = only_request(session)

    assert request.rate_preference is RatePreference.CHEAPEST  # type: ignore[attr-defined]
    assert request.recommended_option is RateStrategy.CHEAPEST  # type: ignore[attr-defined]
    assert request.packet.selection.rate == CHEAP  # type: ignore[attr-defined]
    card = _approval(session)
    assert card["recommended_option"] == "cheapest"
    assert card["carrier"] == "Carrier EY (EY)"
    cheapest = next(o for o in card["options"] if o["key"] == "cheapest")  # type: ignore[attr-defined]
    assert cheapest["recommended"] is True
    assert cheapest["price"] == "18000 INR"
    assert cheapest["departure_date"] == "20/09/2026"
    assert "lowest price" in str(card["client_preference_text"])


def test_selecting_rates_still_sends_nothing(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both options exist, one is recommended — and only the clarification has
    left. The quotation waits for a person."""
    _session(settings, sink, monkeypatch, enquiry_words="cheapest please")

    assert len(to_client(sink)) == 1
    assert to_approver(sink) == []


def test_the_latest_stated_preference_wins(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(
        settings,
        sink,
        monkeypatch,
        enquiry_words="Cheapest option please.",
        reply_words="Update: this has become urgent.",
    )
    request = only_request(session)

    assert request.rate_preference is RatePreference.FASTEST  # type: ignore[attr-defined]
    assert request.packet.selection.rate == FAST  # type: ignore[attr-defined]


def test_mixed_wording_is_unclear_and_falls_back_to_fastest(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, enquiry_words="Urgent, but cheapest preferred.")
    request = only_request(session)

    assert request.rate_preference is RatePreference.UNCLEAR  # type: ignore[attr-defined]
    assert request.recommended_option is RateStrategy.FASTEST  # type: ignore[attr-defined]
    assert "both speed and price" in str(_approval(session)["client_preference_text"])


def test_the_operator_can_quote_the_cheapest_against_the_default(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    request = session.decide(request_id, choice="approve", by=APPROVER, option="cheapest")

    assert request.state is RequestState.QUOTATION_SENT
    assert request.quoted_option is RateStrategy.CHEAPEST
    body = _quotation_body(sink)
    assert "Carrier EY (EY)" in body
    assert "18000 INR" in body
    assert "Carrier QR" not in body  # one rate, never a list (BR-10)
    chosen = [e for e in session.audit.events if e.event is AuditEventType.RATE_OPTION_CHOSEN]  # type: ignore[union-attr]
    assert [e.detail["option"] for e in chosen] == ["cheapest"]
    assert chosen[0].detail["recommended"] == "fastest"


def test_the_operator_can_quote_the_fastest_against_a_cheapest_recommendation(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, enquiry_words="cheapest please")
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    session.decide(request_id, choice="approve", by=APPROVER, option="fastest")

    body = _quotation_body(sink)
    assert "Carrier QR (QR)" in body
    assert "30000 INR" in body


def test_no_option_quotes_the_recommended_one(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that predates the choice (no `option`) keeps working: it quotes
    what the card recommended."""
    session = _session(settings, sink, monkeypatch, enquiry_words="lowest price please")
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    request = session.decide(request_id, choice="approve", by=APPROVER)

    assert request.quoted_option is RateStrategy.CHEAPEST
    assert "Carrier EY (EY)" in _quotation_body(sink)


def test_an_unknown_option_is_refused_and_nothing_is_sent(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    request_id = only_request(session).request_id  # type: ignore[attr-defined]
    before = len(sink.sent)

    with pytest.raises(LiveSequenceError, match="not a rate option"):
        session.decide(request_id, choice="approve", by=APPROVER, option="cheapest-ish")

    assert len(sink.sent) == before
    assert only_request(session).decision is None  # type: ignore[attr-defined]
    # Still decidable: the refusal recorded nothing.
    session.decide(request_id, choice="approve", by=APPROVER, option="fastest")
    assert "Carrier QR (QR)" in _quotation_body(sink)


def test_an_uncomparable_cheapest_is_refused_never_substituted(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    usd = _rate("UA", "100", 30, currency="USD")
    session = _session(
        settings,
        sink,
        monkeypatch,
        enquiry_words="cheapest please",
        rates=(FAST, CHEAP, usd),
    )
    request = only_request(session)
    request_id = request.request_id  # type: ignore[attr-defined]

    # The client asked for price, but price cannot be compared: fastest stays.
    assert request.recommended_option is RateStrategy.FASTEST  # type: ignore[attr-defined]
    card = _approval(session)
    assert [o["key"] for o in card["options"]] == ["fastest"]  # type: ignore[attr-defined]
    assert "different currencies" in str(card["cheapest_unavailable"])
    before = len(sink.sent)

    with pytest.raises(LiveSequenceError, match="cheapest rate cannot be quoted"):
        session.decide(request_id, choice="approve", by=APPROVER, option="cheapest")

    assert len(sink.sent) == before


def test_declining_sends_nothing_whatever_option_is_on_screen(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    request = session.decide(
        request_id, choice="decline", by=APPROVER, reason="client went elsewhere", option="cheapest"
    )

    assert request.state is RequestState.MAKER_REJECTED
    assert request.quoted_option is None
    assert len(to_client(sink)) == 1  # the clarification only


def test_the_option_travels_over_http(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    _LIVE_ACTIONS["quotation/decide"](
        session,
        {"request_id": request_id, "decision": "approve", "by": APPROVER, "option": "cheapest"},
    )

    assert "Carrier EY (EY)" in _quotation_body(sink)
    assert to_client(sink)[-1].to_address == CLIENT  # type: ignore[attr-defined]


def test_identical_options_show_once_end_to_end(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = _rate("TK", "15000", 18)
    session = _session(settings, sink, monkeypatch, rates=(FAST, both, CHEAP))

    card = _approval(session)

    assert [(o["key"], o["label"]) for o in card["options"]] == [  # type: ignore[attr-defined]
        ("fastest", "Fastest and cheapest")
    ]
    rates = live_serialize.rates_json(only_request(session).rates)  # type: ignore[attr-defined]
    assert rates["cheapest_ref"] is None
    assert rates["fastest_is_cheapest"] is True
    # "cheapest" still resolves — to the same rate — so the choice is harmless.
    request_id = only_request(session).request_id  # type: ignore[attr-defined]
    session.decide(request_id, choice="approve", by=APPROVER, option="cheapest")
    assert "Carrier TK (TK)" in _quotation_body(sink)


def test_the_rate_board_marks_the_cheapest_beside_the_fastest(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    rates = live_serialize.rates_json(only_request(session).rates)  # type: ignore[attr-defined]

    assert rates["selection"]["source_ref"] == "ref:QR"  # type: ignore[index]
    assert rates["cheapest_ref"] == "ref:EY"
    assert rates["fastest_is_cheapest"] is False


def test_the_preference_survives_a_restart(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted on the stored request, so the re-derived card after a restart
    recommends the same option instead of silently reverting to fastest."""
    session = _session(settings, sink, monkeypatch, enquiry_words="cheapest please")
    request_id = only_request(session).request_id  # type: ignore[attr-defined]
    stored = session._durable.get_request(request_id)  # noqa: SLF001
    assert stored is not None
    assert stored.rate_preference is RatePreference.CHEAPEST

    restarted = LiveSession(
        settings,
        source=GrowingSource((), ()),  # type: ignore[arg-type]
        sink=sink,
        extractor=ScriptedExtractor(),
    )
    request = restarted.requests[request_id]
    assert request.rate_preference is RatePreference.CHEAPEST
    restarted.poll()  # the rate pass rebuilds the card
    assert request.packet is not None
    assert request.packet.selection.rate == CHEAP


def test_the_decision_record_says_which_rate_was_quoted(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    request = session.decide(
        only_request(session).request_id,  # type: ignore[attr-defined]
        choice="approve",
        by=APPROVER,
        option="cheapest",
    )

    record = live_serialize.decision_json(request)

    assert record is not None
    assert record["quoted_option"] == "cheapest"
    assert str(record["quoted_rate"]).startswith("Carrier EY (EY) — 18000 INR")


# --- a preference stated after the rates are on the card -----------------------


def test_a_late_preference_is_shown_but_changes_nothing_on_its_own(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rates are up (fastest preselected, client said nothing); the client then
    writes "cheapest please". The operator is told, the preselection and the
    packet stay put, and nothing is sent."""
    session = _session(
        settings, sink, monkeypatch, late_words="Please go with the cheapest option."
    )
    before = len(sink.sent)

    session.poll()  # the late message arrives
    request = only_request(session)

    assert len(sink.sent) == before
    assert request.late_messages  # type: ignore[attr-defined]
    assert request.late_preference is RatePreference.CHEAPEST  # type: ignore[attr-defined]
    assert request.rate_preference is RatePreference.CHEAPEST  # type: ignore[attr-defined]
    assert request.recommended_option is RateStrategy.FASTEST  # type: ignore[attr-defined]
    assert request.packet.selection.rate == FAST  # type: ignore[attr-defined]
    assert request.decision is None  # type: ignore[attr-defined]
    card = _approval(session)
    assert card["recommended_option"] == "fastest"
    assert card["late_preference"] == "cheapest"
    assert "was not changed" in str(card["late_preference_notice"])
    stored = session._durable.get_request(request.request_id)  # type: ignore[attr-defined]  # noqa: SLF001
    assert stored is not None
    assert stored.rate_preference is RatePreference.CHEAPEST


def test_the_operator_acts_on_a_late_preference_by_choosing(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, late_words="Cheapest please.")
    session.poll()
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    session.decide(request_id, choice="approve", by=APPROVER, option="cheapest")

    assert "Carrier EY (EY)" in _quotation_body(sink)
    assert "Client preference: asked for the lowest price" in _review(sink)


def test_a_late_message_that_agrees_says_so(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, late_words="This is urgent now.")
    session.poll()

    card = _approval(session)

    assert card["late_preference"] == "fastest"
    assert "matches the preselected option" in str(card["late_preference_notice"])


def test_a_late_message_without_a_preference_changes_nothing(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(
        settings, sink, monkeypatch, late_words="Thanks, also the cartons are palletised."
    )
    session.poll()
    request = only_request(session)

    assert request.late_messages  # type: ignore[attr-defined]
    assert request.late_preference is None  # type: ignore[attr-defined]
    assert _approval(session)["late_preference_notice"] is None


def test_a_late_message_after_the_decision_changes_nothing(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, late_words="Actually, cheapest please.")
    request_id = only_request(session).request_id  # type: ignore[attr-defined]
    session.decide(request_id, choice="approve", by=APPROVER)
    sent = len(sink.sent)

    session.poll()
    request = only_request(session)

    assert len(sink.sent) == sent
    assert request.late_preference is None  # type: ignore[attr-defined]
    assert request.quoted_option is RateStrategy.FASTEST  # type: ignore[attr-defined]


# --- the approver's review email -------------------------------------------------


def _review(sink: CollectingEmailSink) -> str:
    return to_approver(sink)[-1].body_text  # type: ignore[attr-defined]


def test_the_review_labels_the_selected_rate_and_its_alternative(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, enquiry_words="cheapest please")
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    session.decide(request_id, choice="approve", by=APPROVER, option="cheapest")
    review = _review(sink)

    selected = review.index("SELECTED RATE — CHEAPEST")
    alternative = review.index("ALTERNATIVE ELIGIBLE RATE — FASTEST (not selected)")
    assert selected < alternative
    chosen_block = review[selected:alternative]
    other_block = review[alternative:]
    for fact in (
        "Airline : Carrier EY (EY)",
        "Departs : 20/09/2026",
        "Transit : 50 hours",
        "Price   : 18000 INR",
    ):
        assert fact in chosen_block
    for fact in (
        "Airline : Carrier QR (QR)",
        "Departs : 20/09/2026",
        "Transit : 20 hours",
        "Price   : 30000 INR",
    ):
        assert fact in other_block
    assert "Client preference: asked for the lowest price" in review


def test_the_review_follows_the_operators_switch_to_fastest(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, enquiry_words="cheapest please")
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    session.decide(request_id, choice="approve", by=APPROVER, option="fastest")
    review = _review(sink)

    assert "SELECTED RATE — FASTEST" in review
    assert "ALTERNATIVE ELIGIBLE RATE — CHEAPEST (not selected)" in review


def test_the_review_says_when_there_is_no_alternative(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    both = _rate("TK", "15000", 18)
    session = _session(settings, sink, monkeypatch, rates=(FAST, both, CHEAP))
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    session.decide(request_id, choice="approve", by=APPROVER)
    review = _review(sink)

    assert "SELECTED RATE — FASTEST AND CHEAPEST" in review
    assert "ALTERNATIVE: none — the fastest eligible rate is also the cheapest." in review
    assert "Client preference: not stated" in review


def test_the_review_says_why_the_cheapest_is_unavailable(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    usd = _rate("UA", "100", 30, currency="USD")
    session = _session(settings, sink, monkeypatch, rates=(FAST, CHEAP, usd))
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    session.decide(request_id, choice="approve", by=APPROVER)
    review = _review(sink)

    assert "SELECTED RATE — FASTEST" in review
    assert (
        "ALTERNATIVE (CHEAPEST): not available — rates are quoted in different currencies" in review
    )


def test_a_missing_departure_is_stated_not_invented() -> None:
    undated_fast = FAST.model_copy(update={"departure_date_label": ""})
    options = rate_options((undated_fast, CHEAP))
    assert options is not None
    packet = packet_for().model_copy(
        update={
            "selection": options.fastest,
            "options": options,
            "selected_option": RateStrategy.FASTEST,
        }
    )

    body = compose_review_body(packet, is_simulated=True)

    assert "Departs : Not stated by the provider" in body
    assert "Departs : 20/09/2026" in body  # the cheapest alternative has one


# --- the "other rates" column and quoting one of them ---------------------------

TRADE = _rate("TK", "21000", 36)  # between cheapest and fastest on both axes: a trade-off


def test_the_approval_payload_carries_the_other_rates(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, rates=(MIDDLE, CHEAP, FAST, TRADE))

    card = _approval(session)

    assert [r["rate_ref"] for r in card["other_rates"]] == ["ref:TK", "ref:EK"]  # type: ignore[attr-defined]
    first = card["other_rates"][0]  # type: ignore[index]
    assert first["carrier"] == "Carrier TK (TK)"
    assert first["price"] == "21000 INR"
    assert first["departure_date"] == "20/09/2026"
    assert card["other_rates_total"] == 2


def test_the_operator_can_quote_one_of_the_other_rates(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, enquiry_words="cheapest please")
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    request = session.decide(request_id, choice="approve", by=APPROVER, rate_ref="ref:EK")

    assert request.quoted_option is RateStrategy.OTHER
    body = _quotation_body(sink)
    assert "Carrier EK (EK)" in body
    assert "25000 INR" in body
    review = _review(sink)
    assert "SELECTED RATE — OTHER ELIGIBLE RATE (operator's choice)" in review
    assert "FOR COMPARISON — FASTEST (not selected)" in review
    assert "FOR COMPARISON — CHEAPEST (not selected)" in review
    assert "chosen by the operator" in review
    chosen = [e for e in session.audit.events if e.event is AuditEventType.RATE_OPTION_CHOSEN]  # type: ignore[union-attr]
    assert chosen[-1].detail["option"] == "other"
    assert chosen[-1].detail["rate_ref"] == "ref:EK"
    assert chosen[-1].detail["recommended"] == "cheapest"


def test_an_excluded_rate_can_never_be_quoted_by_reference(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate filtering removed (no transit) is not in the eligible set, so its
    ref is refused — and nothing is recorded or sent."""
    unrankable = _rate("ZZ", "100", None)
    session = _session(settings, sink, monkeypatch, rates=(FAST, CHEAP, unrankable))
    request_id = only_request(session).request_id  # type: ignore[attr-defined]
    before = len(sink.sent)

    for ref in ("ref:ZZ", "ref:not-a-rate"):
        with pytest.raises(LiveSequenceError, match="not one of this search's eligible rates"):
            session.decide(request_id, choice="approve", by=APPROVER, rate_ref=ref)

    assert len(sink.sent) == before
    assert only_request(session).decision is None  # type: ignore[attr-defined]


def test_a_rate_and_a_named_option_together_are_refused(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    with pytest.raises(LiveSequenceError, match="not both"):
        session.decide(
            request_id, choice="approve", by=APPROVER, option="cheapest", rate_ref="ref:EK"
        )
    with pytest.raises(LiveSequenceError, match="needs the rate itself"):
        session.decide(request_id, choice="approve", by=APPROVER, option="other")

    assert only_request(session).decision is None  # type: ignore[attr-defined]


def test_a_reference_to_the_fastest_rate_is_recorded_as_fastest(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    request = session.decide(request_id, choice="approve", by=APPROVER, rate_ref="ref:QR")

    assert request.quoted_option is RateStrategy.FASTEST
    assert "SELECTED RATE — FASTEST" in _review(sink)


def test_the_rate_reference_travels_over_http(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch)
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    _LIVE_ACTIONS["quotation/decide"](
        session,
        {"request_id": request_id, "decision": "approve", "by": APPROVER, "rate_ref": "ref:EK"},
    )

    assert "Carrier EK (EK)" in _quotation_body(sink)


def test_other_is_never_a_ranked_option() -> None:
    options = rate_options((FAST, CHEAP))
    assert options is not None
    assert options.selection_for(RateStrategy.OTHER) is None


def test_after_a_late_preference_the_card_says_what_the_enquiry_stated(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live E3: the enquiry stated nothing, a late reply asked for the cheapest.
    The card must not describe the late wording as the original ask above a
    fastest preselection: it says what the enquiry stated, and the late notice
    says what changed."""
    session = _session(settings, sink, monkeypatch, late_words="Please make it the cheapest.")
    session.poll()
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    card = live_serialize.snapshot(session, selected=request_id)["selected"]["approval"]  # type: ignore[index]

    assert card["client_preference"] is None  # what the card was built on
    assert card["client_preference_text"] == (
        "In the enquiry: The client did not state a speed or price preference."
    )
    assert card["late_preference"] == "cheapest"
    assert card["recommended_option"] == "fastest"
    assert "was not changed" in card["late_preference_notice"]


def test_without_a_late_message_the_preference_line_is_unchanged(
    settings: Settings, sink: CollectingEmailSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(settings, sink, monkeypatch, enquiry_words="cheapest please")
    request_id = only_request(session).request_id  # type: ignore[attr-defined]

    card = live_serialize.snapshot(session, selected=request_id)["selected"]["approval"]  # type: ignore[index]

    assert card["client_preference"] == "cheapest"
    assert card["client_preference_text"].startswith("The client asked for the lowest price")
