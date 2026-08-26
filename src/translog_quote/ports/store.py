"""Persistence, as a dependency.

The demo stores requests in memory and writes JSON snapshots so a run leaves an
inspectable trail. Durable persistence is a later adapter behind this same port.
"""

from __future__ import annotations

from typing import Protocol

from translog_quote.domain.conversation import Thread
from translog_quote.domain.workflow import QuotationRequest


class StorePort(Protocol):
    def get_request(self, request_id: str) -> QuotationRequest | None: ...

    def save_request(self, request: QuotationRequest) -> None: ...

    def all_threads(self) -> tuple[Thread, ...]: ...

    def save_thread(self, thread: Thread) -> None: ...
