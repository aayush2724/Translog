/* Translog Express — live view over the real Gmail workflow.
 *
 * Rendering rules, the same strict ones the scripted POC follows:
 *  - every node is built with createElement/textContent — no innerHTML, ever;
 *  - the only network calls are same-origin /api/live/state and /api/live/<action>;
 *  - the browser holds presentation state only (which view is open, what the
 *    approver typed). Every business fact comes from the server snapshot.
 *
 * Nothing here decides anything. APPROVE and DECLINE post one explicit choice
 * and a name; the server-side QuotationStage is what sends or does not send.
 */
"use strict";

const ui = {
  snap: null,
  snapKey: null,
  view: "dashboard",
  selected: null,
  approver: "",
  declineReason: "",
  busy: false,
  busyLabel: null,
  refreshing: false,
  refreshAgain: false,
  editing: false,
  error: null,
};

/* An action — approving a clarification, deciding a quotation — sends a real
   email, so it can legitimately take a while. Without a ceiling a stalled
   request leaves the page waiting forever with no way to tell that from slow. */
const POLL_TIMEOUT_MS = 180000;

/* How often the page asks the server what it knows. This is a read of state
   the server already holds — the mailbox itself is read by the server's own
   poller — so it is cheap, and it is the whole of "the dashboard updates by
   itself": a new enquiry, a finished extraction, a merged reply and a selected
   rate all arrive through it with nothing to click. */
const REFRESH_MS = 3000;

/* ------------------------------------------------------------- utilities */

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null) continue;
      if (key === "class") node.className = value;
      else if (key === "onClick") node.addEventListener("click", value);
      else if (key === "onInput") node.addEventListener("input", value);
      else if (key === "onFocus") node.addEventListener("focus", value);
      else if (key === "onBlur") node.addEventListener("blur", value);
      else node.setAttribute(key, value);
    }
  }
  const append = (child) => {
    if (child == null || child === false) return;
    if (Array.isArray(child)) child.forEach(append);
    else if (child instanceof Node) node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  };
  children.forEach(append);
  return node;
}

/* Every time in this interface is shown in IST, because that is where the
   demonstration happens and a mixture of zones is the fastest way to make a
   real timestamp look wrong. The server sends ISO-8601 with an offset; the
   conversion is entirely here, so nothing on the backend depends on a zone. */
const IST = "Asia/Kolkata";

function fmtDate(iso) {
  if (!iso) return null;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return null;
  return when.toLocaleDateString("en-GB", {
    timeZone: IST, day: "2-digit", month: "short", year: "numeric",
  });
}

function fmtTime(iso) {
  if (!iso) return null;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return null;
  const clock = when.toLocaleTimeString("en-US", {
    timeZone: IST, hour: "2-digit", minute: "2-digit", hour12: true,
  });
  return `${clock} IST`;
}

/* "29 Aug 2026, 03:42 PM IST" — one line, for inline use. */
function fmtStamp(iso) {
  const date = fmtDate(iso);
  return date ? `${date}, ${fmtTime(iso)}` : null;
}

function pill(text, tone) {
  return el("span", { class: `pill pill-${tone}` }, text);
}

function card(headChildren, ...body) {
  return el(
    "article",
    { class: "card" },
    el("div", { class: "card-head" }, ...headChildren),
    el("div", { class: "card-body" }, ...body)
  );
}

function kv(rows) {
  return el(
    "dl",
    { class: "kv" },
    ...rows.flatMap(([label, value]) => [
      el("dt", null, label),
      el("dd", null, value == null || value === "" ? "—" : value),
    ])
  );
}

/* Whether a decision may be taken right now. One predicate, so the button's
   initial state and its state after every keystroke are decided by the same
   rule and cannot drift apart. */
function canDecide() {
  return !ui.busy && ui.approver.trim().length > 0;
}

/* The approver field, and the controls whose availability depends on it.

   Re-rendering on every keystroke would rebuild the input and throw the caret
   away mid-word, which is why the handler originally did not do it — but then
   nothing recomputed the buttons either, so a typed name never enabled
   anything and the button stayed disabled forever. Updating the dependent
   controls directly is what makes both true at once: the field keeps focus,
   and the buttons track what is actually in it. */
function approverField(...dependents) {
  const sync = () => dependents.forEach((control) => {
    control.disabled = !canDecide();
  });
  /* The auto-refresh rebuilds this view, and rebuilding an input the operator
     is typing into takes the caret with it. So the field says when it is in
     use and the refresh loop leaves the page alone until it is not. Nothing is
     lost by waiting: the only thing that could change underneath is a client
     reply, and it will still be there a few seconds later. */
  const field = el("input", {
    class: "approver-input",
    type: "text",
    value: ui.approver,
    placeholder: "Your name, for the record",
    onFocus: () => { ui.editing = true; },
    onBlur: () => { ui.editing = false; },
    onInput: (event) => {
      ui.approver = event.target.value;
      ui.editing = true;
      sync();
    },
  });
  sync();
  return field;
}

function button(label, kind, onClick, disabled) {
  return el("button", { class: `btn btn-${kind}`, type: "button", onClick, disabled: disabled ? "disabled" : null }, label);
}

/* The simulated-rate disclosure. Rendered wherever a simulated figure is,
   never once at the top of the page: an approver looking at a price must see
   it beside the price. */
function simBanner(rates) {
  if (!rates || !rates.simulated || !rates.banner) return null;
  return el(
    "p",
    { class: "sim-banner" },
    rates.banner,
    el("span", { class: "sub" }, "These figures were generated by a demo rate simulator. No rate provider was contacted and this is not a commercial offer.")
  );
}

/* ---------------------------------------------------------------- network */

/* Reads the server's state and redraws only when it actually differs.
 *
 * The comparison is not an optimisation. This runs every few seconds, and a
 * redraw replaces every node on the page: without it an open <details>, a
 * scroll position and any hover would be thrown away on a timer, and the
 * screen would read as flickering rather than as live. `force` is for the
 * moments where the same bytes must still be re-rendered — switching between
 * the dashboard and a request. */
async function refresh(force) {
  if (ui.refreshing) {
    /* One read at a time. A *forced* one is a view change rather than a poll,
       though, so it is queued instead of dropped: a read can sit behind a
       mailbox poll holding the server's lock for a long time, and a click
       discarded in that window would leave the operator looking at the screen
       they clicked away from with no way to tell why. */
    if (force) ui.refreshAgain = true;
    return;
  }
  ui.refreshing = true;
  try {
    await readAndRender(force);
  } finally {
    ui.refreshing = false;
  }
  if (ui.refreshAgain) {
    ui.refreshAgain = false;
    await refresh(true);
  }
}

/* What counts as "the state changed", with the liveness clock taken out of it.
 *
 * The server stamps every poll with the time it read the mailbox, so the raw
 * payload differs on every single tick — comparing it whole would rebuild the
 * page every few seconds forever, which is the thing the comparison exists to
 * prevent. It is not a cosmetic difference: a rebuild throws away open folds
 * and scroll position, and a click landing on a node that is being replaced
 * does nothing at all. */
function stateKey(snap) {
  return JSON.stringify({ ...snap, poll: { ...snap.poll, last_checked_at: null } });
}

async function readAndRender(force) {
  const query = ui.selected ? `?request_id=${encodeURIComponent(ui.selected)}` : "";
  let text = null;
  try {
    const response = await fetch(`/api/live/state${query}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    text = await response.text();
  } catch (err) {
    document.getElementById("load-error").hidden = false;
    return;
  }
  document.getElementById("load-error").hidden = true;

  ui.snap = JSON.parse(text);
  const key = stateKey(ui.snap);
  if (force || key !== ui.snapKey) {
    ui.snapKey = key;
    render();
  } else if (ui.view === "dashboard") {
    /* Nothing happened, so nothing is redrawn — but the indicator still has to
       track the mailbox, or a poll that started failing would go unreported
       until something else changed. Written straight into the header rather
       than through a render. */
    renderLiveIndicator();
  }
}

/* The loop that makes the page live. Two things pause it and neither can stop
   it: an action already in flight (its own response is the newer state), and
   an operator typing their name into a decision field. */
function watchForChanges() {
  /* The tick returns the refresh rather than firing and forgetting it, so a
     caller — a test, today — can wait for the redraw it caused instead of
     guessing how long one takes. */
  setInterval(() => (ui.busy || ui.editing ? null : refresh(false)), REFRESH_MS);
}

async function post(action, body, label) {
  if (ui.busy) return;
  /* The click took the name; nobody is typing any more. Cleared here as well
     as on blur, because this view is about to be rebuilt — the field the blur
     would have come from will not exist, and a flag left set would pause the
     automatic refresh for the rest of the session. */
  ui.editing = false;
  ui.busy = true;
  ui.busyLabel = label || "Working…";
  ui.error = null;
  render();

  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), POLL_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/live/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, request_id: ui.selected }),
      signal: abort.signal,
    });
    const payload = await response.json();
    if (!response.ok) {
      ui.error = payload.error || `Action failed (HTTP ${response.status})`;
    } else {
      ui.snap = payload;
      ui.error = null;
    }
  } catch (err) {
    ui.error =
      err && err.name === "AbortError"
        ? "The server took too long to answer. It may still be working — the page will show the result as soon as it does."
        : "The demo server did not respond.";
  }
  clearTimeout(timer);
  ui.busy = false;
  ui.busyLabel = null;
  /* The action replaced the snapshot with its own response. Recorded as the
     new baseline so the next tick does not redraw over the result the operator
     is reading. */
  ui.snapKey = ui.snap ? stateKey(ui.snap) : null;
  render();
}

/* -------------------------------------------------------------- dashboard */

/* Whether the desk is watching the mailbox — the only status the page shows.
 *
 * Counts, poll timings and internal processing state are the machinery and
 * belong in the log, not on an operator's screen. What does belong is the one
 * fact they cannot otherwise know: that mail is being picked up. It turns
 * amber when the background poll is failing, so a dashboard that has silently
 * stopped receiving work does not look like a quiet morning. The time of the
 * last successful read is the tooltip — available when questioned, not text
 * on the page. */
function renderLiveIndicator() {
  const poll = ui.snap.poll;
  const indicator = document.getElementById("live-indicator");
  const failing = Boolean(poll.error);
  indicator.className = failing ? "live live-stalled" : "live";
  document.getElementById("live-label").textContent = failing ? "Reconnecting" : "Live";
  const checked = fmtStamp(poll.last_checked_at);
  indicator.setAttribute(
    "title",
    failing
      ? "The mailbox could not be reached on the last attempt. Retrying automatically."
      : checked
        ? `Mailbox last checked ${checked}`
        : "Watching the mailbox"
  );
}

/* Nothing has arrived yet. Deliberately almost empty: a dashboard with no work
   on it should look like a desk that is ready, not like a screen that failed
   to load. */
function emptyState() {
  return el("div", { class: "empty" },
    el("span", { class: "empty-icon", "aria-hidden": "true" }, "\u2709"),
    el("h1", { class: "empty-title" }, "Waiting for new enquiry"),
    el("p", { class: "empty-sub" }, "New enquiries will appear here automatically."));
}

function renderDashboard() {
  const snap = ui.snap;
  const holder = document.getElementById("dashboard-list");
  renderLiveIndicator();

  /* The page title is for a page with something on it. The empty state carries
     its own heading and reads better without a second one above it. */
  document.getElementById("page-head").hidden = !snap.requests.length;

  if (!snap.requests.length) {
    holder.replaceChildren(emptyState());
    return;
  }

  /* Two groups, decided by what extraction actually found — not by a list of
     approved subjects or senders anyone has to maintain. A message that stated
     no shipment is still shown, explaining itself, so the operator can see the
     classification is right rather than trust it.

     There is no band for earlier work: the session drops what an earlier
     demonstration left behind, so nothing reaches this function that is not
     part of the run happening now. */
  const enquiries = snap.requests.filter((r) => r.is_enquiry);
  const others = snap.requests.filter((r) => !r.is_enquiry);

  const groups = [el("div", { class: "request-list" }, ...enquiries.map(requestCard))];

  if (others.length) {
    groups.push(
      el("h2", { class: "group-head" }, "Other messages"),
      el("div", { class: "request-list" }, ...others.map(requestCard))
    );
  }

  holder.replaceChildren(...groups);
}

function requestCard(request) {
  const received = fmtStamp(request.received_at);
  const muted = !request.is_enquiry;
  return el("article", { class: `card request-card${muted ? " request-muted" : ""}` },
    el("div", { class: "card-body" },
      el("div", { class: "request-row" },
        el("div", { class: "request-main" },
          /* The reference and the time it came in. The field count that used
             to sit here was extraction's own bookkeeping — true, and of no use
             to somebody quoting a shipment. */
          el("p", { class: "eyebrow" },
            [request.request_id, received].filter(Boolean).join("  ·  ")),
          el("h2", { class: "request-title" }, request.headline),
          request.lane ? el("p", { class: "route" }, request.lane) : null,
          el("p", { class: "muted small" },
            [request.weight, request.client_address].filter(Boolean).join(" · "))),
        el("div", { class: "request-side" },
          pill(request.status.label, request.status.tone),
          button("Open request", "primary", () => {
            ui.selected = request.request_id;
            ui.view = "detail";
            window.scrollTo(0, 0);
            refresh(true);
          }))),
      request.waiting_replies
        ? el("p", { class: "waiting-note" },
            `${request.waiting_replies} client reply waiting — approve the clarification to merge it`)
        : null,
      request.rate_failure
        ? el("p", { class: "waiting-note" }, `Rate search could not run: ${request.rate_failure}`)
        : null,
      request.manual_review_notes && request.manual_review_notes.length
        ? el("p", { class: "waiting-note" },
            "Handed to manual review — the client's answer could not be used automatically.")
        : null,
      request.not_enquiry_reason
        ? el("p", { class: "not-enquiry" }, request.not_enquiry_reason)
        : null));
}

/* ----------------------------------------------------------------- detail */

const MARKS = { done: "\u2713", current: "\u25CF", pending: "\u25CB" };
/* An hourglass when the next move is the client's, a filled dot when it is
   ours. A single marker for both leaves the room unable to tell whose move
   it is, which is the one thing a presenter is narrating at that moment. */
const WAITING_ON_CLIENT = "\u23F3";

function markFor(step) {
  if (step.state === "current" && step.waiting_on === "client") return WAITING_ON_CLIENT;
  return MARKS[step.state];
}

/* The timeline is the screen the presentation is narrated from. Every finished
   row shows the real moment it happened; a row with no timestamp is shown as
   not yet reached rather than given a plausible-looking one. */
function renderTimeline(timeline) {
  document.getElementById("timeline").replaceChildren(
    ...timeline.map((step) =>
      el("li", { class: `tl tl-${step.state}` },
        el("span", { class: "tl-mark", "aria-hidden": "true" }, markFor(step)),
        el("span", { class: "tl-body" },
          el("span", { class: "tl-label" }, step.label),
          el("span", { class: "tl-when" },
            step.at ? fmtStamp(step.at) : step.note || "Pending"))))
  );
}

/* The provenance pills these cards used to carry — "REAL GMAIL",
   "REAL GMAIL · CORRELATED" — were the same demo-plumbing disclosure as the
   capability strip, in a smaller font. What the operator needs from a client
   email is who sent it, when, and what it says. */
function emailCard(title, email) {
  if (!email) return null;
  return card(
    [el("h2", null, title)],
    kv([
      ["From", email.from],
      ["Subject", email.subject],
      ["Received", fmtStamp(email.received_at) || "—"],
    ]),
    el("pre", { class: "email-body" }, email.body_text)
  );
}

function sectionEnquiry(detail) {
  return emailCard("Client email", detail.enquiry || detail.latest_email);
}

function sectionReply(detail) {
  return emailCard("Client reply", detail.reply);
}

function sectionMerged(detail) {
  if (!detail.reply_received || !detail.merged.length) return null;
  return card(
    [el("h2", null, "Merged shipment")],
    el("p", { class: "card-sub" }, "The reply was matched to this enquiry by its mail thread, not by subject line."),
    kv([
      ["Supplied by the reply", detail.merged.join(", ")],
      ["Carried from the enquiry", detail.carried.join(", ") || "—"],
    ])
  );
}

function sectionShipment(detail) {
  const rows = detail.shipment.map((row) =>
    el("tr", { class: `row-${row.status}` },
      el("th", { scope: "row" }, row.label),
      el("td", null, row.value || (row.status === "missing" ? "— missing" : "— not required")),
      el("td", null, row.source === "reply" ? pill("from reply", "blue") : null)));
  return card(
    [el("h2", null, "Extracted shipment"),
     pill(detail.validation.is_valid ? "VALID" : "INCOMPLETE", detail.validation.is_valid ? "green" : "amber")],
    el("table", { class: "shipment-table" }, el("tbody", null, ...rows)),
    detail.validation.issues.length
      ? el("ul", { class: "issue-list" },
          ...detail.validation.issues.map((issue) => el("li", null, `${issue.rule_id}: ${issue.message}`)))
      : null,
    detail.merged.length ? el("p", { class: "small muted" }, `Supplied by the reply: ${detail.merged.join(", ")}. Carried from the enquiry: ${detail.carried.join(", ")}.`) : null
  );
}

function sectionClarification(detail) {
  const clar = detail.clarification;
  if (!clar) return null;
  const children = [
    el("p", { class: "card-sub" }, "Deterministic wording — no model writes client-facing copy."),
    el("ul", { class: "issue-list" }, ...clar.unresolved.map((item) => el("li", null, `${item.title}: ${item.question}`))),
    el("pre", { class: "email-body" }, clar.body_text),
  ];
  if (clar.awaiting_approval && !detail.is_enquiry) {
    /* Extraction found no shipment in this message, so the "missing fields"
       are all of them and the draft would be a cargo questionnaire sent to
       whoever happened to email us. Offering the button here is how a
       newsletter gets a clarification; the operator can still read the draft. */
    children.push(
      el("div", { class: "banner banner-amber" },
        "Not sent — this message does not look like a quotation enquiry",
        el("span", { class: "sub" },
          "No shipment details were found in it, so no clarification is offered. " +
          "If this is genuinely an enquiry, reply to the client directly."))
    );
  } else if (clar.awaiting_approval) {
    if (detail.waiting_replies) {
      /* The client has already answered, and the answer cannot be merged until
         the question it answers has actually been sent. Saying so turns a
         request that looks idle into one that is plainly waiting on the
         operator rather than on the client. */
      children.push(
        el("div", { class: "banner banner-amber" },
          `${detail.waiting_replies} client reply waiting on this clarification`,
          el("span", { class: "sub" },
            "It cannot be merged until the clarification below has been approved and " +
            "sent. Approve it and the reply is picked up on the next mailbox check."))
      );
    }
    const approve = button("Approve & send to client", "approve",
      () => post("clarification/approve", { by: ui.approver }, "Sending clarification…"),
      !canDecide());
    children.push(
      el("div", { class: "decision-actions" }, approverField(approve), approve),
      el("p", { class: "action-note" }, "Sends a real email from the Translog mailbox. Nothing is sent until you click, and a name is required.")
    );
  } else if (clar.sent_by) {
    children.push(el("div", { class: "banner banner-green" }, "✓ Clarification sent to the client", el("span", { class: "sub" }, `Approved by ${clar.sent_by}.`)));
  }
  return card(
    [el("h2", null, "Clarification"), pill(clar.awaiting_approval ? "AWAITING YOUR APPROVAL" : "SENT", clar.awaiting_approval ? "amber" : "green")],
    ...children
  );
}

function sectionRates(detail) {
  const rates = detail.rates;
  if (!rates) {
    /* No rates and a reason why. Without this the detail view simply omits the
       section and the request reads as though nothing had been attempted. */
    if (!detail.rate_failure) return null;
    return card(
      [el("h2", null, "Rate search & selection"), pill("NOT RUN", "amber")],
      el("p", null, `Rate search could not run: ${detail.rate_failure}`),
      el("p", { class: "muted small" },
        "Nothing was sent and nothing was approved. The next mailbox check " +
        "retries this request automatically."));
  }
  const selection = rates.selection;
  return card(
    [el("h2", null, "Rate search & selection"), pill(rates.simulated ? "SIMULATED" : "LIVE PROVIDER", rates.simulated ? "amber" : "green")],
    simBanner(rates),
    /* The search itself, presented as the search it is. */
    el("div", { class: "search-strip" },
      el("span", { class: "search-leg" },
        el("span", { class: "search-place" }, rates.query.origin),
        el("span", { class: "search-arrow", "aria-hidden": "true" }, "✈"),
        el("span", { class: "search-place" }, rates.query.destination)),
      el("span", { class: "search-meta" }, `${rates.query.weight_kg} kg`),
      el("span", { class: "search-meta" }, rates.query.date),
      el("span", { class: "search-meta muted" },
        `${rates.returned} returned · ${rates.eligible_count} eligible`)),
    el("p", { class: "strategy-note" }, rates.strategy),
    /* Every eligible rate as a comparison card, the selected one leading. */
    selection
      ? el("div", { class: "rate-board" },
          ...[...rates.eligible]
            .sort((a, b) => (a.carrier_code === selection.carrier_code ? -1 : b.carrier_code === selection.carrier_code ? 1 : 0))
            .map((rate) => rateCard(rate, rate.carrier_code === selection.carrier_code ? selection : null)))
      : el("p", { class: "muted" }, "No eligible rate — nothing will be quoted."),
    rates.excluded.length
      ? el("details", { class: "excluded-fold" },
          el("summary", null,
            `${rates.excluded.length} rate(s) excluded — see why`),
          el("div", { class: "excluded-rows" },
            ...rates.excluded.map((row) =>
              el("div", { class: "excluded-row" },
                carrierAvatar(row.carrier_code, true),
                el("span", { class: "excluded-name" }, row.carrier_name),
                el("span", { class: `reason-chip reason-${row.reason}` }, row.reason.replace(/_/g, " ")),
                el("span", { class: "excluded-detail muted small" }, row.detail)))))
      : null
  );
}

/* A two-letter carrier mark, coloured stably from its code so the same
   carrier always wears the same colour. Decoration, never data. */
function carrierAvatar(code, dim) {
  const hue = ((code.charCodeAt(0) || 65) * 7 + (code.charCodeAt(1) || 65) * 13) % 6;
  return el("span", { class: `carrier-avatar hue-${hue}${dim ? " avatar-dim" : ""}`, "aria-hidden": "true" }, code);
}

/* One rate as a marketplace-style comparison card. `chosen` is the selection
   payload when this rate is the selected one, else null. */
function rateCard(rate, chosen) {
  return el("div", { class: `rate-card${chosen ? " rate-selected" : ""}` },
    chosen ? el("span", { class: "rate-ribbon" }, "SELECTED") : null,
    el("div", { class: "rate-main" },
      carrierAvatar(rate.carrier_code, false),
      el("div", { class: "rate-carrier" },
        el("span", { class: "rate-name" }, rate.carrier_name),
        el("span", { class: "rate-product" }, rate.product))),
    el("div", { class: "rate-mid" },
      el("span", { class: "transit-chip" }, `⏱ ${rate.transit || "—"}`),
      chosen ? el("span", { class: "rate-why" }, chosen.reason) : null),
    el("div", { class: "rate-price" },
      el("span", { class: "price-amount" }, rate.amount || "—"),
      rate.currency ? el("span", { class: "price-currency" }, rate.currency) : null));
}

function sectionManualReview(detail) {
  /* Why a person has to take over, in the model's own words. Rendered only
     for a request that was actually escalated; an empty card would imply a
     problem where there is none. */
  const notes = detail.manual_review_notes;
  if (!notes || !notes.length) return null;
  return card(
    [el("h2", null, "Manual review required"), pill("HANDED TO A PERSON", "amber")],
    el("p", null,
      "The client replied, but their answer could not be used automatically. " +
      "Asking the same question again will not resolve it — this request needs a person."),
    el("ul", { class: "issue-list" }, ...notes.map((note) => el("li", null, note)))
  );
}

function sectionApproval(detail) {
  const approval = detail.approval;
  if (!approval) return null;
  const decision = detail.decision;

  if (decision) {
    return card(
      [el("h2", null, "Human approval"), pill(decision.approved ? "APPROVED" : "DECLINED", decision.approved ? "green" : "gray")],
      el("div", { class: decision.approved ? "banner banner-green" : "banner banner-amber" },
        decision.headline,
        el("span", { class: "sub" },
          `Decided by ${decision.by}${decision.reason ? ` — ${decision.reason}` : ""}. ` +
          `Quotation email ${decision.sent ? "sent to the client." : "not sent."}`))
    );
  }

  return el("article", { class: "card approval-card" },
    el("div", { class: "card-head" },
      el("h2", null, "Approval required"),
      pill("NOTHING SENT YET", "amber")),
    el("div", { class: "card-body" },
      simBanner({ simulated: approval.simulated, banner: approval.banner }),
      el("p", { class: "card-sub" }, `The full review packet was emailed to ${approval.review_sent_to}. Approving sends the quotation to the client; declining sends nothing.`),
      el("div", { class: "approval-grid" },
        el("div", null,
          el("h3", null, "Shipment"),
          kv(detail.shipment.filter((row) => row.value).map((row) => [row.label, row.value]))),
        el("div", null,
          el("h3", null, "Selected service"),
          kv([
            ["Reference", approval.reference],
            ["Carrier", approval.carrier],
            ["Service", approval.service],
            ["Transit", approval.transit],
            ["Price", approval.price],
            ["Why", approval.reason],
          ]),
          approval.excluded.length
            ? el("div", null,
                el("h3", null, "Excluded"),
                el("ul", { class: "issue-list" },
                  ...approval.excluded.map((row) => el("li", null, `${row.carrier_name} — ${row.detail}`))))
            : null)),
      decisionRow(),
      el("p", { class: "action-note" }, "A name is required for either choice. There is no default and no timeout: if nobody decides, nothing is sent."))
  );
}

function sectionQuotation(detail) {
  const decision = detail.decision;
  if (!decision) return null;
  return card(
    [el("h2", null, "Final quotation"),
     pill(decision.sent ? "SENT TO CLIENT" : "NOT SENT", decision.sent ? "green" : "gray")],
    kv([
      ["Outcome", decision.headline],
      ["Decided by", decision.by],
      ["Decided at", fmtStamp(decision.at) || "—"],
      ["Reason", decision.reason || "—"],
      ["Email to client", decision.sent ? "Sent" : "Not sent"],
    ])
  );
}

/* The quotation gate's controls. Both buttons track the approver field, and
   neither is reachable without a name — the same rule the server enforces, so
   the interface never offers something the backend would refuse. */
function decisionRow() {
  const approve = button("APPROVE — send quotation", "approve",
    () => post("quotation/decide", { decision: "approve", by: ui.approver }, "Sending quotation…"),
    !canDecide());
  const decline = button("DECLINE — send nothing", "decline",
    () => post("quotation/decide", { decision: "decline", by: ui.approver, reason: ui.declineReason }, "Recording decline…"),
    !canDecide());
  return el("div", { class: "decision-actions" },
    approverField(approve, decline), approve, decline);
}

function renderDetail() {
  const detail = ui.snap.selected;
  if (!detail) {
    document.getElementById("sections").replaceChildren(
      el("div", { class: "card empty-state" }, "This request is no longer available.")
    );
    return;
  }

  document.getElementById("detail-header").replaceChildren(
    el("div", { class: "card-head" },
      el("div", null,
        el("p", { class: "eyebrow" }, `Request ${detail.request_id}`),
        el("h1", null, detail.headline),
        el("p", { class: "muted small" },
          [detail.subject, detail.client_address].filter(Boolean).join(" · "))),
      pill(detail.status.label, detail.status.tone))
  );
  renderTimeline(detail.timeline);

  const sections = [
    sectionEnquiry(detail),
    sectionShipment(detail),
    sectionClarification(detail),
    sectionReply(detail),
    sectionMerged(detail),
    sectionManualReview(detail),
    sectionRates(detail),
    sectionApproval(detail),
    sectionQuotation(detail),
  ].filter(Boolean);

  document.getElementById("sections").replaceChildren(...sections);
}

/* ----------------------------------------------------------------- render */

function render() {
  if (!ui.snap) return;
  const dashboard = document.getElementById("view-dashboard");
  const detail = document.getElementById("view-detail");
  dashboard.hidden = ui.view !== "dashboard";
  detail.hidden = ui.view !== "detail";
  if (ui.view === "dashboard") renderDashboard();
  else renderDetail();

  const banner = document.getElementById("busy-banner");
  banner.hidden = !ui.busy;
  banner.textContent = ui.busy ? `${ui.busyLabel} this can take up to a minute.` : "";

  /* Set here rather than inside a view renderer. It used to be appended by
     renderDetail() alone, so an action that failed while the dashboard was on
     screen — which is every action on a fresh demonstration — set ui.error and
     displayed nothing at all. */
  const failure = document.getElementById("action-error");
  failure.hidden = !ui.error;
  failure.textContent = ui.error ? `⚠ ${ui.error}` : "";
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-back").addEventListener("click", () => {
    ui.view = "dashboard";
    ui.selected = null;
    refresh(true);
  });

  /* Draw whatever the server already knows, then keep watching. Reading the
     mailbox is not this page's job and never was a person's: the server polls
     it on its own timer, so an enquiry sent while nobody was looking is
     already processed by the time the dashboard is opened. */
  refresh(true);
  watchForChanges();
});
