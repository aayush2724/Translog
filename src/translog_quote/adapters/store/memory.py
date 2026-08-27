"""In-memory StorePort. Enough for a demo, and honest about being no more.

No file, no database, no durability. A process restart loses everything, which
is correct for a demonstration and would be a defect anywhere else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from translog_quote.domain.conversation import Thread
    from translog_quote.domain.workflow import QuotationRequest


class InMemoryStore:
    """A dict behind the port. Deterministic iteration order."""

    def __init__(self) -> None:
        self._requests: dict[str, QuotationRequest] = {}
        self._threads: dict[str, Thread] = {}

    def get_request(self, request_id: str) -> QuotationRequest | None:
        return self._requests.get(request_id)

    def save_request(self, request: QuotationRequest) -> None:
        self._requests[request.request_id] = request

    def all_threads(self) -> tuple[Thread, ...]:
        return tuple(self._threads[k] for k in sorted(self._threads))

    def save_thread(self, thread: Thread) -> None:
        self._threads[thread.request_id] = thread
