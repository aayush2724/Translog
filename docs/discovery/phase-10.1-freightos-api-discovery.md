# Phase 10.1 — Freightos / WebCargo API discovery

**Date:** 2026-08-27 · **Status:** discovery only — no application code changed
**Question:** which official Freightos API should Translog call for real air-cargo
rates, how is it authenticated, what do request/response look like, and can it
feed the existing deterministic rate-selection pipeline?

---

## A. The correct official API

**WebCargo Booking API** — the partner-gated operational surface of the
**Freight-as-a-Service (FaaS)** program (WebCargo has been renamed
"Freightos for Forwarders"). Official material describes it as: real-time
door-to-door or port-to-port freight rates, **transit time**, and live bookable
capacity on flights, across 55+ carriers, for forwarder / carrier / TMS
partners under commercial agreements.

Every other Freightos API is the wrong product for quotation rates (see D).

## B. Official documentation references

| Source | What it is |
|---|---|
| `developer.freightos.com` / `developers.freightos.com` | Freightos Developer Portal. Public docs cover the estimator/utility APIs; portal APIs are marked beta, "as-is". |
| `developer.freightos.com/getting-started-freightos-integrations` | Entry point for partner integrations (WebCargo Booking, shipment management). Full references are provisioned per partner, not public. |
| `apidocs.freightos.com` | **Freightos Terminal** — market-data APIs (FAX/FBX indices, price stats, transit-time statistics). |
| `ship.freightos.com/api/shippingCalculator` | Public Shipping Calculator API (estimates). |
| `freightos.com/freightos-freight-as-a-service-for-b2b-global-logistics/` | FaaS program description and application form. |

The detailed WebCargo Booking API reference (endpoints, schemas) is **not
publicly published**; Freightos provisions it with partner access.

## C. Authentication (as documented)

- **Freightos Terminal** (market data): two request headers, `apikey` +
  `secret-key`, against `https://api.freightos.com/fd_external_apis`.
- **Shipping Calculator**: optional `apiKey` query parameter (public
  marketplace estimates need none — and a credential in a URL is a pattern we
  will not adopt for anything sensitive).
- **WebCargo Booking API / FaaS**: mechanism **not publicly documented**;
  credentials and auth model are issued during partner onboarding. UNKNOWN
  until Freightos provides the partner reference.

## D. Do not confuse the APIs (§11)

| API | Returns | Verdict for quotation rates |
|---|---|---|
| Shipping Calculator | price *ranges* (min/max) + transit *ranges*, from marketplace estimates | **Not usable** — estimates, not firm quotable rates |
| Freight Estimator (developer portal) | instant estimates from live marketplace + historical database | **Not usable** — same reason; also beta/as-is |
| Freightos Terminal FAX | `date` + `value` index aggregates (global/region/airline level) | **Not usable** — market intelligence, not quotes |
| **WebCargo Booking API (FaaS)** | live carrier rates, transit time, bookable capacity | **The correct product** — contract pending partner access |

The DevTools-captured `webcargonet.com` endpoints in the frozen specification
remain out of scope: not a published API, and Phase 7's refusal stands.

## E. Field mapping — existing domain vs. what is established

"UNKNOWN" means: not establishable from public official documentation; needs
the partner API reference. No domain change is made or proposed yet.

| Existing field | Freightos evidence | Conversion? | Ambiguity |
|---|---|---|---|
| `RateQuery.origin_iata` / `destination_iata` | Calculator accepts 3-letter IATA codes; air product is airport-based | likely none | exact field names UNKNOWN |
| `RateQuery.weight_kg` (single canonical weight) | Calculator takes **per-unit** weight (kg) | per-unit vs total to reconcile | gross vs chargeable semantics UNKNOWN (see F) |
| `RateQuery.dimensions_in` (inches) | Calculator takes width/length/height, **cm default**, inch supported, per load unit | unit declaration + per-piece shape | Booking API shape UNKNOWN |
| `pcs` | Calculator `quantity` per load type | — | package-type concept exists (boxes/pallets/crates); ours has none |
| `RateQuery.date` | FaaS prices live flight capacity → a date is clearly involved | — | AMB-8 unchanged: no approved source for the date; API semantics UNKNOWN |
| `Rate.carrier_code` / `carrier_name` | "across 55+ carriers", airline-level eBooking | — | field names UNKNOWN |
| `Rate.product` (service) | service tiers exist in WebCargo air product | — | UNKNOWN |
| `Rate.total_amount` / `currency` | FaaS advertises real-time bookable pricing | Decimal parse | firm-vs-estimate flag UNKNOWN |
| `TransitTime` (value + unit) | FaaS explicitly advertises **transit time** in rate responses; Terminal has transit-time *statistics*; estimators return min/max *ranges* | shape mapping | **AMB-1 likely resolvable, unresolved**: unit, estimated-vs-guaranteed, and segment structure UNKNOWN. A min/max range would not fit the single-value `TransitTime` without a stated rule. |
| `RateRestrictions.accepts_liquids` | WebCargo supports DGR/perishable/special cargo → eligibility concepts exist | — | AMB-3 unchanged: actual restriction flags UNKNOWN |
| delivery type (`door`/`airport`) | FaaS: "door to door or port-to-port"; calculator: address input = door, port/airport code = port | — | Incoterms handling UNKNOWN; **no Incoterms mapping will be invented** (DAP/DDP/CIF/FCA/EXW are not delivery types) |
| commodity / `cargo_type` | special-cargo booking implies commodity/DG data | — | contract UNKNOWN |

## F. Unresolved fields (blocking questions for the partner reference)

1. **Transit time (AMB-1)** — exact response field, unit, single value vs
   range, estimated vs guaranteed, per-segment vs total.
2. **Weight semantics** — gross vs volumetric vs chargeable; whether the API
   computes chargeable weight from dimensions or expects it. Our single
   `weight_kg` stays untouched until this is answered.
3. **Search date (AMB-8)** — what the date parameter means (departure?
   earliest availability?) and which business source states it.
4. **Restrictions (AMB-3)** — which eligibility flags the response carries.
5. **Delivery scope** — how door vs airport is expressed; whether Incoterms
   appear at all.
6. **Pagination, rate limits, error schema, environments** — all UNKNOWN.

## G. What Freightos needs to provide (we are building this for them)

1. Confirmation that **WebCargo Booking API / FaaS** is the intended rate
   source for this product.
2. **Partner API access** for Translog: application approved, credentials
   issued (delivered as configuration — `TRANSLOG_WEBCARGO__*` settings
   already exist, empty).
3. The **partner API reference**: base URLs, auth mechanism, rate-search
   request/response schemas, error schema, rate limits, pagination.
4. A **sandbox/test environment** and an approved context for one minimal
   smoke request.
5. Written answers to the six unresolved-field questions in F.
6. Explicit written scope of authorization (which operations, which account).

## H. Security requirements before production integration

- Credentials only via the existing `pydantic-settings` `SecretStr` fields;
  never source code, never URLs, never logs, never exceptions, never the
  browser, never prompts.
- TLS-only base URLs, fixed in configuration — no user-influenced URLs
  (SSRF is structurally prevented by config-only destinations).
- Treat every response as untrusted: schema-validate before it becomes a
  `Rate` (the `RealRateMapper` pattern — refuse, never substitute).
- Rate data never enters an LLM prompt (extraction remains the only model
  boundary — keep that structural).
- Bounded retries with the existing exponential backoff + `Retry-After`
  handling; never retry auth failures indefinitely; respect 429s.
- Log operation, timing, request id, and failure *category* only — no keys,
  no payload bodies with client data, no provider-error credential echoes.
- Key rotation procedure agreed with Freightos before go-live.

## I. Smallest implementation once the contract arrives

One adapter, zero domain changes (unless the verified contract forces a
mismatch — which gets reported first):

1. Extend `WebCargoSettings` with the credential fields the partner auth
   actually uses (SecretStr).
2. Implement `FreightosWebCargoAdapter` (satisfying the existing
   `RateSearchPort`) in `adapters/webcargo/`, reusing the retry/backoff
   transport pattern from the OpenRouter adapter.
3. Complete `RealRateMapper.map_transit` against the verified transit field —
   deleting the AMB-1 refusal only when the answer exists.
4. Wire it in `bootstrap.build_rate_provider` behind the existing
   `WebCargoMode.REAL` switch — demo mode stays the default; real mode
   activates only by explicit configuration.
5. Offline contract tests from recorded/documented fixtures first (malformed
   rows, missing fields, 429, timeout, auth failure), then one approved
   sandbox smoke request.

Filtering, ranking, and selection stay exactly where they are — the provider
only ever supplies data.
