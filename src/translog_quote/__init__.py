"""Translog cargo quotation automation — demonstration / proof of concept.

Layering (see docs/architecture.md §4). Dependencies point downward only:

    interface  -> pipeline -> domain -> ports
    adapters   -> ports    (implements)
    bootstrap  -> everything (the only module naming a concrete adapter)

`domain` and `ports` import nothing from the layers above them. That rule is
enforced by tests/architecture/test_layering.py.
"""

__version__ = "0.1.0"
