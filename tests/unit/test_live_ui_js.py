"""The live view's decision controls, driven as JavaScript.

The bug these exist for was invisible to any source-level check: the approve
button's `disabled` attribute was computed once at render time, and the
approver field updated the model without recomputing it — so a typed name
never enabled anything and the button stayed disabled forever, with a healthy
backend sitting behind it.

Catching that needs the real code, actually rendered and actually typed into,
which is why this suite is JavaScript run under node rather than assertions
about the source text. `tests/js/live_ui.test.js` builds a minimal DOM, loads
the shipped `live.js`, and drives the controls.

Skipped where node is unavailable. That is a real gap and worth naming: on a
machine without node these controls are unverified, and the Python suite around
them proves the *server* would accept the approval, not that the button can be
clicked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "js" / "live_ui.test.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; the live-UI suite cannot run"
)


def test_the_harness_is_present() -> None:
    """A missing harness must fail loudly rather than silently skip everything."""
    assert HARNESS.is_file(), f"no JavaScript test harness at {HARNESS}"


@requires_node
def test_the_live_ui_controls_behave() -> None:
    """Run the JavaScript suite; its output is the failure message on error."""
    result = subprocess.run(  # noqa: S603 - a fixed path, no shell, no user input
        [shutil.which("node") or "node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"\n{result.stdout}\n{result.stderr}"


@requires_node
def test_every_case_in_the_harness_actually_ran() -> None:
    """A harness that silently stopped reporting would pass by exiting zero."""
    result = subprocess.run(  # noqa: S603 - a fixed path, no shell, no user input
        [shutil.which("node") or "node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.stdout.count("  ok   ") >= 13, result.stdout
