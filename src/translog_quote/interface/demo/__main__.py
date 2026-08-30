"""Entry point: ``python -m translog_quote.interface.demo [scenario]``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from translog_quote.config import ENV_FILE_VAR
from translog_quote.interface.demo.clarification_demo import run_demo as run_clarification_demo
from translog_quote.interface.demo.extraction_demo import DEFAULT_SCENARIO, run_demo
from translog_quote.interface.demo.gmail_demo import (
    run_gmail_auth,
    run_gmail_auth_send,
    run_gmail_test,
)
from translog_quote.interface.demo.gmail_process import run_gmail_process
from translog_quote.interface.demo.gmail_quote import run_gmail_quote
from translog_quote.interface.demo.gmail_thread import run_gmail_thread
from translog_quote.interface.demo.poc_demo import run_demo as run_poc_demo
from translog_quote.interface.demo.rate_demo import run_demo as run_rate_demo
from translog_quote.interface.demo.reset_state import run_reset_state


def _apply_env_file(args: list[str]) -> list[str] | None:
    """Read `--env-file <path>`; return the remaining arguments, or None to stop.

    Set in the environment rather than threaded through each command, because
    every one of them already resolves its own `load_settings()` and the flag
    must mean the same thing to all of them — including `gmail-auth`, where
    picking the wrong file would write a token over another account's.

    The flag and its value are removed from the argument list, so it can be
    written before the subcommand (`--env-file X gmail-auth`) where a reader
    naturally puts it, and the subcommand is still the first word left.

    None means the flag named a file that is not there: the caller stops rather
    than running against whatever `.env` happens to say.
    """
    if "--env-file" not in args:
        return args
    position = args.index("--env-file")
    value = args[position + 1] if position + 1 < len(args) else ""
    if not value or not Path(value).is_file():
        print(f"No configuration file at {value!r}. --env-file must name an existing env file.")
        return None
    os.environ[ENV_FILE_VAR] = value
    return args[:position] + args[position + 2 :]


def _approved_by(args: list[str]) -> str | None:
    """Read `--approved-by <name>`. Absent means nobody has approved anything,
    which is the safe default: the run then stops at the gate."""
    if "--approved-by" not in args:
        return None
    position = args.index("--approved-by") + 1
    return args[position] if position < len(args) else None


def main(argv: list[str] | None = None) -> int:
    """``python -m translog_quote.interface.demo
    [poc|scenario|clarification|rates|gmail-test|gmail-process|gmail-thread
     |gmail-quote|gmail-auth|gmail-auth-send|reset-state]``."""
    remaining = _apply_env_file(sys.argv[1:] if argv is None else argv)
    if remaining is None:
        return 2
    args = remaining
    if args and args[0] == "clarification":
        return run_clarification_demo()
    if args and args[0] == "rates":
        return run_rate_demo()
    if args and args[0] == "gmail-test":
        return run_gmail_test()
    if args and args[0] == "gmail-process":
        return run_gmail_process()
    if args and args[0] == "gmail-thread":
        return run_gmail_thread(approved_by=_approved_by(args))
    if args and args[0] == "gmail-quote":
        return run_gmail_quote(approved_by=_approved_by(args))
    if args and args[0] == "gmail-auth":
        return run_gmail_auth()
    if args and args[0] == "gmail-auth-send":
        return run_gmail_auth_send()
    if args and args[0] == "reset-state":
        return run_reset_state(confirmed="--yes" in args)
    if args and args[0] in {"poc", "full"}:
        return run_poc_demo()
    scenario = args[0] if args else DEFAULT_SCENARIO
    return run_demo(scenario=scenario)


if __name__ == "__main__":
    raise SystemExit(main())
