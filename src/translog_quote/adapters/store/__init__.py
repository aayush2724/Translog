"""adapters.store

Implements StorePort. InMemoryStore is the demo implementation; durable
persistence is later work behind the same port.
"""

from translog_quote.adapters.store.memory import InMemoryStore

__all__ = ["InMemoryStore"]
