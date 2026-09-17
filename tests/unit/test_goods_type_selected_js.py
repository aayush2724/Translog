"""The committed-Goods-Type read (`_JS_GOODS_TYPE_SELECTED`), driven as JavaScript.

The read is a JavaScript string evaluated in the browser, so the Python
`FakeDriver` (which returns a hand-written value) never exercises its selectors.
The live WebCargo control is AntD v3, which renders a committed value in
`.ant-select-selection-selected-value`; reading only the AntD v4/v5 class
`.ant-select-selection-item` returned "" for a real selection, so a confirmed
pick read back as "the placeholder" and the search refused to run. Catching that
needs the real read actually run, which is why this suite is JavaScript under
node rather than assertions about the source text.
`tests/js/goods_type_selected.test.js` extracts the read from ``pages.py`` and
evaluates it against synthetic AntD v3 / v4 / empty controls.

Skipped where node is unavailable — a real, named gap: on such a machine the
v3/v4 selector behaviour is unverified by this suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "js" / "goods_type_selected.test.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; the read suite cannot run"
)


def test_the_harness_is_present() -> None:
    """A missing harness must fail loudly rather than silently skip everything."""
    assert HARNESS.is_file(), f"no JavaScript test harness at {HARNESS}"


@requires_node
def test_the_committed_goods_type_read_supports_antd_v3_and_v4() -> None:
    """Runs the real _JS_GOODS_TYPE_SELECTED against a v3 control (value in
    `.ant-select-selection-selected-value`, e.g. 'General Cargo'), a v4 control
    (`.ant-select-selection-item`, fallback), and empty/missing controls."""
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted local harness
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"read suite failed:\n{result.stdout}\n{result.stderr}"
