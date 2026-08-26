"""Configuration — loaded once, injected as values.

Domain modules receive a SelectionStrategy, not a settings object. No business
rule can be reached by importing a singleton, and every rule is visible in a
function signature.

Secrets come from the environment and are never committed. `.env.example`
documents the names.
"""

from translog_quote.config.settings import (
    Environment,
    LogLevel,
    Settings,
    WebCargoMode,
    load_settings,
)

__all__ = ["Environment", "LogLevel", "Settings", "WebCargoMode", "load_settings"]
