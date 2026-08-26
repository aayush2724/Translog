"""Fixture email loading and thread grouping (Phase 3, Part 6).

Each test is named for the numbered requirement it satisfies. Nothing here
touches extraction, validation, WebCargo, or Qwen — this is the raw input layer
only, exercised on its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from translog_quote.adapters.email import (
    FixtureEmailSource,
    load_all_scenarios,
    load_scenario,
    parse_fixture_email,
)
from translog_quote.domain.email import RawEmail

FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "emails"

SCENARIO_NAMES = (
    "a_complete_request",
    "b_incomplete_then_clarified",
    "d_conflicting_reply",
    "e_chemical_shipment",
    "f_door_delivery",
)


@pytest.fixture(params=SCENARIO_NAMES)
def scenario_name(request: pytest.FixtureRequest) -> str:
    return request.param  # type: ignore[no-any-return]


# --- 6. fixtures can be loaded without external services --------------------


def test_6_fixtures_directory_exists() -> None:
    assert FIXTURES_ROOT.is_dir()


def test_6_every_scenario_loads_from_disk_alone(scenario_name: str) -> None:
    """No network, no model, no WebCargo — just files on disk."""
    scenario = load_scenario(scenario_name, FIXTURES_ROOT)

    assert scenario.messages, f"{scenario_name} loaded no messages"
    assert all(isinstance(m, RawEmail) for m in scenario.messages)


def test_6_fixture_email_source_implements_the_email_source_shape() -> None:
    """Structural conformance to `ports.EmailSource`: a `fetch_new()` that
    returns a tuple of `RawEmail`. `EmailSource` is a plain (non-runtime-
    checkable) Protocol, so conformance is demonstrated by calling it, not by
    `isinstance`.
    """
    source = FixtureEmailSource(FIXTURES_ROOT / "a_complete_request")

    result = source.fetch_new()

    assert isinstance(result, tuple)
    assert all(isinstance(m, RawEmail) for m in result)


def test_6_fetch_new_returns_the_full_set_on_every_call() -> None:
    """No polling state: calling it twice returns the same messages both times.

    Deciding what has already been processed is the pipeline's job, not this
    adapter's.
    """
    source = FixtureEmailSource(FIXTURES_ROOT / "b_incomplete_then_clarified")

    first = source.fetch_new()
    second = source.fetch_new()

    assert first == second
    assert len(first) == 2


# --- 8. fixture data satisfies the RawEmail contract -------------------------


def test_8_every_message_satisfies_the_rawemail_contract(scenario_name: str) -> None:
    """Construction through the real, frozen, extra='forbid' RawEmail model —
    if a fixture drifted from the contract, this raises rather than silently
    accepting a malformed value."""
    scenario = load_scenario(scenario_name, FIXTURES_ROOT)

    for message in scenario.messages:
        assert isinstance(message, RawEmail)
        assert message.message_id
        assert message.from_address
        assert message.subject
        assert message.body_text.strip()


def test_8_unknown_header_is_read_and_dropped_not_rejected() -> None:
    """`To:` appears in every fixture for realism; RawEmail has no field for
    it, and that must not make parsing fail."""
    text = (
        "Message-Id: <x1@example.example>\n"
        "From: Someone <someone@example.example>\n"
        "To: quotes@translogexpress.example\n"
        "Subject: Test\n"
        "Date: 2026-09-01T10:00:00+05:30\n"
        "\n"
        "Body text.\n"
    )
    email = parse_fixture_email(text)

    assert email.from_address == "Someone <someone@example.example>"
    assert not hasattr(email, "to_address")


def test_8_malformed_fixture_raises_rather_than_silently_accepted() -> None:
    with pytest.raises(ValueError, match="missing required header"):
        parse_fixture_email("From: someone@example.example\n\nBody with no subject or id.\n")


def test_8_a_line_with_no_colon_is_rejected() -> None:
    text = "Message-Id: <x1@example.example>\nnot a header line\n\nBody.\n"
    with pytest.raises(ValueError, match="malformed fixture header"):
        parse_fixture_email(text)


# --- 1. every fixture has a unique message_id --------------------------------


def test_1_every_message_id_is_globally_unique() -> None:
    scenarios = load_all_scenarios(FIXTURES_ROOT)
    all_ids = [m.message_id for s in scenarios for m in s.messages]

    assert len(all_ids) == len(set(all_ids))
    assert len(all_ids) >= 7  # 5 scenarios, two of which have two messages


# --- 2. messages belonging to one conversation share the correct thread -----


def test_2_every_message_in_a_scenario_shares_that_scenarios_thread(
    scenario_name: str,
) -> None:
    scenario = load_scenario(scenario_name, FIXTURES_ROOT)
    thread = scenario.thread

    assert thread.request_id == scenario.request_id
    assert thread.message_ids == tuple(m.message_id for m in scenario.messages)


def test_2_different_scenarios_have_different_thread_identities() -> None:
    scenarios = load_all_scenarios(FIXTURES_ROOT)
    request_ids = [s.thread.request_id for s in scenarios]

    assert len(request_ids) == len(set(request_ids))


# --- 3. message ordering is deterministic ------------------------------------


def test_3_loading_a_scenario_twice_yields_identical_order() -> None:
    first = load_scenario("b_incomplete_then_clarified", FIXTURES_ROOT)
    second = load_scenario("b_incomplete_then_clarified", FIXTURES_ROOT)

    assert first.messages == second.messages
    assert first.thread.message_ids == second.thread.message_ids


def test_3_ordering_follows_filename_not_declaration_order() -> None:
    scenario = load_scenario("b_incomplete_then_clarified", FIXTURES_ROOT)

    assert [m.message_id for m in scenario.messages] == [
        "<b1@oceanictraders.example>",
        "<b2@oceanictraders.example>",
    ]


def test_3_ordering_reflects_chronological_arrival() -> None:
    scenario = load_scenario("d_conflicting_reply", FIXTURES_ROOT)
    timestamps = [m.received_at for m in scenario.messages]

    assert timestamps == sorted(timestamps)


# --- 4. the incomplete request and clarification reply share a thread -------


def test_4_scenario_b_and_c_are_two_messages_in_one_thread() -> None:
    scenario = load_scenario("b_incomplete_then_clarified", FIXTURES_ROOT)
    initial, reply = scenario.messages

    assert reply.in_reply_to == initial.message_id
    assert initial.message_id in reply.references
    # Both are visible under the one thread this scenario represents.
    assert set(scenario.thread.message_ids) == {initial.message_id, reply.message_id}


def test_4_the_reply_actually_supplies_what_the_initial_email_left_out() -> None:
    """Ties this fixture to Phase 2's merge behaviour: these are the exact
    values `test_shipment_merge.py` and `test_reference_scenarios.py` use."""
    scenario = load_scenario("b_incomplete_then_clarified", FIXTURES_ROOT)
    initial, reply = scenario.messages

    assert "commodity" not in initial.body_text.lower()
    assert "polyisobutylene additive" in reply.body_text.lower()
    assert "20" in reply.body_text
    assert "door" in reply.body_text.lower()


# --- 5. the conflicting reply shares a thread with its original email -------


def test_5_scenario_d_reply_belongs_to_the_same_thread_as_its_original() -> None:
    scenario = load_scenario("d_conflicting_reply", FIXTURES_ROOT)
    initial, reply = scenario.messages

    assert reply.in_reply_to == initial.message_id
    assert reply.references == (initial.message_id,)
    assert scenario.thread.request_id == "R-DEMO-D"


def test_5_the_conflict_is_present_in_the_fixture_text_unresolved() -> None:
    scenario = load_scenario("d_conflicting_reply", FIXTURES_ROOT)
    initial, reply = scenario.messages

    assert "500" in initial.body_text
    assert "700" in reply.body_text
    # The fixture states the correction; it does not decide which value wins —
    # that is Phase 2's merge_shipment, exercised elsewhere, not this layer.


# --- 7. fixtures do not contain secrets --------------------------------------


_SECRET_MARKERS = ("password", "secret", "api_key", "api-key", "apikey", "token", "bearer ")


def test_7_no_fixture_file_contains_a_secret_marker() -> None:
    for path in sorted(FIXTURES_ROOT.glob("**/*.eml")):
        lowered = path.read_text(encoding="utf-8").lower()
        for marker in _SECRET_MARKERS:
            assert marker not in lowered, f"{path} contains a secret-like marker: {marker!r}"


def test_7_every_address_uses_the_reserved_example_domain() -> None:
    for path in sorted(FIXTURES_ROOT.glob("**/*.eml")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.lower().startswith(("from:", "to:")):
                assert ".example" in line, f"{path}: non-.example address in {line!r}"


# --- Unknown scenario name -----------------------------------------------


def test_loading_an_unknown_scenario_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown fixture scenario"):
        load_scenario("does_not_exist", FIXTURES_ROOT)


def test_load_all_scenarios_returns_all_five_in_a_fixed_order() -> None:
    scenarios = load_all_scenarios(FIXTURES_ROOT)

    assert [s.name for s in scenarios] == sorted(SCENARIO_NAMES)


# --- Directly against RawEmail's own contract (extra="forbid") --------------


def test_parsed_email_rejects_further_mutation() -> None:
    """RawEmail is frozen; a fixture-loaded instance is no exception."""
    scenario = load_scenario("a_complete_request", FIXTURES_ROOT)
    message = scenario.messages[0]

    with pytest.raises(ValidationError):
        message.subject = "changed"  # type: ignore[misc]
