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
    " watchForChanges, REFRESH_MS," +
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

check("the dashboard shows only this session's requests, with no counters", () => {
  const t = load();
  t.ui.snap = snapshotWith([request({ request_id: "R-NEW" })], { active: true, following: 1 });
  t.renderDashboard();
  const text = t.holderFor("dashboard-list").textContent;

  eq(/R-NEW/.test(text), true, "the request is on the page");
  eq(/Earlier enquiries/.test(text), false, "no band of historical work exists any more");
  eq(/request\(s\)/.test(text), false, "no counter");
  eq(bandHeadings(t.holderFor("dashboard-list")).length, 0, "no subheading for a single band");
});

check("a message that stated no shipment gets its own quiet band", () => {
  /* Still shown, never hidden: the operator has to be able to check the
     classification rather than trust it. */
  const t = load();
  t.ui.snap = snapshotWith(
    [request({ request_id: "R-1" }), request({ request_id: "R-2", is_enquiry: false })],
    { active: true, following: 2 }
  );
  t.renderDashboard();

  eq(
    bandHeadings(t.holderFor("dashboard-list")).join(","),
    "Other messages",
    "one quiet subheading"
  );
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
