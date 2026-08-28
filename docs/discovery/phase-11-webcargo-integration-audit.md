# Phase 11 — WebCargo rate integration: readiness audit

**Date:** 2026-08-28 · **Status:** audit only — no adapter implemented
**Verdict:** **BLOCKED at the integration boundary.** The official Freightos /
WebCargo partner contract is not available in this project, so no real adapter
was written. The boundary was audited, and one untested seam was covered.

This continues Phase 10.1, which established *which* API is correct. This phase
establishes *what is still missing to call it*.

---

## 1. Availability check — what was actually looked for

| Looked for | Found |
|---|---|
| OpenAPI / Swagger / WSDL for WebCargo | none in the repository |
| Partner API reference document | none |
| WebCargo credentials in configuration | `TRANSLOG_WEBCARGO__*` unset; `username`/`password` are `None` |
| Sandbox endpoint or test account | none |
| Anything beyond Phase 10.1's public findings | none |

The only WebCargo material remains `docs/reference/cargo-automation-workflow.pdf`,
whose endpoints were captured from browser DevTools — by its own description.
That is not a published contract, and Phase 7's refusal to build on it stands.

**Therefore: stop conditions apply.** Nothing was probed, guessed, or
substituted.

---

## 2. Architecture map — the path a rate travels

```
ShipmentRecord
   │  build_query()            pipeline/rate_search.py — requires an explicit date (AMB-8)
   ▼
RateQuery                      domain/rates/model.py — origin/dest IATA, weight, dims, date
   │                           no field can carry a client identity (BR-13, structural)
   ▼
RateSearchPort.search()        ports/rates.py — the ONLY boundary an adapter may cross
   │                           returns normalised rates; raw payloads never cross as domain data
   ├── MockWebCargoAdapter     adapter_id="mock-webcargo"  ← today's demo, no network
   └── RealWebCargoAdapter     raises PermanentFailure with the reason  ← BLOCKED
   ▼
RateSearchResult(rates, adapter_id, raw_payload)
   │                           raw_payload is typed Any and read by nothing in domain/
   ▼
filter_rates()                 domain/rates/filters.py — membership only, never order
   │                           BR-4 no price · unrankable (no transit) · BR-5 restriction
   ▼
select_rate(FASTEST_ELIGIBLE)  domain/rates/strategy.py — transit ASC, price ASC, carrier ASC
   ▼
Selection → quotation
```

**The separation the phase requires is already structural, not conventional:**

- Filtering, ranking and selection live in `domain/rates/`. An adapter cannot
  reach them and they cannot reach an adapter.
- `Rate` carries no provenance field, so selection *cannot* branch on whether a
  rate is mock or real — there is no attribute to branch on.
- `RateSearchPort`'s docstring states implementations "filter nothing and rank
  nothing".
- The provider therefore cannot decide the recommendation. It supplies
  candidates; deterministic code picks the winner.

## 3. The two blockers, as executable code

Neither is a comment. Both fail loudly at the point of use:

| Blocker | Where | Behaviour |
|---|---|---|
| **AMB-1** — transit-time source unverified | `adapters/webcargo/mapper.py::map_transit` | raises `UnresolvedFieldMapping` naming the unresolved question |
| No contract at all | `adapters/webcargo/real.py::search` | raises `PermanentFailure` naming what is missing |

`RealRateMapper.map_row` already maps the fields the frozen specification does
document (`airline`, `product`, `total`, `currency`, plus an `accepts_liquids`
flag) and deliberately leaves `transit=None`. A rate with no transit is then
excluded by `drop_unrankable_rate` **with a reason**, so a mapping gap surfaces
as a visible exclusion rather than as a thin market.

This is the fail-closed behaviour the phase demands, and it already exists.

## 4. What is still missing from Freightos

Everything below is **UNKNOWN** — not establishable from public documentation.

### 4.1 Connection
1. Base URL of the WebCargo Booking API (production and sandbox).
2. Authentication mechanism — OAuth 2.0 client credentials, API key headers, or
   something else. Phase 10.1 confirmed this is *not* publicly documented.
3. Token lifetime and refresh rule, if token-based.
4. Rate limits, and whether `Retry-After` is sent on 429.
5. Whether a sandbox exists, and how to get access to it.

### 4.2 Rate-search request
6. Endpoint path and HTTP method for a rate search.
7. Field names and units for: origin, destination, weight, dimensions, pieces.
8. Whether dimensions are per-piece or total, and in which unit (our canonical
   record holds **inches**; the API may want cm).
9. Whether a commodity or cargo-type field is required.

### 4.3 Rate-search response — the critical part
10. **The transit-time field and its exact semantics.** This is AMB-1 and the
    primary ranking key. Specifically needed:
    - the field name;
    - its unit (hours, days, or a pair of timestamps);
    - whether it means door-to-door, airport-to-airport, or elapsed flight time;
    - whether it includes carrier handling / customs time.

    Until all four are confirmed, `map_transit` stays a refusal. **A guessed
    transit value would silently pick the wrong carrier on a real quotation.**
11. Price field, and whether it is all-in or excludes surcharges.
12. Currency field, and whether it is per-rate or per-response.
13. Carrier identification — IATA code vs. name vs. an internal id.
14. Product / service-level field and its vocabulary (our `Rate.product` holds
    values like `"GEN"`).
15. How "no rates available" is expressed — empty list vs. 404 vs. an error body.
16. Error body shape, so failures can be mapped to the existing taxonomy.

### 4.4 Business semantics
17. **AMB-8 — what `RateQuery.date` means to this API.** Departure date?
    Booking date? Validity date? `build_query` deliberately requires the caller
    to state a date and invents nothing.
18. **AMB-3 — carrier restrictions.** Only one liquids restriction is
    documented, and nothing maps a commodity to a physical form. If the API
    returns structured restrictions, their vocabulary is needed.

## 5. What can be implemented now, and what cannot

### Can be implemented now (no contract needed)
- **Nothing further is required for the boundary to be ready.** The port, the
  query type, the normalised `Rate`, the mapper skeleton, the filters, the
  ranking, the selection strategy, the mode switch and the audit events all
  exist and are tested.
- The reusable HTTP pattern already exists in
  `adapters/extraction/transport.py`: bounded timeout, bounded retries,
  exponential backoff with jitter, `Retry-After` honoured, a 30 s cap, and
  provider errors translated into the project taxonomy. The Gmail adapter
  (Phase 10.3) reuses it. A WebCargo transport would be the third consumer and
  should reuse the same pieces rather than copy them.

### Must wait for partner access
- The transport's URL, auth headers and payload shape.
- `RealRateMapper.map_transit` — completed **only** when semantics are confirmed.
- Contract tests against sanitized fixtures — a fixture cannot be sanitized
  from a response nobody has seen.
- Any sandbox smoke request.

## 6. Handoff — the exact request for Freightos

> Translog is building an automated air-freight quotation workflow and needs the
> **WebCargo Booking API** (Freightos for Forwarders / FaaS) as its rate source.
> We have the client-facing workflow working end to end against mock rates and
> are ready to integrate. We are requesting partner access, and specifically:
>
> 1. The WebCargo Booking API reference — endpoints, request/response schemas,
>    and error contract.
> 2. The authentication model, plus credentials for a **sandbox** environment.
> 3. Confirmation of the **transit-time field and its semantics**: field name,
>    unit, and whether it is door-to-door, airport-to-airport, or flight time,
>    and whether handling/customs time is included. Our carrier ranking is
>    driven entirely by transit time, so we will not map this field until the
>    semantics are confirmed in writing.
> 4. Confirmation of what the **date** in a rate-search request means
>    (departure / booking / validity).
> 5. Whether dimensions are expected per-piece or in total, and in which unit.
> 6. Rate limits and whether `Retry-After` is returned on throttling.
> 7. Any structured carrier-restriction vocabulary the response carries.
>
> We will not call undocumented endpoints and are not using the public Shipping
> Calculator API, which returns estimate ranges rather than quotable rates.

**Internal note for the senior:** items 3 and 4 are the two that block
implementation. Items 1, 2, 5, 6, 7 shape the adapter but do not block the
business logic, which is already complete and tested.

## 7. Status legend applied

| Component | Status |
|---|---|
| Rate pipeline: filter → rank → select | **REAL**, deterministic, fully tested |
| Rate data in the demo and POC | **MOCK** — labelled `mock-webcargo` in output |
| Which Freightos product is correct | **OFFICIAL DOCUMENTATION** (Phase 10.1) |
| WebCargo auth, endpoints, schemas | **UNKNOWN / BLOCKED** — partner-gated |
| Transit-time semantics (AMB-1) | **UNKNOWN / BLOCKED** — fails closed today |
| Date semantics (AMB-8) | **UNKNOWN / BLOCKED** — caller must state the date |
| Carrier restrictions (AMB-3) | **UNKNOWN / BLOCKED** — only liquids documented |

## 8. Change made in this phase

One test file, `tests/unit/test_rate_provider_mode.py` (7 tests). The audit
found that `bootstrap.build_rate_provider` — the single point that chooses mock
or real — had no direct test. It now proves: an unconfigured checkout gets the
mock provider; mock needs no credentials; real mode is reachable only by
explicitly setting `TRANSLOG_WEBCARGO__MODE=real`; real mode refuses at search
rather than inventing rates; and the refusal message contains no URL, endpoint
hostname, or credential.

No production code was changed.
