"""`python -m translog_quote.interface.api` — serve the rate-search API."""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m translog_quote.interface.api",
        description="The Translog asynchronous rate-search API service.",
    )
    parser.add_argument(
        "--host",
        # The HOST convention the demo web server already established:
        # loopback unless the deployment says otherwise.
        default=os.environ.get("HOST", "127.0.0.1"),
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument(
        "--env-file",
        default=None,
        help="layer this env file over .env (same semantics as the other commands)",
    )
    args = parser.parse_args(argv)

    from translog_quote.config import load_settings
    from translog_quote.interface.api.app import run

    run(load_settings(args.env_file), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
