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


class IllegalTransition(TranslogError):
    """A state change was attempted that the transition table does not permit.

    A programming error, not a business outcome.
    """
