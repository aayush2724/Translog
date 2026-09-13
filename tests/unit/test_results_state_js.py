"""The WebCargo results-settle predicate, driven as JavaScript.

`_JS_RESULTS_STATE` is a JavaScript string evaluated in the browser, so the
Python fakes (which return a hand-written state dict) never exercise its
regexes. A zero-rate search shows empty-state wording the predicate must
recognise as empty — otherwise the extractor waits the full timeout and fails
instead of returning a valid empty outcome. Catching that needs the real
predicate actually run, which is why this suite is JavaScript under node rather
than assertions about the source text. `tests/js/results_state.test.js` extracts
the predicate from ``pages.py`` and evaluates it against synthetic pages.

Skipped where node is unavailable — a real, named gap: on such a machine the
empty-state classification is unverified by this suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "js" / "results_state.test.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; the predicate suite cannot run"
)


def test_the_harness_is_present() -> None:
    """A missing harness must fail loudly rather than silently skip everything."""
    assert HARNESS.is_file(), f"no JavaScript test harness at {HARNESS}"


@requires_node
def test_the_results_state_predicate_classifies_every_case() -> None:
    """Runs the real _JS_RESULTS_STATE against empty, rates-present and loading
    pages — including WebCargo's live-observed empty-state wording."""
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted local harness
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"predicate suite failed:\n{result.stdout}\n{result.stderr}"
