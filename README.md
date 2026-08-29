# Translog Cargo Quotation Automation — Demo

An AI-assisted cargo quotation workflow, built as a **demonstration / proof of
concept**. A client emails a shipment request; the system understands the email,
asks for anything missing, searches for rates, selects the fastest eligible one,
and stops so a human can approve before the quotation reaches the client.

> **Status: the workflow runs end to end against real Gmail.**
> A client emails a real mailbox; Qwen extracts it; deterministic validation
> finds what is missing; a person approves a clarification that is really sent;
> the client's reply is correlated by RFC header chain and merged; rates are
> simulated and clearly labelled; a person approves or declines; and only on
> approval does a quotation reach the client. Drivable from the terminal or
> from a browser.
>
> Two things are still genuinely absent, and neither is faked: **the real
> WebCargo integration** (no published contract) and **client ACCEPT / REJECT**
> handling. See [What is not implemented](#what-is-not-implemented).
>
> 995 tests pass with no credentials configured; a model or a mailbox is
> reached **only** when explicitly configured.

---

## What this is

Two human roles, and only two.

**The client** communicates by email alone — no account, no portal, no
authentication, and no contact with WebCargo.

**The quotation maker** is the internal person who sees the extracted shipment,
what is missing, clarification status, the WebCargo results and the recommended
rate — then explicitly approves sending the quotation, and afterwards sees the
client's acceptance or rejection.

There is no client authentication, no client portal, and no customer account
system in this proof of concept, and none is planned for it. The browser UI that
exists is the quotation maker's own console — an internal operator view of the
same workflow the terminal drives, bound to localhost. No client ever sees it.

The AI has exactly one job: turning unstructured email into structured fields. It
does not decide whether a shipment is valid, choose an airline, rank rates, approve
quotations, send anything, or book cargo. Those are deterministic application logic
or explicit human action.

## The workflow

Every step below is implemented and tested. The two marked otherwise are the
only gaps.

```
Client email  (real Gmail, read-only credential)
    ↓
Qwen extraction  (live)
    ↓
Canonical shipment
    ↓
Deterministic validation
    │
    ├── incomplete
    │       ↓
    │   clarification drafted   — deterministic wording, no model
    │       ↓
    │   ┌───────────────────────────────┐
    │   │  HUMAN GATE — named approval  │
    │   └───────────────────────────────┘
    │       ↓
    │   clarification sent  (real Gmail, send-only credential)
    │       ↓
    │   client reply
    │       ↓
    │   RFC In-Reply-To / References correlation
    │       ↓
    │   extraction + merge + re-validation
    │
    └── valid
            ↓
       rate search             — DemoRateProvider, SIMULATED and disclosed
            ↓                    (real WebCargo: not implemented)
       eligibility filtering
            ↓
       fastest eligible rate
            ↓
       review packet emailed to the internal approver
            ↓
       ┌───────────────────────────────────┐
       │  HUMAN GATE — approve or decline  │
       └───────────────────────────────────┘
            ↓
       quotation sent to the client  (only on approval)
            ↓
       client ACCEPT / REJECT      — not implemented
```

### The two human gates

Neither can be bypassed, and neither has a default or a timeout.

**Clarification.** A draft is held at `NEEDS_INFO`, and the transition table
permits exactly one edge out of it: `CLARIFICATION_SENT`. Releasing it requires
a named person, and the `CLARIFICATION_SENT` audit event is emitted only after
the mail sink has accepted the message.

**Quotation.** `ApprovalPort` is modelled as a *halt*, not a callback and not a
timeout (BR-11). `Quotation` requires an `Approved` as a mandatory field, so
under `mypy --strict` a `Rejected` cannot reach the send path — "send without
approval" is not a code path waiting to be tested, it is a call that does not
type-check. Verified:

```
Argument 2 to "build_quotation" has incompatible type "Rejected"; expected "Approved"
```

A decision is refused twice over if repeated: an in-memory ledger within a
process, and the persisted request state across processes.

## What is implemented today

Everything in this list exists, is typed, and is covered by tests.

| Implemented | Where |
|---|---|
| Modular monolith, five layers, enforced by an import-layering test | `src/translog_quote/`, `tests/architecture/` |
| Domain models — shipment, email, conversation, rates, quotation, decision, workflow | `domain/` |
| Typed ports (interfaces only, no implementations) | `ports/` |
| Request state machine — 12 states, transition table, enforcement | `domain/workflow/`, `pipeline/state_machine.py` |
| Shipment normalization, merging across replies, conflict detection | `domain/shipment/` |
| Deterministic validation, all eleven business rules | `domain/validation/validator.py` |
| Extraction contract — four-state fields, evidence, ambiguity, injection boundary | `domain/extraction/` |
| OpenRouter / Qwen 3.7 Flash extraction adapter, bounded retries | `adapters/extraction/` |
| **Clarification loop** — draft, hold, release on named approval | `pipeline/clarification_loop.py` |
| **Clarification wording** — deterministic templates, no model | `domain/clarification/` |
| **RFC header correlation** — In-Reply-To then References, refuses to guess | `domain/conversation/correlation.py` |
| **Inbound routing** — place a message, then process it | `pipeline/inbound.py` |
| **Gmail inbound** — one mailbox, `gmail.readonly`, GET-only transport | `adapters/email/gmail.py` |
| **Gmail outbound** — `gmail.send`, POST to one endpoint, separate token | `adapters/email/gmail_send.py` |
| **DemoRateProvider** — simulated rates priced from the shipment, flagged | `adapters/webcargo/demo.py` |
| **Rate pipeline** — map, filter with reasons, rank, select one | `domain/rates/`, `pipeline/rate_search.py` |
| **Quotation composition** — client copy and internal review packet, deterministic | `domain/quotation/compose.py` |
| **Approval gates** — terminal prompt and browser-recorded decision | `adapters/approval/` |
| **Quotation stage** — the only path from a selected rate to a client | `pipeline/quotation.py` |
| **Durable state** — atomic JSON store, survives separate invocations | `adapters/store/json_file.py` |
| **Audit trail** — append-only, persisted, drives the activity timeline | `pipeline/audit.py`, `interface/web/audit_log.py` |
| **Terminal demos** — extraction, clarification, rates, and the full Gmail flow | `interface/demo/` |
| **Web POC (scripted)** — the workflow over a fictional scenario, no credentials | `interface/web/`, `--` default |
| **Web UI (live)** — the real Gmail workflow, driven from a browser | `interface/web/live_session.py`, `--live` |
| **Demonstration scoping** — focus one enquiry without deleting or naming anything | `interface/web/demonstration.py` |

## What is not implemented

Nothing in this list is stubbed, faked, or partially wired.

| Not implemented | Why |
|---|---|
| **Real WebCargo rate search** | No published API contract, no confirmed authentication, and no verified transit-time source. `RealWebCargoAdapter` refuses with the reason rather than inventing one |
| **Client ACCEPT / REJECT handling** | `ClientIntent` and the state edges exist; `read_client_intent` raises rather than guessing |
| Database persistence | JSON files are the POC's durability. Not planned for the POC |
| Frontend authentication, client portal, booking | Out of scope |

## Running the demos

Both demos drive the same pipeline. Neither can send anything a person has not
explicitly approved.

### Without any credentials

```bash
.venv/bin/python -m translog_quote.interface.web          # browser, scripted scenario
.venv/bin/python -m translog_quote.interface.demo rates   # terminal, rate pipeline
```

The scripted web POC runs the real merge, validation, clarification wording,
filtering, selection and approval gate over a fictional enquiry, with no API
key and no mailbox.

### Against real Gmail

Two Gmail accounts and two separate OAuth grants — the read credential cannot
send and the send credential cannot read. Setup:
**[`docs/gmail-test-setup.md`](docs/gmail-test-setup.md)**.

```bash
.venv/bin/python -m translog_quote.interface.demo gmail-auth        # grant gmail.readonly
.venv/bin/python -m translog_quote.interface.demo gmail-auth-send   # grant gmail.send

.venv/bin/python -m translog_quote.interface.web --live             # browser
.venv/bin/python -m translog_quote.interface.demo gmail-quote \
    --approved-by "your.name@company"                               # terminal
```

| Command | What it does |
|---|---|
| `gmail-test` | Read one real message, extract, validate. Sends nothing |
| `gmail-process` | One real enquiry into the clarification loop, stopping at the gate |
| `gmail-thread` | A real reply, correlated and merged. Sends nothing |
| `gmail-quote` | The whole flow, including both gates and real sending |
| `reset-state` | Clear local demo state. Touches no mailbox and no credential |

In the browser: **Start new demonstration** scopes the view to mail arriving
from that moment, then **Check mail** reads the real mailbox, and the two gates
appear as cards with an approver-name field. Simulated rates carry
`SIMULATED WEBCARGO DATA — DEMO ONLY` everywhere they are shown, including in
the client's quotation email.

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
| L4 | `interface/` | terminal demos, CLI entry points, the web POC and the live browser UI |
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
order and never scores; selection changes order but never membership. Each is a
separate component so each can be tested for the property it must preserve.

Nothing on a `Rate` reveals which adapter produced it, so selection cannot
branch on provenance — there is no field it could branch on. Provenance travels
beside the rates (`is_simulated`) purely so the presentation layer can disclose
it.

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

The demo ends on the counter-intuitive result this ordering produces: the
winner is the *most expensive* survivor, because filtering runs before ranking
and the cheapest option is excluded by a hard rule. Every exclusion carries its
reason, and the review packet shows them — silence there would be
indistinguishable from a bug.

## Unresolved business questions

These block specific future phases. None blocks work that is already done, and
none has been resolved by guessing.

| Question | Status | Blocks |
|---|---|---|
| **Real WebCargo transit-time source.** Must be verified before production implementation. Demo transit times are supplied by the mock adapter. The real adapter does not invent a source and assumes no field carries one. | Unresolved for production; resolved for the demo | Real adapter only |
| **`RateQuery.date` — where the search date comes from.** No approved document establishes a rule for its origin. Rather than defaulting to today, tomorrow, next available, shipment date or quote date, `build_query` takes it as a required argument with no default: the caller states it and is accountable for it. | Unresolved; **not blocking** — stated, never invented | Production |
| **Carrier eligibility rules and their input data.** The one documented restriction concerns liquid products, but nothing maps a commodity to its physical form. `cargo_is_liquid` is therefore a required argument that is never derived from a commodity name. | Unresolved | Real adapter |
| **Does client rejection loop back to selection?** Built as terminal; the loop edge is specified but not wired. | Unresolved | Client ACCEPT / REJECT |

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

The Gmail demo needs one extra package, and only for the one-time OAuth consent
step — the runtime adapters need nothing beyond `httpx`:

```bash
.venv/bin/pip install -e ".[gmail]"
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

995 tests pass with nothing configured. No model is called and no mailbox is
contacted unless credentials are explicitly set.

The suite covers:

- **Architecture** — every module imports; configuration loads with no
  environment and no `.env`; no credential has a hardcoded default; every port
  is a `typing.Protocol`; the layering rule holds; only `bootstrap` names an
  adapter
- **Workflow** — the transition table is complete and enforced
- **Shipment** — normalization, merging, conflict detection, and the reference
  clarification and conflict scenarios end to end
- **Validation** — all eleven rules, both conditionals in passing and failing
  form, multi-failure reporting, determinism
- **Extraction** — contract invariants, malformed model output, the four
  absence states, eight worked examples, the prompt-injection boundary
- **Adapters** — request shape, retries and status handling against a mock HTTP
  server, credential hygiene, header-injection refusal on outbound mail, and
  that a failed send is never recorded as delivered
- **Correlation** — header-chain matching, and the refusal to guess when a
  message could belong to two requests
- **Gates** — no approval without a named person; a decline never sends; a
  quotation cannot be sent twice within a process *or* across processes; a gate
  consulted with no decision recorded raises rather than defaulting
- **Persistence** — state survives separate invocations; a corrupt state file
  fails loudly rather than reading as "nothing has happened"
- **Presentation** — simulated rates are labelled everywhere they appear
  including the client's email; no credential appears in any browser snapshot;
  the static surface is a closed whitelist; the timeline never invents a
  timestamp for a stage that has not happened

The browser UI has its own suite, run under node, which loads the shipped
`live.js` into a minimal DOM and drives the real controls:

```bash
node tests/js/live_ui.test.js
```

It is wired into pytest and skipped where node is unavailable.

A live extraction against OpenRouter is opt-in and skipped by default:

```bash
TRANSLOG_OPENROUTER__API_KEY=sk-or-... .venv/bin/python -m pytest -m live
```

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
| **2** | Shipment normalization, merging, conflict detection, validation | ✅ **COMPLETE** |
| **3** | Email fixtures, conversation data, deterministic fixture source | ✅ **COMPLETE** |
| **4** | Qwen 3.7 Flash extraction contract | ✅ **COMPLETE** |
| **5** | Qwen 3.7 Flash + OpenRouter adapter | ✅ **COMPLETE** |
| **6** | Extraction → shipment → validation pipeline, clarification decision loop | ✅ **COMPLETE** |
| **7** | Email/thread correlation and clarification reply handling | ✅ **COMPLETE** |
| **8** | Simulated rate provider for deterministic demo behaviour | ✅ **COMPLETE** |
| **9** | Rate normalization and eligibility filtering | ✅ **COMPLETE** |
| **10** | Fastest eligible rate selection | ✅ **COMPLETE** |
| **10.3–10.5** | Real Gmail inbound, reply correlation over a live mailbox | ✅ **COMPLETE** |
| **11** | Quotation-maker approval, outbound Gmail, quotation sending | ✅ **COMPLETE** |
| **11.5** | Durable state, browser UI, demonstration scoping | ✅ **COMPLETE** |
| **12** | Client ACCEPT / REJECT workflow | Planned |
| — | Real WebCargo integration | Blocked on a published API contract |

New integrations arrive as an adapter behind an existing port plus one line in
`bootstrap`. No domain change is required for any of them.

## Documentation

| | |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Module boundaries, dependency rules, data flow, state transitions, failure boundaries, mock strategy, extension points |
| [`docs/gmail-test-setup.md`](docs/gmail-test-setup.md) | Both OAuth grants, the outbound demo, the browser UI, and the clean demonstration procedure |
| [`docs/discovery/`](docs/discovery/) | What was verified about the Freightos and WebCargo APIs, and why the real adapter stays a refusal |
| [`docs/extraction-contract.md`](docs/extraction-contract.md) | What a model may report, field semantics, missing/negative/ambiguous behaviour, error handling, the injection boundary, worked examples |
| [`fixtures/emails/README.md`](fixtures/emails/README.md) | Fixture format, scenarios, and what the fixture layer does not do |
| [`docs/reference/`](docs/reference/) | Frozen source documents and a note on what is deliberately not committed |
