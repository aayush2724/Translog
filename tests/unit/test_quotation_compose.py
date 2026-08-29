"""The two composers, and the line between their audiences.

Outbound copy is customer-facing and must be reviewable, diffable and identical
across runs, so these tests assert on the text itself rather than on the fact
that some text was produced.
"""

from __future__ import annotations

import pytest
from tests.unit.test_quotation_stage import APPROVER, CLOCK, packet_for

from translog_quote.domain.quotation import (
    INTERNAL_SUBJECT_PREFIX,
    SIMULATED_RATE_NOTICE,
    Approved,
    build_quotation,
    compose_quotation_body,
    compose_review_body,
    compose_review_request,
    quotation_message,
)

APPROVER_MAILBOX = "approvals@translog.example"
CLIENT = "client@example.com"


def approval() -> Approved:
    return Approved(by=APPROVER, at=CLOCK.now())


def client_body(*, is_simulated: bool = True) -> str:
    packet = packet_for()
    return compose_quotation_body(
        packet.record, packet.selection, reference=packet.request_id, is_simulated=is_simulated
    )


# --- determinism ----------------------------------------------------------------


def test_the_same_input_composes_byte_identical_copy() -> None:
    """No model writes outbound copy, and nothing here reads a clock. Two runs
    that differ would mean a client could receive two different quotations for
    the same decision."""
    assert client_body() == client_body()
    assert compose_review_body(packet_for(), is_simulated=True) == compose_review_body(
        packet_for(), is_simulated=True
    )


# --- what the client sees -------------------------------------------------------


def test_the_client_quotation_carries_exactly_one_rate() -> None:
    """BR-10. Showing several options would rebuild the manual process this
    system exists to remove."""
    packet = packet_for()
    body = client_body()

    assert packet.selection.rate.carrier_name in body
    for runner_up in packet.selection.runners_up:
        assert runner_up.carrier_name not in body


def test_the_client_quotation_names_no_excluded_carrier_or_reason() -> None:
    packet = packet_for()
    body = client_body()

    assert packet.rates.excluded, "the fixture should exercise exclusions"
    for excluded in packet.rates.excluded:
        assert excluded.rate.carrier_name not in body
        assert excluded.reason.value not in body


def test_simulated_rates_are_disclosed_to_the_client() -> None:
    """The demo's whole credibility rests on this line. A quotation built from
    invented rates must say so in the message the client actually receives."""
    assert SIMULATED_RATE_NOTICE in client_body(is_simulated=True)


def test_a_non_simulated_quotation_carries_no_simulation_notice() -> None:
    assert SIMULATED_RATE_NOTICE not in client_body(is_simulated=False)


@pytest.mark.parametrize("invented", ["VAT", "GST", "insurance", "markup", "handling charge"])
def test_the_quotation_invents_no_commercial_term(invented: str) -> None:
    """No tax, insurance, handling charge or markup is specified anywhere in
    this project, so none is written into a client's quotation."""
    assert invented.lower() not in client_body().lower()


@pytest.mark.parametrize("label", ["Taxes and surcharges", "Validity", "Payment terms"])
def test_an_unspecified_commercial_term_says_so_rather_than_carrying_a_figure(
    label: str,
) -> None:
    """Named and left blank, not silently dropped. A quotation missing a
    validity line reads as one with no expiry; one that says "Not specified"
    reads as what it is."""
    line = next(line for line in client_body().splitlines() if label in line)

    assert line.strip().endswith("Not specified")
    assert not any(character.isdigit() for character in line)


def test_the_client_subject_carries_no_internal_marker() -> None:
    quotation = build_quotation(packet_for(), approval(), is_simulated=True)

    message = quotation_message(quotation, to_address=CLIENT, in_reply_to=None)

    assert INTERNAL_SUBJECT_PREFIX not in message.subject
    assert message.to_address == CLIENT


def test_the_client_message_threads_onto_the_conversation() -> None:
    quotation = build_quotation(packet_for(), approval(), is_simulated=True)

    message = quotation_message(quotation, to_address=CLIENT, in_reply_to="<enq-1@c.example>")

    assert message.in_reply_to == "<enq-1@c.example>"


# --- what the approver sees -----------------------------------------------------


def test_the_review_shows_every_exclusion_and_its_reason() -> None:
    """The maker has to see *why* a carrier is absent; silence there is
    indistinguishable from a bug."""
    packet = packet_for()
    body = compose_review_body(packet, is_simulated=True)

    for excluded in packet.rates.excluded:
        assert excluded.rate.carrier_code in body
        assert excluded.reason.value in body


def test_the_review_shows_the_eligible_rates_that_were_not_chosen() -> None:
    packet = packet_for()
    body = compose_review_body(packet, is_simulated=True)

    assert packet.selection.runners_up, "the fixture should have runners-up"
    for runner_up in packet.selection.runners_up:
        assert runner_up.carrier_name in body


def test_the_review_states_that_nothing_has_been_sent() -> None:
    body = compose_review_body(packet_for(), is_simulated=True)

    assert "has NOT been sent" in body
    assert "no automatic approval" in body


def test_the_review_is_addressed_to_the_approver_and_marked_internal() -> None:
    message = compose_review_request(
        packet_for(), approver_address=APPROVER_MAILBOX, is_simulated=True
    )

    assert message.to_address == APPROVER_MAILBOX
    assert message.subject.startswith(INTERNAL_SUBJECT_PREFIX)


# --- the approval is inseparable from the quotation -----------------------------


def test_a_quotation_cannot_be_built_without_an_approval() -> None:
    """`Quotation.approved` is a required field of type `Approved`. This is the
    structural half of the guarantee — the type check is the other half."""
    with pytest.raises(TypeError):
        build_quotation(packet_for(), is_simulated=True)  # type: ignore[call-arg]


def test_the_quotation_carries_the_approval_that_authorised_it() -> None:
    quotation = build_quotation(packet_for(), approval(), is_simulated=True)

    assert quotation.approved.by == APPROVER
    assert quotation.selection == packet_for().selection
