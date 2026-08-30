"""Entry point: ``python -m translog_quote.interface.web [--host H] [--port P]``."""

from __future__ import annotations

import argparse
import os

from translog_quote.config import ENV_FILE_VAR, load_settings
from translog_quote.interface.web.server import DEFAULT_HOST, DEFAULT_PORT, run

#: A platform that assigns the port tells the process which one through the
#: environment — Render, Heroku and Cloud Run all use PORT. Read as a *default*
#: for the flag rather than instead of it, so an explicit --port still wins and
#: a local run with neither set behaves exactly as it always has.
PORT_VAR = "PORT"
HOST_VAR = "HOST"


def _env_port() -> int:
    """The port the platform assigned, or the local default.

    A malformed value falls back rather than crashing the process: on a host
    that restarts a failed boot, an unparseable PORT would loop forever with
    the reason buried in a traceback.
    """
    raw = os.environ.get(PORT_VAR, "").strip()
    if not raw.isdigit():
        return DEFAULT_PORT
    return int(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="translog-web-poc", description="Serve the Translog client-facing web POC."
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(HOST_VAR) or DEFAULT_HOST,
        help=f"bind address (default: local only; or set {HOST_VAR})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_port(),
        help=f"port to bind (default: {DEFAULT_PORT}; or set {PORT_VAR})",
    )
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
