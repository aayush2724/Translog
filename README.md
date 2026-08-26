# Translog Cargo Quotation Automation — Demo

An AI-assisted cargo quotation workflow, built as a **demonstration / proof of
concept**. A client emails a shipment request; the system understands the email,
asks for anything missing, searches for rates, selects the fastest eligible one,
and stops so a human can approve before the quotation reaches the client.

> **Status: Phase 1 — architecture only.**
> The project structure, domain types, port interfaces, configuration and test
> foundation exist. **No business functionality is implemented, and no external
> service is integrated.** See [What is not implemented](#what-is-not-implemented-yet).

---

## What this is

Two human roles, and only two.

**The client** communicates by email alone — no account, no portal, no
authentication, and no contact with WebCargo.

**The quotation maker** is the internal person who sees the extracted shipment,
what is missing, clarification status, the WebCargo results and the recommended
rate — then explicitly approves sending the quotation, and afterwards sees the
client's acceptance or rejection.

The pipeline:

```
client email -> ingestion -> AI extraction -> canonical shipment -> validation
             -> clarification if required -> rate search -> normalisation
             -> hard filtering -> fastest eligible rate
             -> QUOTATION MAKER APPROVAL -> quotation sent -> client accept/reject
```

The AI has exactly one job: turning unstructured email into structured fields. It
does not decide whether a shipment is valid, choose an airline, rank rates, approve
quotations, send anything, or book cargo. Those are deterministic application logic
or explicit human action.

## Current demo scope

Four scenarios, each intended to run end to end as an executable specification:

| | Scenario |
|---|---|
| **S1** | Complete email → quotation → accept |
| **S2** | Incomplete email → clarification → client reply → completed → quotation |
| **S3** | Multiple rates → hard filtering → fastest eligible rate |
| **S4** | Quotation → client rejection |

Rate selection ranks by **shortest transit time**. Price is a tie-breaker only, not
the primary criterion, and the ranking strategy is configuration rather than code.

**Explicitly out of scope:** the production website, any frontend, authentication,
a client portal, production infrastructure, and automatic booking.

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

The rate pipeline is four separate stages, deliberately never one function:

```
raw payload --RateMapper--> Rate[] --FilterChain--> FilterOutcome --RateSelector--> Selection
```

Mapping changes shape but never membership; filtering changes membership but never
order and never scores; selection changes order but never membership.

Full detail, including the state machine, failure boundaries and mock strategy:
**[`docs/architecture.md`](docs/architecture.md)**.

## What is not implemented yet

Nothing in this list is stubbed, faked, or partially wired. It does not exist.

| Not implemented | Arrives in |
|---|---|
| OpenRouter / Qwen extraction | Phase 5 — blocked on the exact model slug (AMB-2) |
| WebCargo integration, mock **or** real | Phase 4 |
| Gmail, SMTP, or any live mailbox | later — fixtures first |
| Record merging, the eleven validation rules | Phase 2 |
| Clarification composition | Phase 2 |
| Rate normalisation, filtering, ranking | Phase 3 |
| Quotation composition, approval gate, sending | Phase 7 |
| Client response handling | Phase 7 |
| The four demo scenarios | Phase 8 |
| Frontend, authentication, booking | never — out of scope |

**The real WebCargo adapter is blocked.** The rate response documented in the
specification carries no transit time, flight date or routing, and the fastest-transit
rule needs one. Recorded as AMB-1:

> Real WebCargo transit-time source must be verified before production
> implementation. Demo transit times are supplied by the mock adapter.

The demo is not blocked by this — everything downstream of normalisation is fully
implementable, and the mock adapter supplies deterministic transit values.

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

At Phase 1 the suite proves the skeleton is sound, not that it does anything:

- every package and module imports
- configuration loads safely with no environment and no `.env`
- no credential has a hardcoded default
- every port is a `typing.Protocol`, not a base class with behaviour
- the layering rule holds — `domain` is pure, and only `bootstrap` names an adapter
- the state machine's transition table is complete and enforced

Type checking and linting:

```bash
.venv/bin/python -m mypy
.venv/bin/python -m ruff check .
```

## How future phases add functionality

Each phase produces something demonstrable, and the phases are ordered so the
unresolved questions block as little as possible.

| Phase | Adds | Blocked by |
|---|---|---|
| 1 ✅ | Architecture skeleton — types, ports, config, tests | — |
| 2 | Canonical record merging and the eleven validation rules | — |
| 3 | Rate normalisation, filtering, selection | AMB-1, AMB-3 for real data shapes |
| 4 | Mock WebCargo adapter and fixture rate sets | — |
| 5 | Extraction via OpenRouter | AMB-2 |
| 6 | Ingestion, clarification, and the reply loop | AMB-11 |
| 7 | Quotation, approval gate, client decision | AMB-5, AMB-6, AMB-10, AMB-13 |
| 8 | The four scenarios, wired end to end | all prior |

New integrations arrive as an adapter behind an existing port plus one line in
`bootstrap`. No domain change is required for any of them.

## Documentation

| | |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Module boundaries, dependency rules, data flow, state transitions, failure boundaries, mock strategy, extension points |
| [`docs/reference/`](docs/reference/) | Frozen source documents and a note on what is deliberately not committed |
