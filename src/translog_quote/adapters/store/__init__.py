"""adapters.store

Implements StorePort twice. `InMemoryStore` is the default and keeps nothing
past the process. `JsonFileStore` writes two JSON files under a git-ignored
directory, so a demonstration that spans several CLI invocations — a
clarification today, the client's reply tomorrow — remembers what it already
sent.
"""

from translog_quote.adapters.store.json_file import (
    REQUESTS_FILE,
    THREADS_FILE,
    JsonFileStore,
)
from translog_quote.adapters.store.memory import InMemoryStore

__all__ = ["REQUESTS_FILE", "THREADS_FILE", "InMemoryStore", "JsonFileStore"]
