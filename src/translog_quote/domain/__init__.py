"""Domain layer (L2) — pure types, enums, schemas and contracts.

No I/O, no clock reads, no randomness, no knowledge of any external system. This
layer imports only `translog_quote.domain`, `translog_quote.ports`, pydantic and
the standard library.

At Phase 1 these modules hold *types* only. The behaviour that operates on them —
record merging, the eleven validation rules, the rate filters, the comparator
chain — arrives in later phases.
"""
