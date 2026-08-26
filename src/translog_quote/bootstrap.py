"""Composition root — the only module permitted to name a concrete adapter.

Read this file to learn how the system is assembled. That it is the single
exception to the dependency rule is what makes the rest of the rule enforceable:
without it, "swap the adapter" degrades into a flag check somewhere in the middle
of the pipeline.

Wiring lands as adapters do. At Phase 1 this module exists to hold the boundary,
and to make the layering test's exemption explicit rather than implied.
"""

from __future__ import annotations

from translog_quote.config import Settings, load_settings

__all__ = ["load_settings", "Settings"]
