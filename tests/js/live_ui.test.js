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
  });
}

function load(fetchStub) {
  const context = makeContext(fetchStub);
  const source =
    fs.readFileSync(SOURCE, "utf8") +
    "\n;globalThis.__t = { ui, canDecide, sectionClarification, sectionApproval," +
    " renderDashboard, renderTimeline, holderFor: (id) => document.getElementById(id) };";
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
    poll: { new_messages: 0, skipped_internal: 0, deferred: 0, enquiries: 0, unrecognised: 0 },
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
      is_enquiry: true, in_demonstration: true, is_new: true,
      not_enquiry_reason: null, waiting_replies: 0,
    },
    overrides
  );
}

function headings(holder) {
  return holder.findAll((n) => n.tagName === "h2").map((n) => n.textContent);
}

check("an earlier request is kept on the page, below the demonstration", () => {
  const t = load();
  t.ui.snap = snapshotWith(
    [
      request({ request_id: "R-NEW" }),
      request({ request_id: "R-OLD", in_demonstration: false, is_new: false }),
    ],
    { active: true, following: 1, earlier_requests: 1 }
  );
  t.renderDashboard();
  const holder = t.holderFor("dashboard-list");
  const titles = headings(holder);

  eq(titles[0], "This demonstration", "the demonstration leads");
  eq(titles.includes("Earlier enquiries"), true, "earlier work is still shown, not removed");
});

check("the demonstration scope line admits what was not read", () => {
  const t = load();
  t.ui.snap = snapshotWith([request({})], {
    active: true, following: 1, earlier_requests: 2, outside_messages: 5,
  });
  t.renderDashboard();
  const scope = t.holderFor("demo-scope").textContent;

  eq(scope.includes("2 earlier request(s) kept below"), true, "earlier requests admitted");
  eq(scope.includes("5 older mailbox message(s) not read"), true, "unread mail admitted");
});

check("a NEW REQUEST badge marks the fresh enquiry and nothing else", () => {
  const t = load();
  t.ui.snap = snapshotWith(
    [
      request({ request_id: "R-NEW", is_new: true }),
      request({ request_id: "R-OLD", in_demonstration: false, is_new: false }),
    ],
    { active: true, following: 1, earlier_requests: 1 }
  );
  t.renderDashboard();
  const badges = t.holderFor("dashboard-list").findAll(
    (n) => n.className === "badge-new"
  );

  eq(badges.length, 1, "exactly one NEW REQUEST badge");
  eq(badges[0].textContent, "NEW REQUEST", "badge text");
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

check("the check-mail button never renders a null child", () => {
  const t = load();
  /* The idle label is a plain string; a null spinner slot used to be
     stringified into it, giving "nullCheck mail". */
  eq(t.ui.busy, false, "idle");
});

console.log(failures ? `\n  ${failures} failure(s)` : "\n  all passed");
process.exit(failures ? 1 : 0);
