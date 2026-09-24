/* Behavioural tests for the live view's decision controls.
 *
 * The bug these exist for was invisible to any source-level check: the button's
 * `disabled` attribute was computed once at render time and the input handler
 * updated the model without recomputing it, so a typed name never enabled
 * anything. Catching that needs the real code, actually rendered, actually
 * typed into — so this loads live.js into a minimal DOM and drives it.
 *
 * Run directly: node tests/js/live_ui.test.js
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(
  __dirname, "..", "..", "src", "translog_quote", "interface", "web", "static", "live.js"
);

/* --- the smallest DOM this code needs ----------------------------------- */

class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attrs = {};
    this.listeners = {};
    this.className = "";
    this.disabled = false;
    this._text = "";
  }
  setAttribute(key, value) {
    this.attrs[key] = value;
    if (key === "disabled") this.disabled = true;
    if (key === "value") this.value = value;
  }
  addEventListener(event, fn) {
    (this.listeners[event] = this.listeners[event] || []).push(fn);
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  set textContent(value) {
    this._text = value;
    this.children = [];
  }
  get textContent() {
    return this.children.length
      ? this.children.map((c) => c.textContent).join("")
      : this._text;
  }
  fire(event, payload) {
    (this.listeners[event] || []).forEach((fn) => fn(payload));
  }
  /* Depth-first walk, so a test can find a control without knowing the markup. */
  find(predicate) {
    if (predicate(this)) return this;
    for (const child of this.children) {
      const hit = child.find ? child.find(predicate) : null;
      if (hit) return hit;
    }
    return null;
  }
  findAll(predicate, out = []) {
    if (predicate(this)) out.push(this);
    this.children.forEach((c) => c.findAll && c.findAll(predicate, out));
    return out;
  }
}

const intervals = [];

function makeContext(fetchStub) {
  const byId = {};
  const document = {
    createElement: (tag) => new Node(tag),
    createTextNode: (text) => {
      const node = new Node("#text");
      node._text = text;
      return node;
    },
    getElementById: (id) => (byId[id] = byId[id] || new Node("div")),
    addEventListener: () => {},
  };
  return vm.createContext({
    Node, document, console,
    window: { scrollTo: () => {} },
    fetch: fetchStub,
    AbortController: class {
      constructor() { this.signal = {}; }
      abort() {}
    },
    setTimeout, clearTimeout, Date, Number, JSON, Math, Object, Array, String,
    encodeURIComponent,
    /* Captured rather than run: the page arms a repeating timer at load, and
       a test wants to know it did and then fire it by hand — not to have a
       real interval firing underneath the assertions. */
    setInterval: (fn, ms) => {
      intervals.push({ fn, ms });
      return intervals.length;
    },
  });
}

function load(fetchStub) {
  intervals.length = 0;
  const context = makeContext(fetchStub);
  const source =
    fs.readFileSync(SOURCE, "utf8") +
    "\n;globalThis.__t = { ui, canDecide, sectionClarification, sectionApproval," +
    " renderDashboard, renderTimeline, render, post, sectionRates, refresh," +
    " watchForChanges, REFRESH_MS, renderDetail, nextStep, sectionManualReview," +
    " sectionShipment, sectionMerged," +
    " syncApprover: () => syncApprover && syncApprover()," +
    " holderFor: (id) => document.getElementById(id) };";
  vm.runInContext(source, context);
  return context.__t;
}

/* --- fixtures ------------------------------------------------------------ */

function awaitingClarification() {
  return {
    is_enquiry: true,
    shipment: [{ label: "Origin", value: "Mumbai", status: "known" }],
    clarification: {
      subject: "Re: Air Freight Quote Demo",
      body_text: "Please confirm the following.",
      unresolved: [
        { field: "is_chemical", title: "Chemical status", question: "Is it a chemical?" },
        { field: "delivery_type", title: "Delivery type", question: "Door or airport?" },
      ],
      sent_by: null,
      awaiting_approval: true,
    },
  };
}

/* --- assertions ---------------------------------------------------------- */

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`  ok   ${name}`);
  } catch (err) {
    failures += 1;
    console.log(`  FAIL ${name}\n       ${err.message}`);
  }
}
/* post() is async, and the failure it must surface only exists after the
   response comes back — so these cases cannot run under the sync helper. */
const asyncChecks = [];
function checkAsync(name, fn) {
  asyncChecks.push([name, fn]);
}
function eq(actual, expected, what) {
  if (actual !== expected) {
    throw new Error(`${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const isButton = (n) => n.tagName === "button";
const isInput = (n) => n.tagName === "input";

/* --- the disabled/enabled condition -------------------------------------- */

check("the approve button starts disabled when no name has been typed", () => {
  const t = load();
  const section = t.sectionClarification(awaitingClarification());
  eq(section.find(isButton).disabled, true, "button.disabled");
});

check("typing a name enables the approve button", () => {
  const t = load();
  const section = t.sectionClarification(awaitingClarification());
  const input = section.find(isInput);
  const approve = section.find(isButton);

  input.fire("input", { target: { value: "Aayush" } });

  eq(approve.disabled, false, "button.disabled after typing");
  eq(t.ui.approver, "Aayush", "ui.approver");
});

check("clearing the name disables it again", () => {
  const t = load();
  const section = t.sectionClarification(awaitingClarification());
  const input = section.find(isInput);
  const approve = section.find(isButton);

  input.fire("input", { target: { value: "Aayush" } });
  input.fire("input", { target: { value: "" } });

  eq(approve.disabled, true, "button.disabled after clearing");
});

check("whitespace alone is not a name", () => {
  const t = load();
  const section = t.sectionClarification(awaitingClarification());
  const input = section.find(isInput);
  const approve = section.find(isButton);

  input.fire("input", { target: { value: "   " } });

  eq(approve.disabled, true, "button.disabled for whitespace");
});

check("an autofilled name (DOM value set, no input event) enables the approve button", () => {
  /* The live-browser bug: browser/password-manager autofill populates the DOM
     input's value WITHOUT firing an input event, so ui.approver stayed empty
     and the button stayed disabled though the name was visibly in the field. */
  const t = load();
  const section = t.sectionClarification(awaitingClarification());
  const input = section.find(isInput);
  const approve = section.find(isButton);

  eq(approve.disabled, true, "precondition: disabled with an empty field");

  input.value = "aayush";   // autofill sets the DOM value directly...
  input.fire("change");     // ...and commits with a change event, never input

  eq(approve.disabled, false, "button enabled once the field's value is read back");
  eq(t.ui.approver, "aayush", "ui.approver picked up the autofilled value");
});

check("a name that appeared with no event at all is caught by the refresh resync", () => {
  /* The hardest case: a value is present in the DOM with no event of any kind.
     The refresh tick calls syncApprover(), which re-reads the live field. */
  const t = load();
  const section = t.sectionClarification(awaitingClarification());
  const input = section.find(isInput);
  const approve = section.find(isButton);

  input.value = "aayush";   // no event whatsoever is fired
  eq(approve.disabled, true, "still disabled until a resync runs");

  t.syncApprover();         // what the refresh tick does each tick

  eq(approve.disabled, false, "the resync reads the live DOM and enables it");
  eq(t.ui.approver, "aayush", "and captures the value");
});

check("the field keeps its identity across typing, so the caret survives", () => {
  const t = load();
  const section = t.sectionClarification(awaitingClarification());
  const input = section.find(isInput);

  input.fire("input", { target: { value: "Aa" } });
  input.fire("input", { target: { value: "Aay" } });

  eq(section.find(isInput) === input, true, "the input node was replaced mid-word");
});

/* --- the approval action ------------------------------------------------- */

check("clicking approve posts the name to the clarification endpoint", () => {
  const calls = [];
  const t = load((url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ requests: [] }) });
  });
  const section = t.sectionClarification(awaitingClarification());
  const input = section.find(isInput);
  const approve = section.find(isButton);

  input.fire("input", { target: { value: "Aayush" } });
  approve.fire("click");

  eq(calls.length, 1, "one request");
  eq(calls[0].url, "/api/live/clarification/approve", "endpoint");
  eq(calls[0].body.by, "Aayush", "approver name in the body");
});

check("a disabled button posts nothing even if clicked", () => {
  const calls = [];
  const t = load((url) => {
    calls.push(url);
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  });
  const section = t.sectionClarification(awaitingClarification());
  const approve = section.find(isButton);

  eq(approve.disabled, true, "precondition: disabled");
  /* A real browser swallows the click on a disabled control. The server
     refuses an anonymous approval regardless — this only checks the interface
     is not the thing that would send it. */
  eq(calls.length, 0, "no request was made without a name");
});

check("the quotation gate offers approve and decline, both gated on the name", () => {
  const t = load();
  const detail = {
    is_enquiry: true,
    shipment: [{ label: "Origin", value: "Mumbai" }],
    decision: null,
    approval: {
      reference: "R-1", simulated: true, banner: "SIMULATED WEBCARGO DATA — DEMO ONLY",
      review_sent_to: "approvals@translog.example", carrier: "Emirates (EK)",
      service: "GEN", transit: "2 days", price: "20762.10 INR", reason: "fastest",
      excluded: [],
    },
  };
  const section = t.sectionApproval(detail);
  const buttons = section.findAll(isButton);
  eq(buttons.length, 2, "two decision buttons");
  eq(buttons.every((b) => b.disabled), true, "both start disabled");

  section.find(isInput).fire("input", { target: { value: "Aayush" } });

  eq(buttons.every((b) => !b.disabled), true, "both enabled once named");
});

check("the approval card states the candidate-set scope so 'fastest' is not read as global", () => {
  /* H5: the selected rate is the fastest of the set WebCargo returned, which is
     a price-truncated 'lowest rates' list. The card must say so and show the
     provider's own note, never implying a global fastest. */
  const t = load();
  const detail = {
    is_enquiry: true,
    shipment: [{ label: "Origin", value: "Mumbai" }],
    decision: null,
    approval: {
      reference: "R-1", simulated: false, banner: null, notice: null,
      review_sent_to: "approvals@translog.example", carrier: "Qatar Airways (QR)",
      service: "QR General", transit: "18h 30m", price: "185597.5 Rs",
      departure_date: "25/09/2026", searched_date: "2026-09-25",
      reason: "fastest eligible transit at 18h 30m",
      candidate_scope:
        "Fastest eligible among the 18 rates WebCargo returned — not necessarily the fastest that exists.",
      completeness: "Showing the 18 lowest rates. Other surcharges may apply.",
      excluded: [],
    },
  };

  const text = t.sectionApproval(detail).textContent;

  eq(/among the 18 rates WebCargo returned/.test(text), true, "the scope sentence is shown");
  eq(/not necessarily the fastest that exists/.test(text), true, "it does not claim global fastest");
  eq(/Showing the 18 lowest rates/.test(text), true, "WebCargo's own note is shown verbatim");
});

check("with no provider total the card states the scope but shows no verbatim note", () => {
  const t = load();
  const detail = {
    is_enquiry: true,
    shipment: [],
    decision: null,
    approval: {
      reference: "R-1", simulated: false, banner: null, notice: null,
      review_sent_to: "a@b", carrier: "QR", service: "GEN", transit: "1 day",
      price: "1 INR", departure_date: "x", searched_date: "y", reason: "r",
      candidate_scope:
        "Fastest eligible among the 5 rates WebCargo returned — not necessarily the fastest " +
        "that exists. WebCargo stated no total, so completeness is unconfirmed.",
      completeness: null,
      excluded: [],
    },
  };

  const text = t.sectionApproval(detail).textContent;

  eq(/completeness is unconfirmed/.test(text), true, "the scope states completeness is unknown");
  eq(/WebCargo: /.test(text), false, "no verbatim provider note line when none was given");
});

/* --- the dashboard leads with the demonstration --------------------------- */

function snapshotWith(requests, demonstration) {
  return {
    demonstration: Object.assign(
      { active: false, started_at: null, following: 0, earlier_requests: 0, outside_messages: 0 },
      demonstration
    ),
    requests,
    audit: [],
    poll: {
      new_messages: 0, skipped_internal: 0, deferred: 0, enquiries: 0, unrecognised: 0,
      last_checked_at: "2026-08-29T10:09:00+05:30", error: null,
    },
    mode: { badge: "LIVE", banner: "SIMULATED", provenance: [] },
    selected: null,
  };
}

function request(overrides) {
  return Object.assign(
    {
      request_id: "R-1", headline: "Air Freight Quote Demo", subject: "Air Freight Quote Demo",
      client_address: "client@example.com", lane: "Mumbai → Dubai", weight: "320 kg",
      received_at: "2026-08-29T10:08:00+05:30", shipment_fields: 7,
      status: { label: "INFORMATION REQUIRED", tone: "amber" },
      is_enquiry: true, is_new: true,
      not_enquiry_reason: null, waiting_replies: 0,
    },
    overrides
  );
}

function headings(holder) {
  return holder.findAll((n) => n.tagName === "h2").map((n) => n.textContent);
}

/* The dashboard's band subheadings only — a request card's own title is an h2
   too, and counting those would make "no subheading" impossible to assert. */
function bandHeadings(holder) {
  return holder
    .findAll((n) => n.className === "group-head")
    .map((n) => n.textContent);
}

check("the dashboard shows this session's requests under a named Active band", () => {
  const t = load();
  t.ui.snap = snapshotWith([request({ request_id: "R-NEW" })], { active: true, following: 1 });
  t.renderDashboard();
  const text = t.holderFor("dashboard-list").textContent;

  eq(/R-NEW/.test(text), true, "the request is on the page");
  eq(/Earlier enquiries/.test(text), false, "no band of historical work exists any more");
  eq(/request\(s\)/.test(text), false, "no legacy 'N request(s)' counter");
  eq(bandHeadings(t.holderFor("dashboard-list")).length, 1, "one band heading: Active");
  eq(/Active \(1\)/.test(text), true, "the Active band is labelled with its count");
});

check("a non-enquiry is never rendered — unrelated mail is filtered upstream", () => {
  /* Unrelated mail is dropped at the ingestion/routing boundary and never
     becomes a request, so there is no "Other messages" band. Even if a
     non-enquiry ever reached the snapshot, the view drops it rather than
     exposing a raw Gmail message. */
  const t = load();
  t.ui.snap = snapshotWith(
    [request({ request_id: "R-1" }), request({ request_id: "R-2", is_enquiry: false })],
    { active: true, following: 2 }
  );
  t.renderDashboard();
  const text = t.holderFor("dashboard-list").textContent;

  eq(bandHeadings(t.holderFor("dashboard-list")).length, 1, "only the Active band — no 'Other messages' band");
  eq(/Active \(1\)/.test(text), true, "only the one enquiry is counted in the Active band");
  eq(/R-1/.test(text), true, "the enquiry is shown");
  eq(/R-2/.test(text), false, "the non-enquiry is not exposed");
});

check("the empty state waits for an enquiry and says nothing technical", () => {
  const t = load();
  t.ui.snap = snapshotWith([], { active: true, following: 0 });
  t.renderDashboard();
  const text = t.holderFor("dashboard-list").textContent;

  eq(/Waiting for new enquiry/.test(text), true, "it says what it is waiting for");
  eq(/New enquiries will appear here automatically/.test(text), true, "and that it is automatic");
  eq(/Check mail/.test(text), false, "it asks nobody to press anything");
  eq(/mailbox/i.test(text), false, "no mailbox mechanics on an empty desk");
  eq(t.holderFor("page-head").hidden, true, "and no page title above it");
});

check("the live indicator is the only status, and it tracks the poll", () => {
  /* The mailbox counts went; the one fact an operator cannot otherwise know
     did not. A poll that has started failing must not read as a quiet day. */
  const t = load();
  t.ui.snap = snapshotWith([request({})], { active: true, following: 1 });
  t.renderDashboard();

  eq(t.holderFor("live-label").textContent, "Live", "healthy");
  eq(t.holderFor("live-indicator").className, "live", "no alarm styling");

  const failing = snapshotWith([request({})], { active: true, following: 1 });
  failing.poll.error = "PermanentFailure";
  t.ui.snap = failing;
  t.renderDashboard();

  eq(t.holderFor("live-label").textContent, "Reconnecting", "the failure surfaces");
  eq(/live-stalled/.test(t.holderFor("live-indicator").className), true, "and is styled as one");
});

check("the request card carries the reference and the time, not the field count", () => {
  const t = load();
  t.ui.snap = snapshotWith([request({ shipment_fields: 7 })], { active: true, following: 1 });
  t.renderDashboard();
  const text = t.holderFor("dashboard-list").textContent;

  eq(/R-1/.test(text), true, "the reference");
  eq(/Mumbai → Dubai/.test(text), true, "the lane");
  eq(/field\(s\)/.test(text), false, "extraction bookkeeping is gone");
  eq(/NEW REQUEST/.test(text), false, "and so is the badge");
});

check("a waiting-on-client step shows the hourglass, ours shows the dot", () => {
  const t = load();
  t.renderTimeline([
    { key: "clarification_sent", label: "Clarification sent", state: "done", at: "2026-08-29T10:00:00+05:30", note: null, waiting_on: null },
    { key: "reply_received", label: "Client reply received", state: "current", at: null, note: "Waiting for client reply", waiting_on: "client" },
    { key: "approval_decided", label: "Human approval", state: "pending", at: null, note: null, waiting_on: null },
  ]);
  const marks = t.holderFor("timeline").findAll((n) => n.className === "tl-mark");

  eq(marks[0].textContent, "\u2713", "done is a tick");
  eq(marks[1].textContent, "\u23F3", "waiting on the client is an hourglass");

  t.renderTimeline([
    { key: "approval_decided", label: "Human approval", state: "current", at: null, note: "Waiting for approval", waiting_on: "operator" },
  ]);
  eq(
    t.holderFor("timeline").findAll((n) => n.className === "tl-mark")[0].textContent,
    "\u25CF",
    "waiting on us is a filled dot"
  );
});

check("intermediate stages read as done once a later stage is done", () => {
  /* The bug: rate search and rate selection are proven in the worker process,
     and those audit events never reach the dashboard's trail \u2014 so the server
     leaves their rows "pending" even after human approval and quotation-sent
     are done. Rendered verbatim that is a pipeline running backwards. A timeline
     only moves forward: everything before the furthest step reached is done. */
  const t = load();
  t.renderTimeline([
    { key: "enquiry_received", label: "Enquiry email received", state: "done", at: "2026-08-29T10:00:00+05:30", note: null, waiting_on: null },
    { key: "validation", label: "Validation", state: "done", at: "2026-08-29T10:01:00+05:30", note: null, waiting_on: null },
    { key: "rate_search", label: "Rate search", state: "current", at: null, note: "Pending", waiting_on: null },
    { key: "rate_selected", label: "Rate selected", state: "pending", at: null, note: null, waiting_on: null },
    { key: "approval_decided", label: "Human approval", state: "done", at: "2026-08-29T10:05:00+05:30", note: null, waiting_on: null },
    { key: "quotation_sent", label: "Quotation sent", state: "done", at: "2026-08-29T10:06:00+05:30", note: null, waiting_on: null },
  ]);

  const marks = t.holderFor("timeline").findAll((n) => n.className === "tl-mark").map((n) => n.textContent);
  eq(marks[2], "\u2713", "rate search now shows done");
  eq(marks[3], "\u2713", "rate selected now shows done");
  eq(marks[4], "\u2713", "human approval unchanged");
  eq(marks[5], "\u2713", "quotation sent unchanged");

  /* A backfilled row has no timestamp of its own \u2014 it must read as completed,
     never as the "Pending" it would otherwise fall back to. */
  const whens = t.holderFor("timeline").findAll((n) => n.className === "tl-when").map((n) => n.textContent);
  eq(whens[2], "Completed", "rate search no longer reads Pending under a tick");
  eq(whens[3], "Completed", "rate selected no longer reads Pending under a tick");
});

check("a stage still ahead of the pipeline stays pending", () => {
  /* The correction must not run away: a step after the furthest one reached is
     genuinely not done, and the current step keeps its own marker. */
  const t = load();
  t.renderTimeline([
    { key: "enquiry_received", label: "Enquiry email received", state: "done", at: "2026-08-29T10:00:00+05:30", note: null, waiting_on: null },
    { key: "validation", label: "Validation", state: "current", at: null, note: "Pending", waiting_on: "operator" },
    { key: "rate_search", label: "Rate search", state: "pending", at: null, note: null, waiting_on: null },
  ]);

  const marks = t.holderFor("timeline").findAll((n) => n.className === "tl-mark").map((n) => n.textContent);
  eq(marks[0], "\u2713", "enquiry done");
  eq(marks[1], "\u25CF", "validation is the current step, unchanged");
  eq(marks[2], "\u25CB", "rate search ahead of the pipeline stays pending");
});

/* --- the page keeps itself up to date, with nothing to press -------------- */

/* A fetch stub that hands back a queue of state snapshots as text, the way the
   real endpoint does, and counts how many times it was asked. */
function statePages(...snapshots) {
  const calls = [];
  const stub = async (url) => {
    calls.push(url);
    const body = JSON.stringify(snapshots[Math.min(calls.length - 1, snapshots.length - 1)]);
    return { ok: true, status: 200, text: async () => body };
  };
  stub.calls = calls;
  return stub;
}

checkAsync("the page arms a repeating refresh and never a mailbox poll", async () => {
  const stub = statePages(snapshotWith([], { active: true }));
  const t = load(stub);

  t.watchForChanges();

  eq(intervals.length, 1, "one repeating timer");
  eq(intervals[0].ms, t.REFRESH_MS, "it runs on the page's refresh interval");
  await intervals[0].fn();
  eq(stub.calls.length, 1, "the tick read state");
  eq(stub.calls[0].startsWith("/api/live/state"), true, "state, not an action");
});

checkAsync("a new request appears without anyone clicking anything", async () => {
  /* The requirement, stated: the operator opens the dashboard, an enquiry
     arrives, and the row shows up on a tick of the page's own timer. */
  const stub = statePages(
    snapshotWith([], { active: true, following: 0 }),
    snapshotWith([request({ request_id: "R-FRESH" })], { active: true, following: 1 })
  );
  const t = load(stub);

  await t.refresh(true);
  eq(/Waiting for new enquiry/.test(t.holderFor("dashboard-list").textContent), true, "empty");

  t.watchForChanges();
  await intervals[0].fn();

  eq(
    /R-FRESH/.test(t.holderFor("dashboard-list").textContent),
    true,
    "the enquiry rendered itself on a timer tick"
  );
});

checkAsync("a view change is never dropped behind an in-flight read", async () => {
  /* A read can sit behind a mailbox poll holding the server's lock. A click
     discarded in that window would leave the operator on the screen they
     clicked away from, with nothing to tell them why. */
  let release = null;
  const gate = new Promise((resolve) => { release = resolve; });
  const bodies = [
    JSON.stringify(snapshotWith([], { active: true })),
    JSON.stringify(snapshotWith([request({ request_id: "R-SECOND" })], { active: true })),
  ];
  let n = 0;
  const t = load(async () => {
    const body = bodies[Math.min(n++, bodies.length - 1)];
    if (n === 1) await gate;
    return { ok: true, status: 200, text: async () => body };
  });

  const first = t.refresh(false);      // in flight, and stuck
  const clicked = t.refresh(true);     // the operator's click lands meanwhile
  release();
  await first;
  await clicked;

  eq(n, 2, "the queued view change ran once the read ahead of it finished");
  eq(
    /R-SECOND/.test(t.holderFor("dashboard-list").textContent),
    true,
    "and it is what the page ended up showing"
  );
});

checkAsync("an unchanged snapshot is not redrawn", async () => {
  /* A redraw replaces every node on the page. Doing that every few seconds
     when nothing has changed throws away scroll position and open folds, and
     reads as flicker rather than as live. */
  const t = load(statePages(snapshotWith([request({})], { active: true, following: 1 })));
  await t.refresh(true);
  const first = t.holderFor("dashboard-list").children[0];

  await t.refresh(false);

  eq(t.holderFor("dashboard-list").children[0] === first, true, "the page was left alone");
});

checkAsync("the poll clock moves without rebuilding the page", async () => {
  /* The regression this exists for: the server stamps every poll with the time
     it read the mailbox, so the payload differs on every tick. Comparing it
     whole rebuilt the page every few seconds forever — folds closed, scroll
     jumped, and a click landing mid-rebuild did nothing. */
  const first = snapshotWith([request({})], { active: true, following: 1 });
  const later = snapshotWith([request({})], { active: true, following: 1 });
  later.poll.last_checked_at = "2026-08-29T10:59:00+05:30";
  const t = load(statePages(first, later));

  await t.refresh(true);
  const card = t.holderFor("dashboard-list").children[0];

  await t.refresh(false);

  eq(t.holderFor("dashboard-list").children[0] === card, true, "the list was not rebuilt");
  eq(
    /10:59/.test(t.holderFor("live-indicator").attrs.title || ""),
    true,
    "and the indicator still tracked the read, so a working dashboard is legible"
  );
});

checkAsync("a real change still rebuilds the page", async () => {
  const first = snapshotWith([request({ request_id: "R-1" })], { active: true, following: 1 });
  const later = snapshotWith([request({ request_id: "R-2" })], { active: true, following: 1 });
  later.poll.last_checked_at = "2026-08-29T10:59:00+05:30";
  const t = load(statePages(first, later));

  await t.refresh(true);
  await t.refresh(false);

  eq(/R-2/.test(t.holderFor("dashboard-list").textContent), true, "the new state rendered");
});

checkAsync("the refresh pauses while a name is being typed into a decision", async () => {
  /* Rebuilding the view under a half-typed name takes the caret with it. */
  const stub = statePages(snapshotWith([], { active: true }));
  const t = load(stub);
  t.ui.editing = true;
  t.watchForChanges();

  await intervals[0].fn();

  eq(stub.calls.length, 0, "no fetch while the operator is typing");
});

checkAsync("an action releases the typing pause it inherited", async () => {
  /* The view is rebuilt by the action, so the blur that would have cleared
     this never fires — and a flag left set pauses the page for good. */
  const t = load(async () => ({ ok: true, status: 200, json: async () => snapshotWith([], {}) }));
  t.ui.snap = snapshotWith([], {});
  t.ui.editing = true;

  await t.post("clarification/approve", { by: "Aayush" }, "Sending\u2026");

  eq(t.ui.editing, false, "the automatic refresh resumes after a decision");
});

checkAsync("the refresh pauses while an action is in flight", async () => {
  const stub = statePages(snapshotWith([], { active: true }));
  const t = load(stub);
  t.ui.busy = true;
  t.watchForChanges();

  await intervals[0].fn();

  eq(stub.calls.length, 0, "the action's own response is the newer state");
});


/* --- a failed action has to be visible from whichever view you are on ----- */

function failingFetch() {
  return async () => ({
    ok: false,
    status: 500,
    json: async () => ({ error: "PermanentFailure" }),
  });
}

check("a request that could not be priced says so on its dashboard card", () => {
  const t = load();
  t.ui.snap = snapshotWith(
    [request({ rate_failure: "'Hyderabad' is not in the demo lane table" })],
    { active: true, following: 1 }
  );
  t.renderDashboard();
  const notes = t.holderFor("dashboard-list").findAll((n) => n.className === "waiting-note");

  eq(notes.length, 1, "one failure note");
  eq(/Hyderabad/.test(notes[0].textContent), true, "names the cause");
});

check("a priced request shows no failure note", () => {
  const t = load();
  t.ui.snap = snapshotWith([request({ rate_failure: null })], { active: true, following: 1 });
  t.renderDashboard();

  eq(
    t.holderFor("dashboard-list").findAll((n) => n.className === "waiting-note").length,
    0,
    "no note when nothing failed"
  );
});

check("the detail view explains an absent rate section", () => {
  const t = load();
  const section = t.sectionRates({
    rates: null,
    rate_failure: "'Hyderabad' is not in the demo lane table",
  });

  eq(section !== null, true, "a section is rendered");
  eq(/Hyderabad/.test(section.textContent), true, "names the cause");
});

check("the detail view renders nothing when there is no failure and no rates", () => {
  const t = load();
  eq(t.sectionRates({ rates: null, rate_failure: null }), null, "still nothing to show");
});

/* A door-delivery request whose returned airport rates were all excluded is
   handed to a person, who prices the door leg from these rows — so each row has
   to show what the rate actually offered, not just the carrier and the reason. */
function doorHandOverRates() {
  const row = (code, name, amount) => ({
    carrier_code: code, carrier_name: name, product: `${code} General`,
    amount, currency: "Rs", transit: "20h 30m", departure_date: "05/10/2026",
    source_ref: `webcargo-browser:05/10/2026:${code} General:#0`,
    reason: "service_not_available",
    detail: `${name} does not state door delivery for this rate; an undeclared capability is not offered`,
  });
  return {
    simulated: false, banner: null, adapter_id: "webcargo-browser",
    returned: 2, eligible_count: 0, excluded_count: 2,
    query: { origin: "Chennai (MAA)", destination: "Singapore (SIN)", weight_kg: 500, date: "2026-10-05" },
    eligible: [],
    excluded: [row("TK", "Turkish Cargo", "16900.00"), row("EK", "Emirates", "20762.00")],
    selection: null,
    strategy: "Fastest eligible transit — ranked by transit time, not price",
  };
}

check("door hand-over: excluded rows show each returned rate's service, date, transit and price", () => {
  const t = load();
  const section = t.sectionRates({
    rates: doorHandOverRates(),
    status: { state: "manual_review" },
  });
  const rows = section.findAll((n) => n.className === "excluded-rate small");
  eq(rows.length, 2, "one rate line per excluded row");
  eq(rows[0].textContent, "TK General · departs 05/10/2026 · ⏱ 20h 30m · 16900.00 Rs", "first row");
  eq(rows[1].textContent, "EK General · departs 05/10/2026 · ⏱ 20h 30m · 20762.00 Rs", "second row");
  eq(/No eligible rate — nothing will be quoted/.test(section.textContent), true, "still unquotable");
});

check("door hand-over: excluded rows start open for manual review, folded otherwise", () => {
  const t = load();
  const fold = (state) =>
    t.sectionRates({ rates: doorHandOverRates(), status: { state } })
      .find((n) => n.tagName === "details");
  eq("open" in fold("manual_review").attrs, true, "open for the operator to price from");
  eq("open" in fold("rate_selected").attrs, false, "folded when a rate was selected");
});

check("door hand-over: a missing price or transit reads as absent, never guessed", () => {
  const t = load();
  const rates = doorHandOverRates();
  rates.excluded = [{ ...rates.excluded[0], amount: null, currency: null, transit: null, departure_date: null }];
  const section = t.sectionRates({ rates, status: { state: "manual_review" } });
  const line = section.find((n) => n.className === "excluded-rate small");
  eq(line.textContent, "TK General · ⏱ — · price —", "absent values stay absent");
});

check("exactly one card is SELECTED when the winning carrier returns several rates", () => {
  /* Bug A: the SELECTED card was matched by carrier_code, so every rate from the
     winning carrier lit up SELECTED and inherited the winner's reason. Identity
     is source_ref now, so exactly one card is marked — and Bug B: its chip and
     its reason are the same spelling of the same duration, never "1110 minutes"
     beside "18h 30m". */
  const t = load();
  const qr = (source_ref, transit, amount) => ({
    carrier_code: "QR", carrier_name: "Qatar Airways", product: "QR General",
    amount, currency: "Rs", transit, source_ref,
  });
  const rates = {
    simulated: false, banner: null, adapter_id: "webcargo-browser",
    returned: 2, eligible_count: 2,
    query: { origin: "Bangalore", destination: "Manila", weight_kg: 320, date: "2026-09-25" },
    strategy: "Fastest eligible transit — ranked by transit time, not price",
    eligible: [qr("ref-fast", "18h 30m", "185597"), qr("ref-slow", "22h 30m", "90000")],
    excluded: [],
    selection: Object.assign(qr("ref-fast", "18h 30m", "185597"), {
      reason: "fastest eligible transit at 18h 30m", runners_up: [],
    }),
  };

  const section = t.sectionRates({ rates, rate_failure: null, rate_search_pending: false });

  eq(section.findAll((n) => n.className === "rate-ribbon").length, 1, "one SELECTED ribbon");
  const selectedCards = section.findAll((n) => /rate-selected/.test(n.className));
  eq(selectedCards.length, 1, "exactly one selected card despite two QR rates");

  const chip = selectedCards[0].find((n) => n.className === "transit-chip");
  const why = selectedCards[0].find((n) => n.className === "rate-why");
  eq(/18h 30m/.test(chip.textContent), true, "the selected card's chip is the winner's transit");
  eq(/18h 30m/.test(why.textContent), true, "and its reason states the same duration");
  eq(/minutes/.test(chip.textContent), false, "never a raw minute count");

  const chips = section.findAll((n) => n.className === "transit-chip").map((n) => n.textContent);
  eq(chips.some((c) => /22h 30m/.test(c)), true, "the slower QR rate keeps its own chip");
});

checkAsync("an action failing on the DASHBOARD renders a visible error", async () => {
  /* The regression: ui.error was only ever appended by renderDetail(), so a
     poll that failed while the dashboard was on screen — every poll on a fresh
     demonstration — set the error and displayed absolutely nothing. */
  const t = load(failingFetch());
  t.ui.snap = snapshotWith([], { active: true });
  t.ui.view = "dashboard";

  await t.post("poll", {}, "Checking mail\u2026");

  const banner = t.holderFor("action-error");
  eq(banner.hidden, false, "the banner is shown");
  eq(/PermanentFailure/.test(banner.textContent), true, "it names the failure");
});

checkAsync("an action failing on the DETAIL view still renders the error", async () => {
  const t = load(failingFetch());
  t.ui.snap = snapshotWith([], { active: true });
  t.ui.view = "detail";

  await t.post("poll", {}, "Checking mail\u2026");

  eq(t.holderFor("action-error").hidden, false, "shown in the other view too");
});

checkAsync("a successful action clears a previous error", async () => {
  const t = load(async () => ({ ok: true, status: 200, json: async () => snapshotWith([], {}) }));
  t.ui.snap = snapshotWith([], {});
  t.ui.view = "dashboard";
  t.ui.error = "PermanentFailure";

  await t.post("poll", {}, "Checking mail\u2026");

  eq(t.holderFor("action-error").hidden, true, "banner hidden again");
});


/* --- Phase 1: connection, next step, hand-over wording, History, gone --- */

/* A fetch that answers from a script: a snapshot object is a 200, `null` is a
   network failure (the server unreachable, as during a restart or deploy). */
function scriptedReads(...steps) {
  let i = 0;
  return async () => {
    const step = steps[Math.min(i, steps.length - 1)];
    i += 1;
    if (step === null) throw new Error("network down");
    const body = JSON.stringify(step);
    return { ok: true, status: 200, text: async () => body };
  };
}

checkAsync("B1: an unreachable server shows Disconnected and marks the last list stale", async () => {
  const snap = snapshotWith([request({ request_id: "R-SEEN" })], { active: true, following: 1 });
  const t = load(scriptedReads(snap, null));

  await t.refresh(true);
  eq(t.holderFor("live-label").textContent, "Live", "connected first");
  await t.refresh(false);

  eq(t.holderFor("live-label").textContent, "Disconnected", "the header stops claiming Live");
  eq(/live-stalled/.test(t.holderFor("live-indicator").className), true, "amber");
  const banner = t.holderFor("load-error");
  eq(banner.hidden, false, "the connection error is shown");
  eq(/Can’t reach the Quotation Desk server/.test(banner.textContent), true, "production wording");
  eq(/last successful update/.test(banner.textContent), true, "says the list is from the last update");
  eq(/may be out of date/.test(banner.textContent), true, "and does not imply it is current");
  eq(/demo/i.test(banner.textContent), false, "no demo wording");
  eq(/R-SEEN/.test(t.holderFor("dashboard-list").textContent), true, "the last list is kept");
  eq(t.holderFor("view-dashboard").className, "is-stale", "and visibly marked stale");
});

checkAsync("B1: a first read that fails claims no stale data", async () => {
  const t = load(scriptedReads(null));

  await t.refresh(true);

  eq(t.holderFor("live-label").textContent, "Disconnected", "not Live");
  const text = t.holderFor("load-error").textContent;
  eq(/Can’t reach the Quotation Desk server/.test(text), true, "the error is shown");
  eq(/last successful update/.test(text), false, "there was no earlier update to mention");
});

checkAsync("B1: reconnecting clears the stale marking even when nothing changed", async () => {
  const snap = snapshotWith([request({})], { active: true, following: 1 });
  const t = load(scriptedReads(snap, null, snap));

  await t.refresh(true);
  await t.refresh(false);
  await t.refresh(false);

  eq(t.holderFor("live-label").textContent, "Live", "live again");
  eq(t.holderFor("load-error").hidden, true, "the error is gone");
  eq(t.holderFor("view-dashboard").className, "", "no longer dimmed");
});

checkAsync("B1/C2: an action the server never answered is worded for production", async () => {
  const t = load(async () => { throw new Error("network down"); });
  t.ui.snap = snapshotWith([], {});
  t.ui.view = "dashboard";

  await t.post("clarification/approve", { by: "Ops" }, "Sending…");

  const text = t.holderFor("action-error").textContent;
  eq(/demo/i.test(text), false, "no demo server");
  eq(/Quotation Desk server did not respond/.test(text), true, "names what failed");
});

check("B2: the next step follows the summary, first match wins", () => {
  const t = load();
  const cases = [
    [{ goods_type_hold: { catalog: [] }, rate_failure: "x" }, "Choose the WebCargo goods type"],
    [{ awaiting_clarification: true }, "Review and approve the clarification draft"],
    [{ awaiting_decision: true }, "Approve or decline the quotation"],
    [{ status: { state: "manual_review", label: "MANUAL REVIEW", tone: "amber" } }, "Handle manually"],
    [{ rate_search_pending: true }, "Searching WebCargo for rates"],
    [{ rate_search_pending: true, worker_notice: "worker offline" }, "waiting for the rate-search worker"],
    [{ status: { state: "clarification_sent", label: "AWAITING CLIENT REPLY", tone: "blue" } },
      "Waiting on the client"],
    [{ rate_failure: "the date is in the past" }, "Check why the rate search failed"],
  ];
  for (const [overrides, expected] of cases) {
    const step = t.nextStep(request(overrides));
    eq(step !== null && step[1].includes(expected), true, `${JSON.stringify(overrides)} -> ${expected}`);
  }
  eq(t.nextStep(request({ status: { state: "validated", label: "VALIDATED", tone: "green" } })), null,
    "nothing to say when nothing is pending");
  eq(t.nextStep(request({ status: { state: "quotation_sent", label: "QUOTATION SENT", tone: "green" } })),
    null, "a settled request has no next step");
});

check("B2: the next step is on the card, and the status pill is unchanged", () => {
  const t = load();
  t.ui.snap = snapshotWith(
    [request({ awaiting_clarification: true, status: { state: "needs_info", label: "INFORMATION REQUIRED", tone: "amber" } })],
    { active: true, following: 1 }
  );
  t.renderDashboard();
  const holder = t.holderFor("dashboard-list");
  const line = holder.find((n) => /next-step/.test(n.className) && n.tagName === "p");

  eq(line !== null, true, "a next-step line is rendered");
  eq(/Next step/.test(line.textContent), true, "labelled");
  eq(/approve the clarification draft/.test(line.textContent), true, "with the operator action");
  eq(/next-step-action/.test(line.className), true, "styled as an action");
  eq(holder.find((n) => n.className === "pill pill-amber").textContent, "INFORMATION REQUIRED", "pill as before");
});

check("B3: a hand-over card is neutral and shows the recorded reason", () => {
  const t = load();
  const reason = "Client did not provide the required shipment details within the 30-minute response window.";
  t.ui.snap = snapshotWith(
    [request({ status: { state: "manual_review", label: "MANUAL REVIEW", tone: "amber" }, manual_review_notes: [reason] })],
    { active: true, following: 1 }
  );
  t.renderDashboard();
  const text = t.holderFor("dashboard-list").textContent;

  eq(/Handed to a person — Client did not provide/.test(text), true, "the actual reason");
  eq(/answer could not be used/.test(text), false, "no assumed cause");
});

check("B3: a hand-over with no recorded note shows the next step once, and invents no cause", () => {
  const t = load();
  t.ui.snap = snapshotWith(
    [request({ status: { state: "manual_review", label: "MANUAL REVIEW", tone: "amber" }, manual_review_notes: [] })],
    { active: true, following: 1 }
  );
  t.renderDashboard();
  const holder = t.holderFor("dashboard-list");

  eq(holder.findAll((n) => n.className === "waiting-note").length, 0,
    "no second line repeating what the next step already says");
  eq(/Handle manually/.test(holder.textContent), true, "the hand-over is still stated");
  eq(/answer could not be used/.test(holder.textContent), false, "no assumed cause");
});

check("B3: the detail hand-over card lists the notes and drops the old assumption", () => {
  const t = load();
  const withNotes = t.sectionManualReview({
    status: { state: "manual_review" },
    manual_review_notes: ["This message could not be read into a shipment."],
  }).textContent;
  eq(/HANDED TO A PERSON/.test(withNotes), true, "the pill says it");
  eq(/Automatic processing has stopped for this request/.test(withNotes), true, "neutral lead");
  eq(/could not be read into a shipment/.test(withNotes), true, "the recorded reason");
  eq(/The client replied, but their answer/.test(withNotes), false, "no assumed cause");

  const noNotes = t.sectionManualReview({ status: { state: "manual_review" }, manual_review_notes: [] });
  eq(noNotes !== null, true, "a restored hand-over still gets its card");
  eq(/not available in this view/.test(noNotes.textContent), true, "and says the reason is not here");

  eq(t.sectionManualReview({ status: { state: "validated" }, manual_review_notes: [] }), null,
    "no card for a request that was never handed over");
});

function historySnapshot(activeId) {
  const snap = snapshotWith([request({ request_id: activeId })], { active: true, following: 1 });
  snap.history = [request({ request_id: "R-OLD", status: { state: "quotation_sent", label: "QUOTATION SENT", tone: "green" } })];
  return snap;
}
const isDetails = (n) => n.tagName === "details";

check("B4: an open History stays open when the dashboard is rebuilt", () => {
  const t = load();
  t.ui.snap = historySnapshot("R-1");
  t.renderDashboard();
  const fold = t.holderFor("dashboard-list").find(isDetails);
  eq(fold.open === true, false, "closed by default");

  fold.open = true;
  fold.fire("toggle");
  t.ui.snap = historySnapshot("R-2");
  t.renderDashboard();
  const rebuilt = t.holderFor("dashboard-list").find(isDetails);

  eq(rebuilt !== fold, true, "it really was rebuilt");
  eq(rebuilt.open, true, "and is still open");
  eq(rebuilt.attrs.open, "", "open in the markup too");
});

checkAsync("B4: a polling update does not collapse History; closing it is remembered too", async () => {
  const t = load(scriptedReads(historySnapshot("R-1"), historySnapshot("R-2"), historySnapshot("R-3")));
  await t.refresh(true);
  const fold = t.holderFor("dashboard-list").find(isDetails);
  fold.open = true;
  fold.fire("toggle");

  await t.refresh(false);  // a real change, so the page is rebuilt
  const afterPoll = t.holderFor("dashboard-list").find(isDetails);
  eq(/R-2/.test(t.holderFor("dashboard-list").textContent), true, "the update rendered");
  eq(afterPoll.open, true, "History survived the poll");

  afterPoll.open = false;
  afterPoll.fire("toggle");
  await t.refresh(false);
  eq(t.holderFor("dashboard-list").find(isDetails).open === true, false, "and stays closed once closed");
});

check("B7: a request that disappeared clears its header, timeline and sections", () => {
  const t = load();
  t.holderFor("detail-header").replaceChildren(new Node("h1"));
  t.holderFor("detail-header").children[0].textContent = "Old request header";
  t.holderFor("timeline").replaceChildren(new Node("li"));
  t.ui.view = "detail";
  t.ui.selected = "R-GONE";
  t.ui.snap = snapshotWith([], { active: true });  // selected: null

  t.renderDetail();

  eq(t.holderFor("detail-header").children.length, 0, "no stale header");
  eq(t.holderFor("timeline").children.length, 0, "no stale timeline");
  eq(t.holderFor("timeline").hidden, true, "and no empty timeline box left on screen");
  eq(/no longer available/.test(t.holderFor("sections").textContent), true, "says why");
});

check("C1: validation issues show the message, not the internal rule id", () => {
  const t = load();
  const text = t.sectionShipment({
    shipment: [{ label: "Shipment date", value: "26 Sep 2024", status: "known", source: "enquiry" }],
    validation: {
      is_valid: false,
      issues: [{ rule_id: "SHIP_DATE_IN_PAST", message: "Shipment date 2024-09-26 is in the past." }],
    },
    merged: [], carried: [],
  }).textContent;

  eq(/Shipment date 2024-09-26 is in the past\./.test(text), true, "the message");
  eq(/SHIP_DATE_IN_PAST/.test(text), false, "no rule id");
});

check("C6: no narration copy on the clarification or merge cards", () => {
  const t = load();
  eq(/Deterministic wording/.test(t.sectionClarification(awaitingClarification()).textContent), false,
    "clarification card");
  const merged = t.sectionMerged({ reply_received: true, merged: ["pcs"], carried: ["origin"] });
  eq(/mail thread/.test(merged.textContent), false, "merge card");
});

(async () => {
  for (const [name, fn] of asyncChecks) {
    try {
      await fn();
      console.log(`  ok   ${name}`);
    } catch (err) {
      failures += 1;
      console.log(`  FAIL ${name}\n       ${err.message}`);
    }
  }
  console.log(failures ? `\n  ${failures} failure(s)` : "\n  all passed");
  process.exit(failures ? 1 : 0);
})();
