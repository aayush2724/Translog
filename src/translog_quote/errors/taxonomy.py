"""The error classes the system is allowed to raise."""


class TranslogError(Exception):
    """Base class. Never raised directly."""


class TransientFailure(TranslogError):
    """Infrastructure failed in a way that may succeed on retry.

    Network errors, timeouts, 5xx responses, rate limits. Raised and retried
    inside an adapter, bounded and logged. Must not escape `adapters/` untranslated.
    """


class PermanentFailure(TranslogError):
    """Infrastructure failed in a way retrying cannot fix.

    Missing configuration, rejected credentials, an endpoint that does not exist.
    """


class WebCargoSessionLost(PermanentFailure):
    """The persistent WebCargo session no longer reaches the authenticated app.

    Raised instead of any automatic login attempt: the operator
    re-authentication command is the only path back, never the worker process.
    Lives in the taxonomy (not the adapter) so the worker's application layer can
    recognise it without importing the adapter — the layering rule that keeps
    adapters reachable only through the composition root.
    """


class WebCargoUnreachable(TransientFailure):
    """WebCargo could not be reached or loaded — DNS/connection/timeout or a
    navigation failure — as distinct from a loaded login page.

    This is the boot-before-network case: transient, not a login problem. The
    worker retries with backoff and, if still unreachable, exits with a normal
    non-78 code so systemd restarts it later — it must NEVER be mistaken for
    ``WebCargoSessionLost`` (which writes ``needs_login`` and stops for an
    operator). Lives in the taxonomy so the worker layer can tell the two apart
    without importing the adapter.
    """


class UnresolvedLocation(PermanentFailure):
    """A place the client named could not be resolved to a provider identifier.

    A property of one enquiry, never of the system: it names a request that
    cannot be priced yet, and says nothing about any other request in the same
    poll. Callers isolate it per request rather than letting it end the batch.

    Deliberately *not* recoverable by inference. Deriving an airport code from a
    place name — by prefix, by similarity, by any table shipped in this
    repository — produces a search against the wrong lane that succeeds and
    looks right, which is the one failure mode a quotation must never have
    (AMB-9). Refusing is the safe answer and the only one permitted here.
    """


class ContractViolation(TranslogError):
    """Something produced output that does not satisfy its declared contract.

    A model response that fails schema validation, a required field that cannot be
    mapped, a payload missing a documented key. Raised loudly and never repaired by
    guessing: half a shipment record is more dangerous than none, because validation
    would pass it.
    """


class ExtractionUnavailable(TranslogError):
    """The extraction model could not be called for one message.

    Raised only by the clarification workflow, and only around its single model
    call: the provider's `TransientFailure`/`PermanentFailure` is translated into
    this so a caller can isolate *that message* without also swallowing an
    unrelated infrastructure failure elsewhere in the turn. ``permanent`` says
    whether retrying soon can help (a timeout) or not (a spent API key, HTTP 402).
    """

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


class IllegalTransition(TranslogError):
    """A state change was attempted that the transition table does not permit.

    A programming error, not a business outcome.
    """
