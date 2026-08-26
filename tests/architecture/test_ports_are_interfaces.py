"""Ports declare contracts and nothing else."""

from __future__ import annotations

import inspect

import pytest

from translog_quote import ports

PORT_NAMES = sorted(ports.__all__)


def test_every_port_is_exported() -> None:
    assert PORT_NAMES, "ports package exports nothing"


@pytest.mark.parametrize("name", PORT_NAMES)
def test_port_is_a_protocol(name: str) -> None:
    """A port is an interface, never a base class with behaviour."""
    port = getattr(ports, name)
    assert inspect.isclass(port)
    assert getattr(port, "_is_protocol", False), f"{name} must be a typing.Protocol"
