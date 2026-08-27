"""The dependency rule, enforced (docs/architecture.md §4 and §7).

Dependencies point downward and inward only. `domain` and `ports` import nothing
from the layers above them, and exactly one module — `bootstrap` — may name a
concrete adapter.

This is a stdlib AST walk rather than a third-party import linter: it gives the
same guarantee, runs inside the normal test command, and costs no dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = "translog_quote"
SRC = Path(__file__).resolve().parents[2] / "src" / PACKAGE

# Which top-level areas each area may import from.
ALLOWED: dict[str, set[str]] = {
    "domain": {"domain", "ports"},
    "ports": {"domain", "ports"},
    "pipeline": {"domain", "ports", "pipeline", "errors"},
    "adapters": {"domain", "ports", "adapters", "errors", "config", "observability"},
    "config": {"config"},
    "observability": {"observability"},
    "errors": {"errors"},
    "interface": {
        "domain",
        "ports",
        "pipeline",
        "config",
        "observability",
        "errors",
        "interface",
        "bootstrap",
    },
    # Evaluation tooling. An entry-point area like `interface`: it drives the
    # real system, and reaches adapters only through the composition root.
    "evaluation": {
        "domain",
        "ports",
        "config",
        "observability",
        "errors",
        "evaluation",
        "bootstrap",
    },
    # The package root re-exports nothing and must stay import-free.
    "__init__": set(),
    # The composition root is the single exception: it wires everything.
    "bootstrap": {
        "domain",
        "ports",
        "pipeline",
        "adapters",
        "config",
        "observability",
        "errors",
    },
}


def _area(path: Path) -> str:
    """The top-level area a source file belongs to."""
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def _internal_imports(path: Path) -> set[str]:
    """Areas of `translog_quote` that this file imports from."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    own_area = _area(path)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{PACKAGE}."):
                    found.add(alias.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — resolve against this file's area
                found.add(own_area)
            elif node.module and node.module.startswith(f"{PACKAGE}."):
                found.add(node.module.split(".")[1])
            elif node.module == PACKAGE:
                for alias in node.names:
                    found.add(alias.name)

    return {a for a in found if a in ALLOWED}


SOURCE_FILES = sorted(SRC.rglob("*.py"))


def test_source_files_were_found() -> None:
    assert SOURCE_FILES, f"no source files under {SRC}"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.name))
def test_file_respects_layering(path: Path) -> None:
    area = _area(path)
    assert area in ALLOWED, f"{path} sits in an area with no declared import rule"

    permitted = ALLOWED[area]
    violations = {imported for imported in _internal_imports(path) if imported not in permitted}

    assert not violations, (
        f"{path.relative_to(SRC.parent.parent)} is in '{area}' and imports "
        f"{sorted(violations)}, which it may not. "
        f"'{area}' may import: {sorted(permitted)}."
    )


def test_only_bootstrap_may_import_adapters() -> None:
    """The rule that keeps the mock strategy honest."""
    offenders = [
        str(p.relative_to(SRC))
        for p in SOURCE_FILES
        if _area(p) not in {"adapters", "bootstrap"} and "adapters" in _internal_imports(p)
    ]
    assert not offenders, (
        f"only bootstrap may name a concrete adapter; these import adapters: {offenders}"
    )


def test_domain_is_pure() -> None:
    """Domain imports no infrastructure — not even configuration or logging."""
    forbidden = {"adapters", "pipeline", "interface", "config", "observability", "bootstrap"}
    offenders = [
        (str(p.relative_to(SRC)), sorted(_internal_imports(p) & forbidden))
        for p in SOURCE_FILES
        if _area(p) == "domain" and _internal_imports(p) & forbidden
    ]
    assert not offenders, f"domain must stay pure; found: {offenders}"
