/* Behavioural test for the committed-Goods-Type read (_JS_GOODS_TYPE_SELECTED).
 *
 * The bug this exists for was invisible to the Python suite: the read is a
 * JavaScript string evaluated in the browser, and the FakeDriver returns a
 * hand-written value rather than running it. The live WebCargo control is AntD
 * v3, which renders a committed value in `.ant-select-selection-selected-value`;
 * the read used only the AntD v4/v5 class `.ant-select-selection-item`, so a
 * confirmed pick read back as "" ("the placeholder") and the search refused to
 * run.
 *
 * This loads the REAL read out of pages.py and runs it against synthetic AntD
 * v3 / v4 / empty controls, so the selector is actually exercised.
 *
 * Run directly: node tests/js/goods_type_selected.test.js
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(
  __dirname, "..", "..", "src", "translog_quote", "adapters", "webcargo", "browser", "pages.py"
);
const src = fs.readFileSync(SOURCE, "utf8");

const match = src.match(/_JS_GOODS_TYPE_SELECTED = """([\s\S]*?)"""/);
if (!match) {
  console.error("could not find _JS_GOODS_TYPE_SELECTED in pages.py");
  process.exit(1);
}
const readSrc = match[1];

/* Build a synthetic Goods Type select. `by` maps a CSS class to the element the
 * control exposes for it (or nothing). `getAttribute`/`textContent` model the
 * AntD node. */
function selectWith(by) {
  const sel = {
    querySelector: (cls) => (Object.prototype.hasOwnProperty.call(by, cls) ? by[cls] : null),
  };
  return { querySelector: (q) => (q === '[id^="goodsType"]' ? sel : null) };
}

function evalRead(documentStub) {
  const sandbox = { document: documentStub };
  vm.createContext(sandbox);
  const fn = vm.runInContext("(" + readSrc + ")", sandbox);
  return fn();
}

let failures = 0;
function eq(name, got, expected) {
  if (got !== expected) {
    console.error(`  FAIL ${name}: got ${JSON.stringify(got)} expected ${JSON.stringify(expected)}`);
    failures++;
  } else {
    console.log(`  ok   ${name}`);
  }
}

/* --- AntD v3: the live WebCargo control (the fix) ----------------------- */
eq(
  "v3 committed value in .ant-select-selection-selected-value is read",
  evalRead(selectWith({
    ".ant-select-selection-selected-value": { getAttribute: () => null, textContent: "General Cargo" },
  })),
  "General Cargo",
);

eq(
  "v3 title attribute is preferred over textContent",
  evalRead(selectWith({
    ".ant-select-selection-selected-value": {
      getAttribute: (a) => (a === "title" ? "0000 - General Cargo" : null),
      textContent: "General Cargo",
    },
  })),
  "0000 - General Cargo",
);

/* --- AntD v4/v5: fallback preserved ------------------------------------- */
eq(
  "v4 committed value in .ant-select-selection-item is still read (fallback)",
  evalRead(selectWith({
    ".ant-select-selection-item": { getAttribute: () => null, textContent: "0000 - General Cargo" },
  })),
  "0000 - General Cargo",
);

/* --- empty select: only the placeholder shows -> "" --------------------- */
eq(
  "an empty select (no selected-value, no item) reads as ''",
  evalRead(selectWith({})),
  "",
);

/* --- no Goods Type select on the page at all -> "" ---------------------- */
eq(
  "a missing Goods Type select reads as ''",
  evalRead({ querySelector: () => null }),
  "",
);

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("\nall goods-type-selected read cases passed");
