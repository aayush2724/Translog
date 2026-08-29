"""The durable StorePort. Everything here is about what survives, and what fails loudly.

`JsonFileStore` is the only reason the demo can span two processes, so its
contract is the same as `InMemoryStore`'s plus one promise: what was saved is
still there after the object is gone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from translog_quote.adapters.store import (
    REQUESTS_FILE,
    THREADS_FILE,
    InMemoryStore,
    JsonFileStore,
)
from translog_quote.domain.conversation import Thread
from translog_quote.domain.shipment import CargoDimensions, RequestSource, ShipmentRecord
from translog_quote.domain.workflow import QuotationRequest, RequestState
from translog_quote.errors import ContractViolation

RECORD = ShipmentRecord(
    request_id="R-1",
    source=RequestSource.EMAIL,
    origin="Ahmedabad",
    destination="Bahrain",
    weight_kg=500.0,
    dimensions_in=CargoDimensions(length=34, width=24, height=6),
)

REQUEST = QuotationRequest(
    request_id="R-1",
    state=RequestState.CLARIFICATION_SENT,
    record=RECORD,
    client_address="client@example.com",
)


# --- the port contract ----------------------------------------------------------


def test_an_empty_directory_is_an_empty_store(tmp_path: Path) -> None:
    """A first run must not need the directory to exist."""
    store = JsonFileStore(tmp_path / "never-created")

    assert store.get_request("R-1") is None
    assert store.all_threads() == ()


def test_it_behaves_like_the_in_memory_store(tmp_path: Path) -> None:
    """The two implementations are interchangeable behind the port — which is
    what lets the demo run against a scratch copy and commit into this one."""
    durable: JsonFileStore = JsonFileStore(tmp_path)
    memory = InMemoryStore()

    for store in (durable, memory):
        store.save_request(REQUEST)
        store.save_thread(Thread(request_id="R-1", message_ids=("<a>", "<b>")))

    assert durable.get_request("R-1") == memory.get_request("R-1")
    assert durable.all_threads() == memory.all_threads()


def test_threads_iterate_in_a_deterministic_order(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    for request_id in ("R-3", "R-1", "R-2"):
        store.save_thread(Thread(request_id=request_id, message_ids=()))

    assert [t.request_id for t in store.all_threads()] == ["R-1", "R-2", "R-3"]


def test_saving_the_same_request_twice_replaces_it(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    store.save_request(REQUEST)

    store.save_request(REQUEST.model_copy(update={"state": RequestState.QUOTATION_SENT}))

    reloaded = JsonFileStore(tmp_path).get_request("R-1")
    assert reloaded is not None
    assert reloaded.state is RequestState.QUOTATION_SENT


# --- the one promise the in-memory store cannot make ----------------------------


def test_a_saved_request_survives_a_new_store_object(tmp_path: Path) -> None:
    JsonFileStore(tmp_path).save_request(REQUEST)

    reloaded = JsonFileStore(tmp_path).get_request("R-1")

    assert reloaded == REQUEST


def test_the_whole_shipment_record_survives_the_round_trip(tmp_path: Path) -> None:
    """Not just the state. The next run merges a reply into this record, so a
    field lost here is a field the client would be asked for twice."""
    JsonFileStore(tmp_path).save_request(REQUEST)

    reloaded = JsonFileStore(tmp_path).get_request("R-1")

    assert reloaded is not None
    assert reloaded.record.dimensions_in == RECORD.dimensions_in
    assert reloaded.record.weight_kg == 500.0
    assert reloaded.client_address == "client@example.com"


def test_a_saved_thread_survives_a_new_store_object(tmp_path: Path) -> None:
    """Threads are what correlation matches against and what message-skipping
    reads, so losing one would both re-extract and mis-place the next reply."""
    JsonFileStore(tmp_path).save_thread(Thread(request_id="R-1", message_ids=("<a>", "<b>")))

    assert JsonFileStore(tmp_path).all_threads() == (
        Thread(request_id="R-1", message_ids=("<a>", "<b>")),
    )


# --- failure is loud ------------------------------------------------------------


@pytest.mark.parametrize("filename", [REQUESTS_FILE, THREADS_FILE])
def test_an_unreadable_state_file_raises_rather_than_reading_as_empty(
    tmp_path: Path, filename: str
) -> None:
    """Treating corrupted state as "nothing has happened yet" would let a
    second quotation go to a client who already had one. The error names the
    consequence of the obvious fix rather than just suggesting it."""
    (tmp_path / filename).write_text("{ not json", encoding="utf-8")

    with pytest.raises(ContractViolation, match="already been sent"):
        JsonFileStore(tmp_path)


@pytest.mark.parametrize("filename", [REQUESTS_FILE, THREADS_FILE])
def test_a_state_file_that_is_not_an_object_raises(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_text("[]", encoding="utf-8")

    with pytest.raises(ContractViolation, match="not a JSON object"):
        JsonFileStore(tmp_path)


def test_a_state_file_holding_the_wrong_shape_raises(tmp_path: Path) -> None:
    """Validated on load through the same pydantic model that wrote it, so a
    stale file from an older shape fails at startup rather than mid-demo."""
    (tmp_path / REQUESTS_FILE).write_text(json.dumps({"R-1": {"nope": 1}}), encoding="utf-8")

    with pytest.raises(Exception, match="validation error|ContractViolation"):
        JsonFileStore(tmp_path)


# --- writes are atomic ----------------------------------------------------------


def test_a_write_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    store = JsonFileStore(tmp_path)
    store.save_request(REQUEST)
    store.save_thread(Thread(request_id="R-1", message_ids=("<a>",)))

    assert sorted(p.name for p in tmp_path.iterdir()) == [REQUESTS_FILE, THREADS_FILE]


def test_the_state_files_are_human_readable(tmp_path: Path) -> None:
    """A person inspecting the demo between runs should be able to read what
    the system thinks has happened."""
    JsonFileStore(tmp_path).save_request(REQUEST)

    payload = json.loads((tmp_path / REQUESTS_FILE).read_text(encoding="utf-8"))

    assert payload["R-1"]["state"] == "clarification_sent"
    assert payload["R-1"]["record"]["origin"] == "Ahmedabad"
