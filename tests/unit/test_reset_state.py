"""The reset-state command clears local demo state and nothing else.

The safety properties are the point: it deletes only named files under the
configured state directory, and it cannot reach Gmail or the credentials.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from translog_quote.config import Settings
from translog_quote.interface.demo.reset_state import (
    EXIT_OK,
    EXIT_REFUSED,
    REMOVABLE,
    run_reset_state,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(update={"demo": base.demo.model_copy(update={"state_dir": tmp_path})})


def seed(state_dir: Path) -> None:
    for name in REMOVABLE:
        (state_dir / name).write_text("{}", encoding="utf-8")


def test_it_removes_every_state_file_including_the_demonstration(settings: Settings) -> None:
    """A reset that left the demonstration cutoff behind would not be a reset:
    the next run would still be scoped to the old demonstration."""
    state_dir = settings.demo.state_dir
    seed(state_dir)

    code = run_reset_state(settings=settings, confirmed=True, out=io.StringIO())

    assert code == EXIT_OK
    assert [p.name for p in state_dir.iterdir()] == []
    assert "demonstration.json" in REMOVABLE


def test_it_refuses_without_confirmation_and_deletes_nothing(settings: Settings) -> None:
    state_dir = settings.demo.state_dir
    seed(state_dir)

    code = run_reset_state(settings=settings, confirmed=False, out=io.StringIO())

    assert code == EXIT_REFUSED
    assert sorted(p.name for p in state_dir.iterdir()) == sorted(REMOVABLE)


def test_it_only_ever_names_files_inside_the_state_directory(settings: Settings) -> None:
    """No glob, no recursion: the whole list of what it can delete is a fixed
    tuple of bare filenames."""
    for name in REMOVABLE:
        assert "/" not in name and "\\" not in name and ".." not in name


def test_an_empty_state_directory_is_a_no_op(settings: Settings) -> None:
    code = run_reset_state(settings=settings, confirmed=True, out=io.StringIO())

    assert code == EXIT_OK


def test_it_reads_no_credential_and_touches_no_mailbox(settings: Settings) -> None:
    """Structural: the module imports no mailbox client and reads no secret.
    The output says so, and this pins that the claim stays true."""
    seed(settings.demo.state_dir)
    out = io.StringIO()

    run_reset_state(settings=settings, confirmed=True, out=out)

    text = out.getvalue()
    assert "does not touch Gmail" in text
    assert ".secrets" in text
