"""Entry point: ``python -m translog_quote.interface.web [--host H] [--port P]``."""

from __future__ import annotations

import argparse

from translog_quote.interface.web.server import DEFAULT_HOST, DEFAULT_PORT, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="translog-web-poc", description="Serve the Translog client-facing web POC."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default: local only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "drive the real Gmail workflow instead of the scripted scenario "
            "(requires the inbound and outbound credentials and an approver address)"
        ),
    )
    args = parser.parse_args(argv)
    return run(host=args.host, port=args.port, live=args.live)


if __name__ == "__main__":
    raise SystemExit(main())
