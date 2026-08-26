"""Logging setup.

Two distinct streams. This module owns the *application log* — developer
diagnostics, freeform, discardable. The *audit trail* is a different thing
entirely and lives in `pipeline.audit`.

Never log a secret, a credential or a raw API key.
"""

from translog_quote.observability.logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
