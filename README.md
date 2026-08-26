# Translog Cargo Quotation Automation — Demo

An AI-assisted cargo quotation workflow, built as a **demonstration / proof of
concept**. A client emails a shipment request; the system understands the email,
asks for anything missing, searches for rates, selects the fastest eligible one,
and stops so a human can approve before the quotation reaches the client.

> **Status: Phase 4 complete.**
> The deterministic core of the workflow exists and is tested: the canonical
> shipment record, normalization, merging with conflict detection, the eleven
> validation rules, a deterministic email fixture layer, and the extraction
> contract a model will later be held to. **No external service is
> integrated** — no model is ever called, no mailbox, no rate provider.
> See [What is implemented today](#what-is-implemented-today).

---

## What this is

Two human roles, and only two.

**The client** communicates by email alone — no account, no portal, no
authentication, and no contact with WebCargo.

**The quotation maker** is the internal person who sees the extracted shipment,
what is missing, clarification status, the WebCargo results and the recommended
rate — then explicitly approves sending the quotation, and afterwards sees the
client's acceptance or rejection.

There is no client authentication, no client portal, no frontend, and no customer
account system in this proof of concept, and none is planned for it.

The AI has exactly one job: turning unstructured email into structured fields. It
does not decide whether a shipment is valid, choose an airline, rank rates, approve
quotations, send anything, or book cargo. Those are deterministic application logic
or explicit human action.

## Target POC flow

**This is the destination, not the current state.** Only the steps marked below
exist today; everything else is a later phase.

```
Client email
    ↓
Email ingestion
    ↓
Qwen extraction                       ← contract implemented; adapter not
    ↓
Canonical shipment                    ← implemented
    ↓
Deterministic validation              ← implemented
    │
    ├── incomplete
    │       ↓
    │   clarification email
    │       ↓
    │   client reply
    │       ↓
    │   extraction + merge + re-validation   ← merge implemented
    │
    └── valid
            ↓
       WebCargo rate search
            ↓
       normalize rates
            ↓
       eligibility filtering
            ↓
       fastest eligible rate
            ↓
       quotation maker approval
            ↓
       quotation sent
            ↓
       client ACCEPT / REJECT
```

## What is implemented today

Everything in this list exists, is typed, and is covered by tests.

| Implemented | Where |
|---|---|
| Modular monolith architecture, five layers, enforced by an import-layering test | `src/translog_quote/`, `tests/architecture/` |
| Domain models — shipment, email, conversation, rates, quotation, decision, workflow | `domain/` |
| Typed ports (interfaces only, no implementations) | `ports/` |
| Configuration, environment-backed, safe with nothing set | `config/` |
| Request state machine — 12 states, transition table, enforcement | `domain/workflow/`, `pipeline/state_machine.py` |
| Deterministic shipment normalization (whitespace, blank-as-absent) | `domain/shipment/normalize.py` |
| Canonical shipment merging across replies | `domain/shipment/merge.py` |
| Conflict detection on contradicting values | `domain/shipment/merge.py` |
| Deterministic validation, all eleven business rules | `domain/validation/validator.py` |
| Conditional MSDS rule (chemical shipments) | `MSDS_REQUIRED_FOR_CHEMICAL` |
| Conditional address rule (door delivery) | `ADDRESS_REQUIRED_FOR_DOOR` |
| Deterministic email fixtures, five scenarios | `fixtures/emails/` |
| Fixture email source implementing `EmailSource` | `adapters/email/fixtures.py` |
| Conversation/thread fixture relationships | `EmailFixtureScenario.thread` |
| Extraction contract — four-state fields, evidence, ambiguity | `domain/extraction/` |
| Deterministic mapping into the canonical record | `to_extracted_fields` |
| The extraction prompt and its injection boundary | `domain/extraction/prompt.py` |

## What is not implemented yet

Nothing in this list is stubbed, faked, or partially wired. It does not exist.

| Not implemented | Arrives in |
|---|---|
| OpenRouter adapter, any model call | Phase 5 |
| Extraction → shipment → validation pipeline; clarification decision loop | Phase 6 |
| Clarification message generation | Phase 6 |
| Production email/thread correlation policy | Phase 7 |
| Gmail, SMTP, or any live mailbox | Phase 7 and beyond |
| WebCargo integration — mock **or** real | Phase 8 |
| Rate normalization and eligibility filtering | Phase 9 |
| Fastest eligible rate selection | Phase 10 |
| Quotation generation, approval workflow, sending | Phase 11 |
| Client ACCEPT / REJECT workflow | Phase 12 |
| End-to-end demo orchestration | Phase 13 |
| Database persistence | not planned for the POC |
| Frontend, authentication, client portal, booking | out of scope |

## Validation

Deterministic, pure, and **never uses a language model**. `validate_shipment()`
takes a `ShipmentRecord` and returns a `ValidationResult` without mutating its
input; calling it twice on an unchanged record returns an equal result. It reports
*every* applicable failure in one pass — it never stops at the first — because a
later phase turns one result into one batched clarification message.

`ValidationResult` carries a tuple of `ValidationIssue`, each with:

| Field | Meaning |
|---|---|
| `rule_id` | stable machine-readable identifier (`ValidationRuleId`) |
| `field` | which shipment field the issue concerns (`FieldName`) |
| `severity` | `MISSING`, `INVALID`, or `WARNING` (`ValidationSeverity`) |
| `message` | human-readable explanation |

with derived views: `is_valid`, `missing_fields`, `invalid_fields`, `warnings`.

`WARNING` is declared and supported by the result type, but **no current rule
produces one** — it exists so a future non-blocking rule needs no redesign. A
result carrying only warnings is still `is_valid`.

### The eleven business rules

Nine unconditional:

| # | Business rule | Rule identifier(s) in code |
|---|---|---|
| 1 | Origin required | `ORIGIN_REQUIRED` |
| 2 | Destination required | `DESTINATION_REQUIRED` |
| 3 | Weight required, and must be a positive number | `WEIGHT_REQUIRED`, `WEIGHT_INVALID` |
| 4 | Dimensions required | `DIMENSIONS_REQUIRED` |
| 5 | Commodity required | `COMMODITY_REQUIRED` |
| 6 | Cargo type required | `CARGO_TYPE_REQUIRED` |
| 7 | Chemical status required | `CHEMICAL_STATUS_REQUIRED` |
| 8 | Pieces required, and must be a positive count | `PCS_REQUIRED`, `PCS_INVALID` |
| 9 | Delivery type required | `DELIVERY_TYPE_REQUIRED` |

Two conditional:

| # | Condition | Then required | Rule identifier |
|---|---|---|---|
| 10 | `is_chemical = true` | MSDS information | `MSDS_REQUIRED_FOR_CHEMICAL` |
| 11 | `delivery_type = DOOR` | delivery address | `ADDRESS_REQUIRED_FOR_DOOR` |

Eleven business rules, **thirteen** rule identifiers: weight and pieces each have a
separate identifier for their `INVALID` case, so "absent" and "present but
nonsensical" are distinguishable by a caller without parsing a message string.

Two notes on exact semantics as implemented:

- **MSDS means "known", not "true".** `is_chemical = true` with
  `msds_attached = false` is **valid** — the client answered the question. Only
  `msds_attached = None` (never asked) triggers rule 10.
- **There is no `DIMENSIONS_INVALID`.** `CargoDimensions` enforces positive
  length, width and height at construction, so a non-positive dimension cannot
  exist inside a `ShipmentRecord`; it is rejected at the type boundary, and the
  validator only ever sees "present" or "absent".

## Merge behaviour

`merge_shipment(existing, incoming)` folds a new extraction into an existing
record and returns a `MergeResult` — the updated `record`, plus `changed`,
`unchanged` and `conflicts`. Three rules, applied independently per field:

| Existing | Incoming | Result |
|---|---|---|
| missing | present | fill the value → reported in `changed` |
| present | identical | retain the value → reported in `unchanged` |
| present | **different** | **conflict** → reported in `conflicts`; the record is not modified |

A fourth case follows from the first three: if the incoming extraction says
nothing about a field, the existing value carries over untouched and nothing is
reported for it.

**A conflict never silently overwrites.** `FieldConflict` records the `field`,
the `existing_value` and the `new_value`, and the merged record keeps the
existing value unchanged. Retaining is not deciding — the merge makes no claim
that the older value is correct, only that it will not discard data on its own.

**The current implementation does not decide which conflicting value is right.**
Conflict resolution is a future policy/workflow responsibility, and no resolution
policy has been specified or invented.

## Email fixtures

Deterministic demo and test data — **not Gmail integration, and not any mailbox
integration.** `FixtureEmailSource` reads plain-text `.eml`-style files from disk
and returns `RawEmail` values. It speaks no mail protocol and has no network
dependency.

| Scenario | Directory | Demonstrates |
|---|---|---|
| A | `a_complete_request/` | One email with every required field present, both conditionals satisfied |
| B / C | `b_incomplete_then_clarified/` | Two messages, one thread: an incomplete request, then a clarification reply supplying the missing fields |
| D | `d_conflicting_reply/` | Two messages, one thread: 500 kg, then a reply stating 700 kg. Unresolved by design |
| E | `e_chemical_shipment/` | Chemical cargo with MSDS explicitly stated |
| F | `f_door_delivery/` | Door delivery with an address present |

Fixture-assigned request IDs (`R-DEMO-A` … `R-DEMO-F`) are **test/demo identifiers,
not production correlation output.** No correlation policy has been implemented;
`EmailFixtureScenario.thread` groups messages from fixture metadata. What the
fixture layer does guarantee is that the underlying data is *correlatable* — each
reply's `in_reply_to` and `references` correctly chain to its parent, which is
what a real policy will depend on once it exists.

Detail: [`fixtures/emails/README.md`](fixtures/emails/README.md).

### The `RawEmail` contract

`RawEmail` carries exactly: `message_id`, `from_address`, `subject`, `body_text`,
`received_at`, `in_reply_to`, `references`.

It has **no `thread_id` field and no recipient field**, deliberately. Thread
identity is not stamped onto a message; it is derived by correlating `in_reply_to`
and `references`, which is why a stamped `thread_id` would defeat the mechanism.
Thread identity lives on `Thread.request_id` instead. Fixture files do carry a
`To:` header for realism, which the parser reads and discards.

## Current architecture

A **modular monolith** — one process, one deployable. Not microservices. Module
boundaries here are compile-time and review-time boundaries enforced by import
rules, not network hops.

```
interface  ->  pipeline  ->  domain  ->  ports
adapters   ->  ports        (implements)
bootstrap  ->  everything   (the only module naming a concrete adapter)
```

| Layer | Package | Holds |
|---|---|---|
| L4 | `interface/` | demo runners, CLI entry points |
| L3 | `pipeline/` | orchestration, state machine, audit |
| L2 | `domain/` | shipment, validation, clarification, conversation, rates, quotation, decision, workflow — **pure** |
| L1 | `ports/` | interfaces only |
| L0 | `adapters/` | concrete implementations |

`domain` imports nothing but `domain`, `ports`, pydantic and the standard library.
That rule is enforced by `tests/architecture/test_layering.py`, which fails the
build on a violation.

The rate pipeline is designed as four separate stages, deliberately never one
function — **none of these stages is implemented yet** (Phases 8–10):

```
raw payload --RateMapper--> Rate[] --FilterChain--> FilterOutcome --RateSelector--> Selection
```

Mapping changes shape but never membership; filtering changes membership but never
order and never scores; selection changes order but never membership.

Full detail, including the state machine, failure boundaries and mock strategy:
**[`docs/architecture.md`](docs/architecture.md)**.

## Rate selection requirement

The current stakeholder requirement, superseding the older reference document's
cheapest-wins example:

1. **Filter for eligibility** — hard rules only, no scoring.
2. **Select the fastest eligible transit time.**
3. **Use total price as the tie-breaker** where transit times are equal.
4. **Apply `carrier_code` as a final deterministic tie-breaker** — not a business
   rule, only a guarantee that two identical offers always produce the same winner.

Encoded as data in `domain/rates/strategy.py` (`FASTEST_ELIGIBLE`), so switching
the business to cheapest-wins is a reordering of keys rather than a code change.
**The selection logic itself is not implemented** — Phase 10.

## Unresolved business questions

These block specific future phases. None blocks work that is already done, and
none has been resolved by guessing.

| Question | Status | Blocks |
|---|---|---|
| **Extraction model identifier.** Intended model: **Qwen 3.7 Flash**. The provider-specific model identifier will be configured during the OpenRouter integration phase after verification. No slug is guessed; `openrouter.model` defaults to `None`, and a test asserts it stays that way. | Unresolved | Phase 5 |
| **Real WebCargo transit-time source.** Must be verified before production implementation. Demo transit times are supplied by the mock adapter. The real adapter does not invent a source and assumes no field carries one. | Unresolved for production; resolved for the demo | Real adapter only |
| **`RateQuery.date` — where the search date comes from.** A rate query requires a date, but no approved project document establishes a business rule for its origin. Not defaulted to today, tomorrow, next available, shipment date, or quote date. | **Unresolved — blocking** | Phase 8 |
| **Carrier eligibility rules and their input data.** The one documented restriction concerns liquid products, but nothing maps a commodity to its physical form. | Unresolved | Phase 9 |
| **Does client rejection loop back to selection?** Built as terminal; the loop edge is specified but not wired. | Unresolved | Phase 12 |

## Installing

Requires Python 3.12 or newer.

```bash
uv venv
uv pip install -e ".[dev]"
```

Or with plain pip:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Configuration is optional — the project loads and its tests pass with nothing set:

```bash
cp .env.example .env    # then fill in only what you need
```

Secrets come from the environment and are never committed.

## Running tests

```bash
.venv/bin/python -m pytest
```

The suite covers:

- **Architecture** — every module imports; configuration loads safely with no
  environment and no `.env`; no credential has a hardcoded default; every port is
  a `typing.Protocol`; the layering rule holds; only `bootstrap` names an adapter
- **Workflow** — the state machine's transition table is complete and enforced
- **Shipment** — normalization, merging, conflict detection, and the reference
  clarification and conflict scenarios end to end
- **Validation** — all eleven rules individually, both conditionals in their
  passing and failing forms, multi-failure reporting, and determinism
- **Fixtures** — unique message IDs, thread grouping, deterministic ordering,
  reply-to-parent chaining, `RawEmail` contract conformance, and a secret scan
- **Extraction** — contract invariants, malformed and impossible model output,
  the four absence states, mapping into the canonical record, eight worked
  examples against the real fixtures, and the prompt-injection boundary

Type checking, linting, and formatting:

```bash
.venv/bin/python -m mypy
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| **1** | Architecture, domain contracts, ports, configuration | ✅ **COMPLETE** |
| **2** | Canonical shipment normalization, merging, conflict detection, and deterministic validation | ✅ **COMPLETE** |
| **3** | Email fixtures, conversation fixture data, deterministic fixture source | ✅ **COMPLETE** |
| **4** | Qwen 3.7 Flash extraction contract | ✅ **COMPLETE** |
| **5** | Qwen 3.7 Flash + OpenRouter adapter | **NEXT** |
| **6** | Extraction → canonical shipment → validation pipeline, and clarification decision loop | Planned |
| **7** | Email/thread correlation and clarification reply handling | Planned |
| **8** | Mock WebCargo adapter for deterministic demo behaviour | Planned |
| **9** | Rate normalization and eligibility filtering | Planned |
| **10** | Fastest eligible rate selection | Planned |
| **11** | Quotation-maker approval and quotation sending workflow | Planned |
| **12** | Client ACCEPT / REJECT workflow | Planned |
| **13** | End-to-end demo orchestration and demo hardening | Planned |

New integrations arrive as an adapter behind an existing port plus one line in
`bootstrap`. No domain change is required for any of them.

## Documentation

| | |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Module boundaries, dependency rules, data flow, state transitions, failure boundaries, mock strategy, extension points |
| [`docs/extraction-contract.md`](docs/extraction-contract.md) | What a model may report, field semantics, missing/negative/ambiguous behaviour, error handling, the injection boundary, worked examples |
| [`fixtures/emails/README.md`](fixtures/emails/README.md) | Fixture format, scenarios, and what the fixture layer does not do |
| [`docs/reference/`](docs/reference/) | Frozen source documents and a note on what is deliberately not committed |
