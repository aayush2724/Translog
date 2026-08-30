"""Entry point: ``python -m translog_quote.interface.web [--host H] [--port P]``."""

from __future__ import annotations

import argparse

from translog_quote.config import ENV_FILE_VAR, load_settings
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
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help=(
            "layer this env file over .env — how a second Gmail account is selected "
            f"without editing the first one (or set {ENV_FILE_VAR})"
        ),
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except FileNotFoundError as exc:
        # Named a file that is not there. Reported here rather than served as a
        # demo silently running against whatever .env points at.
        print(exc)
        return 2

    return run(host=args.host, port=args.port, settings=settings, live=args.live)


if __name__ == "__main__":
    raise SystemExit(main())
