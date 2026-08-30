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


class UnresolvedFieldMapping(ContractViolation):
    """A field's source has not been verified, so no mapping may be written.

    This is how an integration blocker travels as executable code rather than as a
    comment. The known instance is AMB-1: the real WebCargo transit-time source is
    unverified, so `RealRateMapper` raises this rather than guessing a field.
    """


class IllegalTransition(TranslogError):
    """A state change was attempted that the transition table does not permit.

    A programming error, not a business outcome.
    """
