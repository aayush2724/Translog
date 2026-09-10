"""interface.api — the asynchronous rate-search HTTP service.

Submit a search, get a job id, poll for the outcome. No browser lives here.
"""

from translog_quote.interface.api.app import JobAccepted, create_app

__all__ = ["JobAccepted", "create_app"]
