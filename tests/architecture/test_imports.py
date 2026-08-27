"""Smoke tests: everything imports, and configuration loads with nothing set.

These prove the skeleton is wired, not that it does anything. That distinction is
the point of Phase 1.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

PACKAGE_MODULES = [
    "translog_quote",
    "translog_quote.bootstrap",
    # domain (L2)
    "translog_quote.domain",
    "translog_quote.domain.shipment",
    "translog_quote.domain.email",
    "translog_quote.domain.conversation",
    "translog_quote.domain.validation",
    "translog_quote.domain.clarification",
    "translog_quote.domain.rates",
    "translog_quote.domain.quotation",
    "translog_quote.domain.decision",
    "translog_quote.domain.workflow",
    # ports (L1)
    "translog_quote.ports",
    "translog_quote.ports.email",
    "translog_quote.ports.extraction",
    "translog_quote.ports.rates",
    "translog_quote.ports.approval",
    "translog_quote.ports.clock",
    "translog_quote.ports.store",
    # pipeline (L3)
    "translog_quote.pipeline",
    "translog_quote.pipeline.state_machine",
    "translog_quote.pipeline.audit",
    "translog_quote.pipeline.stages",
    # adapters (L0) — packages exist; implementations do not
    "translog_quote.adapters",
    "translog_quote.adapters.email",
    "translog_quote.adapters.extraction",
    "translog_quote.adapters.webcargo",
    "translog_quote.adapters.clock",
    "translog_quote.adapters.store",
    "translog_quote.adapters.approval",
    # infrastructure
    "translog_quote.config",
    "translog_quote.observability",
    "translog_quote.errors",
    # interface (L4)
    "translog_quote.interface",
    "translog_quote.interface.demo",
    "translog_quote.interface.cli",
]


@pytest.mark.parametrize("module_name", PACKAGE_MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None


def test_package_exposes_version() -> None:
    import translog_quote

    assert translog_quote.__version__


def test_settings_load_without_any_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuration must load safely with nothing configured and no .env file."""
    for key in list(__import__("os").environ):
        if key.startswith("TRANSLOG_"):
            monkeypatch.delenv(key, raising=False)

    from translog_quote.config import Environment, Settings, WebCargoMode

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.environment is Environment.DEMO
    assert settings.webcargo.mode is WebCargoMode.MOCK
    assert settings.demo.deterministic is True


def test_no_secret_has_a_default_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """No credential is hardcoded anywhere in the settings tree."""
    for key in list(__import__("os").environ):
        if key.startswith("TRANSLOG_"):
            monkeypatch.delenv(key, raising=False)

    from translog_quote.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.openrouter.api_key is None
    assert settings.webcargo.username is None
    assert settings.webcargo.password is None


def test_openrouter_model_is_the_verified_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """AMB-2, resolved in Phase 5.

    Pinned as a test because the OpenRouter catalogue also carries
    `qwen3.7-plus`, `qwen3.7-max` and `qwen3.6-flash`. A well-meaning edit to any
    of those would change which model quotes cargo, and would otherwise be
    invisible.
    """
    for key in list(__import__("os").environ):
        if key.startswith("TRANSLOG_"):
            monkeypatch.delenv(key, raising=False)

    from translog_quote.config import Settings

    assert Settings(_env_file=None).openrouter.model == "qwen/qwen3.7-flash"  # type: ignore[call-arg]


# --- secret hygiene -----------------------------------------------------------


def test_no_file_contains_a_provider_shaped_credential() -> None:
    """No literal in this repository may look like a real provider API key.

    Added after a secret scanner raised an incident on a *fake* key in a test
    fixture: the placeholder carried the provider's real prefix, so it matched
    the detector's pattern even though the payload spelled "NOTREAL".

    A test credential gains nothing from imitating the real format, and the cost
    of it doing so is a false-positive security incident on every scan — which
    trains people to wave alerts through. The prefixes below are assembled from
    fragments so that this guard does not trip itself.
    """
    prefixes = [
        "sk" + "-or-" + "v1-",  # OpenRouter
        "sk" + "-proj-",  # OpenAI project keys
        "gh" + "p_",  # GitHub personal access tokens
        "AKI" + "A",  # AWS access key ids
        "xox" + "b-",  # Slack bot tokens
    ]
    pattern = re.compile("(" + "|".join(re.escape(p) for p in prefixes) + r")[A-Za-z0-9_-]{6,}")

    root = Path(__file__).resolve().parents[2]
    skip = {
        ".venv",
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "evaluation",
    }

    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md", ".toml", ".json", ".example"}:
            continue
        if any(part in skip for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):  # pragma: no cover - binary or unreadable
            continue
        if pattern.search(text):
            offenders.append(str(path.relative_to(root)))

    assert not offenders, (
        "provider-shaped credential literal found (use an obviously-fake "
        f"placeholder instead): {offenders}"
    )
