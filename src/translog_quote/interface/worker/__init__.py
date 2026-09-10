"""interface.worker — the single browser-worker process.

    jobs.run_rate_search  — what RQ executes (search -> filter -> select)
    main.run_worker       — lock, one provider, serial SimpleWorker loop
    __main__              — `python -m translog_quote.interface.worker`
                            (`--login` opens the headed operator sign-in)
"""

from translog_quote.interface.worker.main import build_provider, run_worker

__all__ = ["build_provider", "run_worker"]
