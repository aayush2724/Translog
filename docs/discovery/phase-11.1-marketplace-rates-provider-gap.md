# Phase 11.1 — Freightos Marketplace Rates Provider API: integration gap

**Date:** 2026-08-28 · **Spec analysed:** `freightos-marketplace-apis.yaml`
(OpenAPI 3.0.3, `Freightos Marketplace Rates Provider`, v1.0, 291 KB — currently
in `~/Downloads`, not in the repository)
**Verdict:** **Wrong direction.** This spec makes Translog a rate *supplier* to
Freightos. It contains no way for Translog to *obtain* rates. The Phase 11
blocker is unchanged.

---

## 1. Confirmed: this is a provider-side callback API

The spec says so in its own words, in several places:

| Where | What it says |
|---|---|
| `info.title` | "Freightos Marketplace Rates **Provider**" |
| `info.description` | "This API helps you **provide your freight rates** … **from your own rate management system to** Freightos Marketplace." |
| `/your_availability_endpoint` summary | "Initiated request **coming from** Freightos Marketplace" |
| same, description | "Freightos Marketplace **will trigger** one or more requests **to your designated endpoint service** with every search performed by shippers … This request normally expects **rates returned from your system** to show to the shipper." |
| `servers` on that path | `https://your_endpoint_domain/` — **we** host it |

The data flows Freightos → us (an `AvailabilityRequestBody` describing a
shipper's search), and back us → Freightos (a `RatesResponseBody` containing our
rates). The "shipper" in this model is Freightos' customer, not ours.

**Implementing this would mean Translog publishes its own rates onto the
Freightos marketplace for other people to buy.** That is the opposite of the
business need, which is to obtain carrier rates in order to quote our own
clients.

## 2. No consumer-side rate search exists in this spec

Every path, with its direction:

| Path | Who hosts it | Purpose |
|---|---|---|
| `POST /your_availability_endpoint` | **us** | Freightos asks us for rates |
| `POST /your_booking_endpoint` | **us** | Freightos announces a booking to us |
| `POST /your_tracking_endpoint` | **us** | Freightos announces tracking to us |
| `POST /marketplace/shipment/booking` | Freightos | we push booking *updates* |
| `GET·POST /marketplace/shipment/{shipmentNumber}/attachments` | Freightos | attachments on an existing shipment |
| `GET·POST /marketplace/shipment/{shipmentNumber}/charges` | Freightos | charges on an existing shipment |
| `POST /marketplace/shipment/{shipmentNumber}/tracking` | Freightos | tracking on an existing shipment |

Seven paths, all accounted for. The four Freightos-hosted ones are **all keyed by
`{shipmentNumber}`** — they operate on a shipment that already exists because a
booking already happened on the marketplace. None accepts an
origin/destination/weight query, and none returns rates.

The only "Rates Response" in the document is a sample of what *we* would send,
not something we can receive.

**Answer: no.** There is no endpoint in this spec that Translog can call to
initiate a rate search.

## 3. `RealWebCargoAdapter` cannot legitimately use any endpoint here

`RateSearchPort.search(query) -> RateSearchResult` is a **client** operation: we
send a query and receive rates. Nothing in this spec offers that.

Using `/your_availability_endpoint` would not mean calling an API — it would mean
**standing up an HTTPS server** that receives shipper searches and answers them
with our own rates. That is a different product, it requires a rate source we do
not have, and it would place Translog's rates in front of Freightos' shippers.

So the adapter stays as it is: refusing with the reason. Nothing was implemented.

## 4. What is still required

Unchanged from Phase 11. We need the **consumer-side** product:

1. **WebCargo Booking API** (Freightos for Forwarders / FaaS) partner reference —
   endpoints, request/response schemas, error contract. Still partner-gated.
2. Credentials plus a **sandbox** environment.
3. **Transit-time semantics** — the AMB-1 blocker.
4. **Date semantics** — the AMB-8 blocker.

Two further observations from this spec — useful, but not sufficient:

- **No authentication is defined anywhere in it.** There is no `securitySchemes`
  block and no global `servers` block. The only auth-shaped item is an
  `authenticationType` *declaration* field (`BASIC | API_KEY | O_AUTH | X_509 |
  EXTENSION`) inside a message header, describing how a provider's own endpoint
  is secured. This document does not answer our authentication question either.
- **Freightos models transit as a range, not a scalar.** The vocabulary here is
  `connection.transitTime.estimatedTransitTimes[].from/.to`, e.g.
  `from: {value: 6, unitCode: day}, to: {value: 10, unitCode: day}`.

  Our `TransitTime` holds a single `value` + `unit`. **If** the Booking API uses
  the same vocabulary, AMB-1 acquires a second question: when ranking on speed,
  does a 6–10 day range rank on its lower bound, its upper bound, or its
  midpoint? That is a business decision, not a mapping detail. It is flagged,
  not decided — and it is not yet confirmed that the Booking API shares this
  vocabulary.

## 4b. Three findings from the full schema list

Reviewing every schema (not just the paths) turned up three things that sharpen
the ask. None of them unblocks implementation.

### `SpotQuotesResponse` is defined but unreachable

The spec defines a `SpotQuotesResponse` schema:

```
SpotQuotesResponse:
  messageHeader, businessInfo
  quotes:            array of FreightShipmentType   ← the rates payload
  paging:            SpotQuotesResponsePaging       ← nextCursor / hasMore / count
  URIReferences:     RfqURIReferences               ← RFQ = request for quote
```

**No path in the document references it.** It is an orphan: nothing produces it
and nothing consumes it.

That shape — paginated quotes, returned against an RFQ reference — is exactly
what a *consumer-side* quote search returns. Its presence is good evidence that
the capability Translog needs exists in Freightos' platform data model, and that
this particular document simply does not expose it. That makes the request far
more specific than "please send the Booking API": we can point at a schema in
their own spec and ask which API returns it.

### Transit is modelled as either a scalar or a range

```
TransitTimeType:
  time:                   MeasureType              ← a single value + unit
  estimatedTransitTimes:  [ { from, to } ]         ← "Estimated times ... (range"
```

Both are optional, so which one a provider populates is a provider choice. Our
`TransitTime` (single `value` + `unit`) maps cleanly onto `time`, and would need
a stated rule for `estimatedTransitTimes`. The AMB-1 range question stands, but
the shape is now known rather than guessed.

### Freightos distinguishes door-to-door from port-to-port transit

`ConnectionAndRatesType` carries **two** transit fields:

```
transitTime:             TransitTimeType    ← overall
portToPortTransitTime:   TransitTimeType    ← airport-to-airport leg only
```

This answers one of the four AMB-1 sub-questions directly: the vocabulary does
separate the two, so a mapping would not have to guess which one it received.

It also suggests where the choice belongs. Our canonical record already carries
`delivery_type` (`AIRPORT` vs door), which is precisely the distinction these two
fields encode — so the field to rank on would follow from what the client asked
for, rather than from an adapter-level default.

**This is a candidate mapping, not a confirmed one.** It is contingent on the
WebCargo Booking API using this same vocabulary, which is not established. And it
still does not say whether `transitTime` includes carrier handling or customs
time. `map_transit` therefore stays a refusal.

## 5. What was and was not done

- No integration code written. No endpoint invented.
- `MockWebCargoAdapter` unchanged; the demo and POC still run with no credentials.
- `RealWebCargoAdapter` unchanged; still refuses with the reason.
- No production code changed at all in this analysis.

## 6. The ask for Freightos, corrected

The previous request stands, with one clarification now that we have seen this
document:

> We have reviewed the **Freightos Marketplace Rates Provider API** and confirmed
> it is the supply-side integration: it lets a rate owner publish rates *into*
> the marketplace. That is not our use case.
>
> Translog is a freight forwarder that needs to **retrieve** carrier rates in
> order to quote its own clients. Please provide access to the consumer-side
> product — the **WebCargo Booking API** (Freightos for Forwarders / FaaS) —
> including its API reference, authentication model, and sandbox credentials.
>
> One specific pointer: the Rates Provider spec defines a `SpotQuotesResponse`
> schema — paginated `quotes` returned against an RFQ reference — but no path in
> that document produces it. That schema is the shape we need. **Which API
> returns it, and how do we get access to that one?**
>
> If Freightos' position is that rate retrieval is available only through the
> WebCargo platform under a forwarder agreement, please confirm that, so we can
> plan the commercial step rather than the technical one.

**Note for the senior:** the last paragraph matters. It is worth establishing
early whether the blocker is a document we are waiting for, or a commercial
agreement that has not been signed. Those need different actions from us.
