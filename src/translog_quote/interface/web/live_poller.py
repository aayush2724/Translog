"""The mailbox poll, run by the server rather than by a person.

The live demonstration used to advance only when somebody pressed "Check mail".
That made the operator part of the machinery: an enquiry sat unprocessed until
a click, the client's reply sat unprocessed until another one, and the workflow
looked manual in exactly the places where it is not.

This thread removes the click and nothing else. It calls the same
`LiveSession.poll()` the button called, under the same lock every request
handler takes, at a fixed interval. No stage is skipped and no gate is opened:
`poll()` reads the mailbox and stops at both human gates, which is the property
the whole demonstration rests on.

Two rules make it safe to leave running:

- **It cannot die.** A poll that raises — an expired token, a provider outage,
  a malformed message — records the class of failure and waits for the next
  interval. A thread that ended on its first bad poll would leave a dashboard
  that silently never updated again, which is worse than the failure it died
  of.
- **It cannot leak.** What reaches the browser is the exception's class name;
  the full text goes to the log, where provider detail belongs.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from translog_quote.observability import get_logger

if TYPE_CHECKING:
    from translog_quote.interface.web.live_session import LiveSession

_log = get_logger("interface.web.live_poller")

#: Where Linux reports this process's resident set size.
_STATUS = "/proc/self/status"


def resident_kb() -> int | None:
    """This process's RSS in kB, or None where the kernel does not report it.

    Diagnostic only — nothing branches on it. It exists because the hosting
    plan this demonstration runs on shows no memory graph, and the instance has
    been killed for exceeding its limit: without this the only evidence of how
    memory behaved before a kill is that the log stops. Reading one line of
    /proc costs nothing and turns every poll into a datapoint.

    Returns None rather than raising anywhere /proc is absent (macOS, Windows),
    so the poller is unchanged on a developer's machine.
    """
    try:
        with open(_STATUS, encoding="utf-8") as status:  # noqa: PTH123 - /proc, not a path op
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


class LivePoller:
    """Reads the mailbox on a timer for as long as the server is up.

    The lock is the server's own, passed in rather than made here: a poll
    mutates the very session the request handlers serialise their reads
    against, and a second lock would serialise nothing.
    """

    def __init__(
        self,
        session: LiveSession,
        *,
        lock: threading.Lock,
        interval_seconds: float,
    ) -> None:
        self._session = session
        self._lock = lock
        self._interval = max(0.1, interval_seconds)
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._polls = 0
        """How many polls this process has run. Logged beside RSS so a memory
        curve can be read against poll count rather than against wall time,
        which varies with how slow the instance is that minute."""

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Begin polling. Idempotent, so a double start cannot make two threads."""
        if self.is_running:
            return
        _log.info(
            "Mailbox poll starting: interval=%.1fs poll=0 rss=%s kB",
            self._interval,
            resident_kb(),
        )
        self._stopped.clear()
        # A daemon thread: Ctrl+C on the server must not be held open by a poll
        # waiting on a Gmail request that will never answer.
        self._thread = threading.Thread(target=self._run, name="translog-live-poll", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Ask the thread to finish the poll it is in, and stop. Sends nothing."""
        self._stopped.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def poll_once(self) -> bool:
        """One poll under the server lock. True when it read the mailbox.

        Public because it is the whole unit of work: a test drives this
        directly and gets exactly what the loop does, with no timing in it.
        """
        self._polls += 1
        started = time.monotonic()
        try:
            with self._lock:
                self._session.poll()
        except Exception as exc:  # noqa: BLE001 - the top of a thread
            # Deliberately every exception. There is nobody above this frame to
            # catch anything: letting one out ends polling for the life of the
            # process, and the only symptom would be a dashboard that quietly
            # stopped moving.
            self._session.last_poll_error = type(exc).__name__
            _log.warning(
                "Background mailbox poll %d failed after %.1fs (rss=%s kB): %s",
                self._polls,
                time.monotonic() - started,
                resident_kb(),
                exc,
            )
            return False
        self._session.last_poll_error = None
        # One line per poll: the poll number, how long the lock was held, and
        # the memory the process is holding at that moment. Enough to tell a
        # flat curve from a climbing one, and to see the last value before a
        # kill leaves nothing else behind.
        _log.info(
            "Poll %d done in %.1fs, rss=%s kB, requests=%d, audit=%d",
            self._polls,
            time.monotonic() - started,
            resident_kb(),
            len(self._session.requests),
            len(self._session.audit.events),
        )
        return True

    def _run(self) -> None:
        # Polls immediately, then waits. The first read must not be an interval
        # late: the operator opens the dashboard and sends the enquiry, and the
        # gap between those two is usually shorter than the interval.
        while not self._stopped.is_set():
            self.poll_once()
            self._stopped.wait(self._interval)
