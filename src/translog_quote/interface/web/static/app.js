/* Translog Express — client-facing POC.
 *
 * Rendering rules, deliberately strict:
 *  - every node is built with createElement/textContent — no innerHTML, ever;
 *  - the only network calls are same-origin /api/state and /api/action/<name>;
 *  - the browser holds presentation state only (which view, what is revealed);
 *    every business fact comes from the server snapshot, which the backend
 *    gates step by step.
 */
"use strict";

const STEP_RANK = {
  enquiry_processed: 0,
  clarification_approved: 1,
  reply_processed: 2,
  rates_searched: 3,
  quotation_acknowledged: 4,
};

/* Motion is decoration here, never information: every state is also shown in
   text and chips, and reduced-motion viewers get the same content instantly. */
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const ui = {
  snap: null,
  view: "dashboard",
  draftRevealed: false,
  draftEditing: false,
  draftText: null,
  quotationRevealed: false,
  busy: false,
  error: null,
  revealTarget: null,
  seenSections: new Set(),
  extractionSettled: false,
  extractionTimer: null,
  processingAnimated: false,
  ratesAnimated: false,
};

function resetMotionState() {
  ui.seenSections.clear();
  ui.extractionSettled = false;
  if (ui.extractionTimer) clearTimeout(ui.extractionTimer);
  ui.extractionTimer = null;
  ui.processingAnimated = false;
  ui.ratesAnimated = false;
}

function flyLetter(anchor, mode) {
  if (REDUCED || !anchor || !anchor.getBoundingClientRect) return;
  const rect = anchor.getBoundingClientRect();
  const glyph = document.createElement("span");
  glyph.className = `fx-letter ${mode}`;
  glyph.textContent = "✉";
  glyph.setAttribute("aria-hidden", "true");
  glyph.style.left = `${Math.round(rect.left + rect.width / 2)}px`;
  glyph.style.top = `${Math.round(rect.top)}px`;
  glyph.addEventListener("animationend", () => glyph.remove());
  document.body.appendChild(glyph);
}

/* ------------------------------------------------------------- utilities */

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null) continue;
      if (key === "class") node.className = value;
      else if (key === "onClick") node.addEventListener("click", value);
      else if (key === "onInput") node.addEventListener("input", value);
      else node.setAttribute(key, value);
    }
  }
  const append = (child) => {
    if (child == null) return;
    if (Array.isArray(child)) child.forEach(append);
    else if (child instanceof Node) node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  };
  children.forEach(append);
  return node;
}

function rank() {
  return ui.snap ? STEP_RANK[ui.snap.step] : 0;
}

function fmtMoney(amount, currency) {
  if (amount == null) return "—";
  const value = Number(amount);
  if (currency === "INR" && Number.isFinite(value)) {
    const fraction = value % 1 === 0 ? 0 : 2;
    return new Intl.NumberFormat("en-IN", {
      style: "currency",
      currency: "INR",
      minimumFractionDigits: fraction,
      maximumFractionDigits: fraction,
    }).format(value);
  }
  return currency ? `${amount} ${currency}` : String(amount);
}

function field(name) {
  const source = ui.snap.merged ? ui.snap.merged.shipment : ui.snap.shipment;
  return source.find((f) => f.field === name) || null;
}

function fieldValue(name) {
  const found = field(name);
  return found && found.value != null ? found.value : "—";
}

/* -------------------------------------------------------------- network */

async function refresh() {
  try {
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    ui.snap = await response.json();
    ui.error = null;
    // A page opened mid-demo (or reloaded) skips the intro pacing beat.
    if (ui.snap.step !== "enquiry_processed") ui.extractionSettled = true;
  } catch (err) {
    document.getElementById("load-error").hidden = false;
    return;
  }
  document.getElementById("load-error").hidden = true;
  render();
}

async function act(name) {
  if (ui.busy) return;
  ui.busy = true;
  render();
  try {
    // Content-Type is required by the server's same-origin guard: a
    // cross-site form cannot set application/json without a preflight the
    // server refuses, which is what turns away CSRF.
    const response = await fetch(`/api/action/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) {
      ui.error = payload.error || `Action failed (HTTP ${response.status})`;
    } else {
      ui.snap = payload;
      ui.error = null;
    }
  } catch (err) {
    ui.error = "The demo server did not respond.";
  }
  ui.busy = false;
  render();
}

async function resetDemo() {
  ui.draftRevealed = false;
  ui.draftEditing = false;
  ui.draftText = null;
  ui.quotationRevealed = false;
  ui.view = "dashboard";
  resetMotionState();
  await act("reset");
}

function reveal(id) {
  ui.revealTarget = id;
}

/* ------------------------------------------------------------ fragments */

function pill(text, tone) {
  return el("span", { class: `pill pill-${tone}` }, text);
}

function statusPill() {
  const snap = ui.snap;
  if (snap.quotation_acknowledgement) return pill("QUOTATION APPROVED — SIMULATED", "green");
  const byState = {
    needs_info: ["INFORMATION REQUIRED", "amber"],
    clarification_sent: ["AWAITING CLIENT REPLY", "blue"],
    validated: ["VALIDATED", "green"],
    rate_selected: ["RATE SELECTED", "green"],
    no_eligible_rate: ["NO ELIGIBLE RATE", "red"],
  };
  const [text, tone] = byState[snap.request_state] || [snap.request_state.toUpperCase(), "gray"];
  return pill(text, tone);
}

function chip(status) {
  const map = {
    known: ["KNOWN", "chip-known"],
    missing: ["MISSING", "chip-missing"],
    ambiguous: ["AMBIGUOUS", "chip-ambiguous"],
    denied: ["DECLINED", "chip-ambiguous"],
    not_required: ["—", "chip-neutral"],
  };
  const [text, cls] = map[status] || [status, "chip-neutral"];
  return el("span", { class: `chip ${cls}` }, text);
}

function emailPanel(email, extraMeta) {
  return el(
    "div",
    { class: "email-panel" },
    el(
      "div",
      { class: "email-meta" },
      el("span", null, "From: ", el("strong", null, email.from)),
      extraMeta || null,
      el("span", null, "Subject: ", el("strong", null, email.subject))
    ),
    el("pre", { class: "email-body" }, email.body_text)
  );
}

function card(id, headChildren, ...body) {
  const isNew = !ui.seenSections.has(id);
  ui.seenSections.add(id);
  return el(
    "article",
    { class: isNew && !REDUCED ? "card enter" : "card", id },
    el("div", { class: "card-head" }, ...headChildren),
    ...body
  );
}

function actionRow(...children) {
  return el("div", { class: "card-actions" }, ...children);
}

function button(label, kind, onClick, disabled) {
  return el(
    "button",
    { class: `btn btn-${kind}`, type: "button", onClick, disabled: disabled || ui.busy ? "" : null },
    label
  );
}

/* ------------------------------------------------------------ dashboard */

function renderDashboard() {
  const snap = ui.snap;
  const holder = document.getElementById("dashboard-card");
  holder.replaceChildren(
    el(
      "article",
      { class: "card enquiry-card" },
      el(
        "div",
        { class: "enquiry-card-top" },
        el(
          "div",
          null,
          el("p", { class: "eyebrow" }, `New enquiry · ${snap.request_id}`),
          el("h2", null, snap.client.name),
          el("p", { class: "muted" }, snap.client.company)
        ),
        statusPill()
      ),
      el(
        "div",
        { class: "route" },
        el("span", null, fieldValue("origin")),
        el("span", { class: "arrow" }, "→"),
        el("span", null, fieldValue("destination"))
      ),
      el(
        "dl",
        { class: "facts" },
        fact("Weight", fieldValue("weight_kg")),
        fact("Dimensions", fieldValue("dimensions_in")),
        fact("Cargo type", fieldValue("cargo_type")),
        fact("Received", new Date(snap.enquiry.received_at).toLocaleString())
      ),
      actionRow(button("View Enquiry", "primary", () => {
        ui.view = "detail";
        window.scrollTo(0, 0);
        render();
      }))
    )
  );
}

function fact(label, value) {
  return el("div", null, el("dt", null, label), el("dd", null, value));
}

/* --------------------------------------------------------------- stepper */

function renderStepper() {
  const r = rank();
  const steps = [
    ["Enquiry", true, false],
    ["AI Extraction", true, false],
    ["Validation", true, false],
    ["Clarification", r >= 1, r === 0],
    ["Client Reply", r >= 2, r === 1],
    ["Revalidation", r >= 2, false],
    ["Rate Options", r >= 3, r === 2],
    ["Quotation", r >= 4, r === 3],
  ];
  document.getElementById("stepper").replaceChildren(
    ...steps.map(([label, done, current]) =>
      el(
        "li",
        { class: done ? "done" : current ? "current" : null },
        el("span", { class: "dot", "aria-hidden": "true" }, done ? "✓" : ""),
        label
      )
    )
  );
}

/* -------------------------------------------------------------- sections */

function sectionEnquiry() {
  const snap = ui.snap;
  return card(
    "sec-enquiry",
    [el("h2", null, "Client Enquiry"), pill("RECEIVED BY EMAIL", "gray")],
    emailPanel(snap.enquiry)
  );
}

function sectionPipelineStatus() {
  const snap = ui.snap;
  const validation = ui.snap.merged ? ui.snap.merged.validation : snap.validation;
  const ok = validation.is_valid;

  // The extraction already happened server-side; this brief "analyzing" beat
  // is presentation pacing only, and reduced motion skips it entirely.
  if (!ui.extractionSettled) {
    return card(
      "sec-pipeline",
      [el("h2", null, "Processing")],
      el(
        "div",
        { class: "status-lines" },
        el(
          "div",
          { class: "status-line" },
          el("span", { class: "mark run" }, "●"),
          el("strong", null, "AI Extraction"),
          el("span", { class: "muted" }, "Analyzing enquiry"),
          el("span", { class: "dots", "aria-hidden": "true" },
            el("span", null, "●"), el("span", null, "●"), el("span", null, "●"))
        )
      ),
      el(
        "p",
        { class: "small muted" },
        "Extraction reads what the client wrote. Whether the shipment can be quoted is decided by fixed business rules, not by the model."
      )
    );
  }

  const firstReveal = !ui.processingAnimated && !REDUCED;
  ui.processingAnimated = true;
  return card(
    "sec-pipeline",
    [el("h2", null, "Processing")],
    el(
      "div",
      { class: "status-lines" },
      el(
        "div",
        { class: firstReveal ? "status-line fade-line" : "status-line" },
        el("span", { class: "mark ok" }, "✓"),
        el("strong", null, "AI Extraction"),
        el("span", { class: "muted" },
          `Completed — ${snap.extraction.stated} of ${snap.extraction.assessed} fields stated by the enquiry`)
      ),
      el(
        "div",
        { class: firstReveal ? "status-line fade-line" : "status-line" },
        el("span", { class: `mark ${ok ? "ok" : "warn"}` }, ok ? "✓" : "⚠"),
        el("strong", null, "Validation"),
        el("span", { class: "muted" }, ok ? "All required information present" : "Information required")
      )
    ),
    el(
      "p",
      { class: "small muted" },
      "Extraction reads what the client wrote. Whether the shipment can be quoted is decided by fixed business rules, not by the model."
    )
  );
}

function shipmentTable(fields, withSource) {
  const header = el(
    "tr",
    null,
    el("th", null, "Field"),
    el("th", null, "Value"),
    el("th", null, "Status"),
    withSource ? el("th", null, "Source") : el("th", null, "Evidence from email")
  );
  const rows = fields.map((f) => {
    const absent = f.value == null;
    const last = withSource
      ? el(
          "td",
          null,
          f.source === "reply"
            ? el("span", { class: "chip chip-source" }, "CLIENT REPLY")
            : f.source === "enquiry"
              ? el("span", { class: "chip chip-neutral" }, "ENQUIRY")
              : ""
        )
      : el("td", { class: "evidence" }, f.evidence ? `“${f.evidence}”` : f.note || "");
    return el(
      "tr",
      null,
      el("td", { class: "field-label" }, f.label),
      el("td", { class: `field-value${absent ? " absent" : ""}` },
        absent ? (f.status === "missing" ? "— not provided" : "—") : f.value),
      el("td", null, chip(f.status)),
      last
    );
  });
  return el("div", { class: "table-wrap" },
    el("table", null, el("thead", null, header), el("tbody", null, ...rows)));
}

function sectionShipment() {
  const snap = ui.snap;
  return card(
    "sec-shipment",
    [el("h2", null, "Extracted Shipment Information"), statusPillForShipment(snap.validation)],
    shipmentTable(snap.shipment, false)
  );
}

function statusPillForShipment(validation) {
  return validation.is_valid ? pill("COMPLETE", "green") : pill("INCOMPLETE", "amber");
}

function sectionMissing() {
  const snap = ui.snap;
  if (snap.validation.is_valid || snap.missing.length === 0) return null;
  const showButton = !ui.draftRevealed && rank() === 0;
  const resolved = snap.merged && snap.merged.validation.is_valid;
  return card(
    "sec-missing",
    [el("h2", null, "Information Required"),
     resolved ? pill("RESOLVED BY CLIENT REPLY", "green") : pill(`${snap.missing.length} ITEMS`, "amber")],
    el(
      "ul",
      { class: "missing-list" },
      ...snap.missing.map((m) =>
        el("li", null, el("strong", null, m.title), el("div", { class: "why" }, m.question))
      )
    ),
    showButton
      ? actionRow(
          button("Generate Clarification Draft", "primary", () => {
            ui.draftRevealed = true;
            reveal("sec-clarification");
            render();
          }),
          el("span", { class: "action-note" }, "Drafted for review — nothing is sent without approval.")
        )
      : null
  );
}

function sectionClarification() {
  const snap = ui.snap;
  const clar = snap.clarification;
  if (!clar) return null;
  if (!ui.draftRevealed && rank() === 0) return null;

  const approved = clar.status === "approved";
  const bodyText = ui.draftText != null ? ui.draftText : clar.body_text;

  const head = [
    el("h2", null, "Clarification ", approved ? pill("APPROVED", "green") : pill("DRAFT", "amber")),
    approved
      ? pill("RELEASED TO OUTBOX — NO EMAIL SENT", "gray")
      : el("div", { class: "quote-flags" },
          pill("NOT SENT", "gray"),
          pill("REQUIRES HUMAN APPROVAL", "amber")),
  ];

  const meta = el("span", null, "To: ", el("strong", null, clar.to));
  const panel = ui.draftEditing
    ? el(
        "div",
        { class: "email-panel" },
        el("div", { class: "email-meta" }, meta, el("span", null, "Subject: ", el("strong", null, clar.subject))),
        el("textarea", {
          class: "email-body",
          onInput: (event) => { ui.draftText = event.target.value; },
        }, bodyText)
      )
    : emailPanel({ from: "Translog Express (draft)", subject: clar.subject, body_text: bodyText }, meta);

  const children = [panel];

  if (!approved) {
    children.push(
      actionRow(
        button(ui.draftEditing ? "Done Editing" : "Edit Draft", "secondary", () => {
          ui.draftEditing = !ui.draftEditing;
          render();
        }),
        button("Approve & Send", "approve", (event) => {
          flyLetter(event.currentTarget, "out");
          act("approve-clarification");
        }),
        el("span", { class: "action-note" },
          "Simulated send — this build has no email sender. Approval releases the draft to an internal outbox only.")
      )
    );
    if (ui.draftText != null && ui.draftText !== clar.body_text) {
      children.push(el("p", { class: "small muted" },
        "Draft edits are local to this demonstration preview."));
    }
  } else {
    children.push(
      el(
        "div",
        { class: "banner banner-green" },
        "✓ Approved by a human operator",
        el("span", { class: "sub" },
          `${clar.approved_by} — no email was sent; no sender exists in this build.`)
      )
    );
    if (rank() === 1) {
      children.push(
        actionRow(
          button("Show Client Reply", "primary", (event) => {
            flyLetter(event.currentTarget, "in");
            reveal("sec-reply");
            act("receive-reply");
          }),
          el("span", { class: "action-note" }, "Simulated inbound email — no mailbox is connected.")
        )
      );
    }
  }
  return card("sec-clarification", head, ...children);
}

function sectionReply() {
  const snap = ui.snap;
  if (!snap.reply) return null;
  return card(
    "sec-reply",
    [el("h2", null, "Client Reply"), pill("SIMULATED DEMO INPUT", "gray")],
    emailPanel(snap.reply)
  );
}

function sectionMerged() {
  const snap = ui.snap;
  if (!snap.merged) return null;
  const merged = snap.merged;
  const children = [
    shipmentTable(merged.shipment, true),
    el("p", { class: "small muted" },
      `Carried from the original enquiry: ${merged.carried.join(", ")}. ` +
      `Supplied by the client reply: ${merged.supplied.join(", ")}.`),
  ];
  if (merged.validation.is_valid) {
    children.push(
      el("div", { class: "banner banner-green" }, "✓ VALIDATED",
        el("span", { class: "sub" }, "All required shipment information is present."))
    );
    if (rank() === 2) {
      children.push(
        actionRow(
          button("Search Rates", "primary", () => {
            reveal("sec-rates");
            act("search-rates");
          }),
          el("span", { class: "action-note" }, "Demo data — WebCargo integration not connected.")
        )
      );
    }
  } else {
    children.push(
      el("div", { class: "banner banner-amber" }, "⚠ Still incomplete",
        el("span", { class: "sub" }, "A quotation is not produced from an incomplete shipment."))
    );
  }
  return card(
    "sec-merged",
    [el("h2", null, "Updated Shipment"), merged.validation.is_valid ? pill("VALIDATED", "green") : pill("INCOMPLETE", "amber")],
    ...children
  );
}

function sectionRates() {
  const snap = ui.snap;
  if (!snap.rates) return null;
  const rates = snap.rates;

  const header = el("tr", null,
    el("th", null, "Carrier"),
    el("th", null, "Service"),
    el("th", null, "Transit"),
    el("th", { class: "num" }, "Air Freight"),
    el("th", null, ""));
  const rows = rates.eligible.map((r) =>
    el("tr", { class: r.recommended ? "row-recommended" : null },
      el("td", { class: "field-label" }, `${r.carrier_name} (${r.carrier_code})`),
      el("td", null, r.product),
      el("td", null, r.transit),
      el("td", { class: "num" }, fmtMoney(r.amount, r.currency)),
      el("td", null,
        r.recommended ? el("span", { class: "chip chip-reco reco-chip" }, "FASTEST ELIGIBLE") : "")));

  // First appearance: rows stagger in, then the backend's selection is
  // highlighted. The class flip runs on the live table so the highlight is a
  // transition, and the decision itself is always in the row's text chip.
  let tableClass = "rates-table settled";
  if (!ui.ratesAnimated) {
    ui.ratesAnimated = true;
    if (!REDUCED) {
      tableClass = "rates-table reveal";
      setTimeout(() => {
        const table = document.querySelector("#sec-rates table");
        if (table) table.classList.add("settled");
      }, 900);
    }
  }

  const children = [
    el("p", { class: "card-sub" },
      `Search: ${rates.query.origin_iata} → ${rates.query.destination_iata} · ` +
      `${rates.query.weight_kg} kg · ${rates.query.date} · ` +
      `${rates.returned} rates returned, ${rates.eligible.length} eligible`),
    el("div", { class: "table-wrap" },
      el("table", { class: tableClass }, el("thead", null, header), el("tbody", null, ...rows))),
  ];

  if (rates.excluded.length) {
    children.push(
      el("details", { class: "excluded" },
        el("summary", null, `Excluded rates (${rates.excluded.length}) — with reasons`),
        el("ul", null, ...rates.excluded.map((e) =>
          el("li", null, `${e.carrier_name} (${e.carrier_code}): ${e.detail || e.reason}`)))));
  }
  return card(
    "sec-rates",
    [el("h2", null, "Rate Options"), pill("DEMO DATA — WEBCARGO NOT CONNECTED", "amber")],
    ...children
  );
}

function sectionRecommended() {
  const snap = ui.snap;
  if (!snap.rates || !snap.rates.selection) return null;
  const selection = snap.rates.selection;
  const rate = selection.rate;

  const children = [
    el("div", { class: "reco" },
      el("div", null,
        el("p", { class: "eyebrow" }, "Recommended"),
        el("p", { class: "reco-carrier" }, `${rate.carrier_name} (${rate.carrier_code})`),
        el("p", { class: "reco-meta" }, `${rate.product} · ${rate.transit} transit`)),
      el("div", null,
        el("p", { class: "reco-price" }, fmtMoney(rate.amount, rate.currency),
          el("span", { class: "cur" }, rate.currency || "")))),
    el("p", { class: "reco-reason" }, el("strong", null, "Reason: "), selection.reason),
    el("p", { class: "small muted" }, snap.rates.strategy),
    el("ul", { class: "ranking" },
      ...selection.ranking.map((r, index) =>
        el("li", null,
          el("span", { class: "t" }, r.transit),
          `${r.carrier_name} — ${fmtMoney(r.amount, r.currency)}`,
          index === 0 ? el("span", { class: "chip chip-known" }, "SELECTED") : ""))),
  ];

  if (rank() === 3 && !ui.quotationRevealed) {
    children.push(
      actionRow(
        button("Prepare Quotation Preview", "primary", () => {
          ui.quotationRevealed = true;
          reveal("sec-quotation");
          render();
        })
      )
    );
  }
  return card("sec-recommended", [el("h2", null, "Recommended Rate")], ...children);
}

function sectionQuotation() {
  const snap = ui.snap;
  if (!snap.quotation) return null;
  if (!ui.quotationRevealed && rank() === 3) return null;
  const quote = snap.quotation;
  const ack = snap.quotation_acknowledgement;

  const children = [
    el("p", { class: "card-sub" }, `Quotation reference: ${quote.reference} · ` +
      `Client: ${snap.client.name}, ${snap.client.company}`),
    el("div", { class: "quote-grid" },
      el("div", null,
        el("h3", null, "Shipment"),
        el("dl", { class: "kv" },
          ...quote.shipment_rows.flatMap((row) => [el("dt", null, row.label), el("dd", null, row.value)]))),
      el("div", null,
        el("h3", null, "Selected Service"),
        el("dl", { class: "kv" },
          ...quote.rate_rows.flatMap((row) => [el("dt", null, row.label), el("dd", null, row.value)])),
        el("p", { class: "small muted" },
          `Not included in this POC: ${quote.unspecified.join(", ")}.`))),
  ];

  if (!ack) {
    children.push(
      actionRow(
        button("Approve Quotation", "approve", () => act("approve-quotation")),
        el("span", { class: "action-note" },
          "Simulated — approval is recorded for the demonstration; quotation dispatch is not built and nothing is sent.")
      )
    );
  } else {
    children.push(
      el("div", { class: "banner banner-green" },
        "✓ Quotation approved — simulated",
        el("span", { class: "sub" }, `${ack.by}. ${ack.note}`))
    );
  }

  const flags = ack
    ? quote.flags.map((flag) => (flag === "NOT APPROVED" ? "APPROVED — SIMULATED" : flag))
    : quote.flags;
  return card(
    "sec-quotation",
    [el("h2", null, "Quotation Preview"),
     el("div", { class: "quote-flags" },
       ...flags.map((flag) =>
         pill(flag,
           flag === "APPROVED — SIMULATED" ? "green"
             : flag === "NOT SENT" || flag === "NOT APPROVED" ? "gray" : "amber")))],
    ...children
  );
}

function sectionComplete() {
  const snap = ui.snap;
  if (!snap.quotation_acknowledgement) return null;
  const rows = [
    ["AI extraction", "Scripted for this demo — mirrors the live model output for this scenario"],
    ["Validation", "Real — deterministic business rules"],
    ["Clarification wording", "Real — deterministic templates"],
    ["Human approval gates", "Real — nothing advanced without the operator's click"],
    ["Merge & revalidation", "Real — deterministic"],
    ["Rate data", "Demo data — WebCargo integration not connected"],
    ["Email & quotation dispatch", "Not built — nothing was sent"],
  ];
  return card(
    "sec-complete",
    [el("h2", null, "Demonstration Complete"), pill("POC", "blue")],
    el("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [el("dt", null, k), el("dd", null, v)]))
  );
}

/* --------------------------------------------------------------- render */

function renderDetail() {
  const snap = ui.snap;
  document.getElementById("detail-header").replaceChildren(
    el("div", { class: "card-head" },
      el("div", null,
        el("p", { class: "eyebrow" }, `Enquiry ${snap.request_id}`),
        el("h1", null, `${snap.client.company} — ${fieldValue("origin")} → ${fieldValue("destination")}`),
        el("p", { class: "muted small" }, snap.client.address)),
      statusPill())
  );
  renderStepper();

  if (!ui.extractionSettled) {
    if (REDUCED) {
      ui.extractionSettled = true;
    } else if (!ui.extractionTimer) {
      ui.extractionTimer = setTimeout(() => {
        ui.extractionSettled = true;
        render();
      }, 1100);
    }
  }

  const sections = ui.extractionSettled
    ? [
        sectionEnquiry(),
        sectionPipelineStatus(),
        sectionShipment(),
        sectionMissing(),
        sectionClarification(),
        sectionReply(),
        sectionMerged(),
        sectionRates(),
        sectionRecommended(),
        sectionQuotation(),
        sectionComplete(),
      ].filter(Boolean)
    : [sectionEnquiry(), sectionPipelineStatus()];

  if (ui.error) {
    sections.unshift(el("div", { class: "banner banner-amber" }, "⚠ ", ui.error));
  }
  document.getElementById("sections").replaceChildren(...sections);
}

function render() {
  if (!ui.snap) return;
  const dashboard = document.getElementById("view-dashboard");
  const detail = document.getElementById("view-enquiry");
  dashboard.hidden = ui.view !== "dashboard";
  detail.hidden = ui.view !== "detail";
  if (ui.view === "dashboard") renderDashboard();
  else renderDetail();

  if (ui.revealTarget) {
    const target = document.getElementById(ui.revealTarget);
    ui.revealTarget = null;
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

/* ----------------------------------------------------------------- init */

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-back").addEventListener("click", () => {
    ui.view = "dashboard";
    render();
  });
  document.getElementById("btn-reset").addEventListener("click", resetDemo);
  refresh();
});
