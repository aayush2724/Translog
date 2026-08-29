"""JsonFileStore — a durable StorePort, so a demo survives a process exit.

The demo's flow is genuinely multi-session: a clarification goes out, the
process ends, and the client replies hours later. Nothing about that works if
the request's state dies with the process — the next run would re-extract the
enquiry, re-draft the clarification, and mail the client a second copy of a
question they have already answered.

Two files, each a JSON object keyed by request id:

    <state_dir>/requests.json   request_id -> QuotationRequest
    <state_dir>/threads.json    request_id -> Thread

Two flat files rather than a file per request, because request ids would then
reach the filesystem as path components and every one of them would need to be
proved safe. A key in a JSON object cannot escape its directory.

Writes are atomic — a temporary file in the same directory, then `os.replace`.
A run interrupted mid-write leaves the previous state intact rather than a
half-written record of what was sent, which is the one thing worse than losing
the state entirely.

This is still demo-grade persistence and says so: no locking, so two
concurrent runs against one directory would race, and no migration path if the
stored shapes change. It is a file, not a database. What it does guarantee is
the property the demo needs — that what a person authorised in one process is
still true in the next.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import TYPE_CHECKING, Any

from translog_quote.domain.conversation import Thread
from translog_quote.domain.workflow import QuotationRequest
from translog_quote.errors import ContractViolation
from translog_quote.observability import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_log = get_logger("adapters.store.json_file")

REQUESTS_FILE = "requests.json"
THREADS_FILE = "threads.json"


def _read_object(path: Path) -> dict[str, Any]:
    """One state file as a dict. A missing file is an empty store, not an error.

    A file that exists but cannot be parsed *is* an error: silently treating
    corrupted state as "nothing has happened yet" would let a second
    quotation go out to a client who already had one.
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractViolation(
            f"The demo state file at {path} could not be read. Delete the state "
            "directory to start a fresh run, but be aware that doing so forgets "
            "what has already been sent."
        ) from exc
    if not isinstance(loaded, dict):
        raise ContractViolation(f"The demo state file at {path} is not a JSON object")
    return loaded


def _write_object(path: Path, payload: dict[str, Any]) -> None:
    """Replace one state file atomically.

    The temporary file is created in the destination directory so the replace
    is a rename within one filesystem, which is what makes it atomic. A run
    interrupted mid-write therefore leaves the previous state intact rather
    than a half-written record of what was sent — which is the one outcome
    worse than losing the state entirely.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        # The temporary file may already be gone if the failure was in the
        # replace itself, so its removal is best-effort.
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


class JsonFileStore:
    """A `StorePort` backed by two JSON files under one directory.

    State is loaded once at construction and written through on every save, so
    a crash between two saves loses at most the save that was in flight.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._requests_path = directory / REQUESTS_FILE
        self._threads_path = directory / THREADS_FILE

        self._requests: dict[str, QuotationRequest] = {
            key: QuotationRequest.model_validate(value)
            for key, value in _read_object(self._requests_path).items()
        }
        self._threads: dict[str, Thread] = {
            key: Thread.model_validate(value)
            for key, value in _read_object(self._threads_path).items()
        }
        if self._requests or self._threads:
            _log.info(
                "Loaded demo state from %s: %d request(s), %d thread(s)",
                directory,
                len(self._requests),
                len(self._threads),
            )

    @property
    def directory(self) -> Path:
        return self._directory

    # ------------------------------------------------------------ StorePort --

    def get_request(self, request_id: str) -> QuotationRequest | None:
        return self._requests.get(request_id)

    def save_request(self, request: QuotationRequest) -> None:
        self._requests[request.request_id] = request
        _write_object(
            self._requests_path,
            {key: value.model_dump(mode="json") for key, value in self._requests.items()},
        )

    def all_threads(self) -> tuple[Thread, ...]:
        return tuple(self._threads[key] for key in sorted(self._threads))

    def save_thread(self, thread: Thread) -> None:
        self._threads[thread.request_id] = thread
        _write_object(
            self._threads_path,
            {key: value.model_dump(mode="json") for key, value in self._threads.items()},
        )
