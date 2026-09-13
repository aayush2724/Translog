/* Behavioural tests for the WebCargo results-settle predicate (_JS_RESULTS_STATE).
 *
 * The bug these exist for was invisible to the Python suite: the predicate is a
 * JavaScript string evaluated in the browser, and the Python fakes return a
 * hand-written state dict rather than running it. A zero-rate search shows
 * empty-state wording ("No results found" / "There may be no results because:")
 * that the predicate's `noRates` regexes did not match, so the extractor waited
 * the full timeout and failed instead of returning a valid empty outcome.
 *
 * This loads the REAL predicate out of pages.py and runs it against synthetic
 * pages, so the empty-state wording is actually exercised.
 *
 * Run directly: node tests/js/results_state.test.js
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(
  __dirname, "..", "..", "src", "translog_quote", "adapters", "webcargo", "browser", "pages.py"
);
const src = fs.readFileSync(SOURCE, "utf8");

const match = src.match(/_JS_RESULTS_STATE = """([\s\S]*?)"""/);
if (!match) {
  console.error("could not find _JS_RESULTS_STATE in pages.py");
  process.exit(1);
}
// Inside the Python triple-quoted string the regex backslashes are written as
// "\\s"/"\\d"; the browser receives single-backslash "\s"/"\d". Collapse them
// so node runs exactly what the browser runs.
const predicateSrc = match[1].replace(/\\\\/g, "\\");

const HASH = "#ebookings/dynamic-results";

function evalPredicate(texts, hash = HASH) {
  const sandbox = {
    document: { querySelectorAll: () => texts.map((t) => ({ textContent: t })) },
    location: { hash },
  };
  vm.createContext(sandbox);
  const fn = vm.runInContext("(" + predicateSrc + ")", sandbox);
  return fn();
}

let failures = 0;
function check(name, got, expect) {
  for (const key of Object.keys(expect)) {
    if (got[key] !== expect[key]) {
      console.error(`  FAIL ${name}: ${key}=${JSON.stringify(got[key])} expected ${JSON.stringify(expect[key])}`);
      failures++;
      return;
    }
  }
  console.log(`  ok   ${name}`);
}

/* The verbatim count phrase WebCargo shows on a rates-bearing page (Matrix). */
const COUNT_MATRIX =
  "We found the 11 cheapest rates. These rates only include freight, fuel and security by default. Other surcharges may apply.";

/* --- empty searches must be recognised as empty (the fix) --------------- */

// The worker's exact zero-rate case (BOM->DXB, 30 Sep, 750kg): headline
// "No results found" plus "There may be no results because:".
check("empty: 'No results found' + 'There may be no results because:'",
  evalPredicate(["No results found", "There may be no results because:", "BOMBAY, INDIA"]),
  { onResults: true, settled: false, loading: false, empty: true });

// The criteria-level empty wording.
check("empty: 'No results found for your search criteria.'",
  evalPredicate(["No results found for your search criteria.",
    "We couldn't find any results for these airlines"]),
  { onResults: true, settled: false, loading: false, empty: true });

/* --- rates present must stay settled and NOT be marked empty ------------- */

// Rates present AND the "No results found for your search criteria" section is
// also on the page (it shows even on successful searches): the !anyCount guard
// must keep this settled, not empty.
check("rates + empty-section present: settled, not empty",
  evalPredicate([COUNT_MATRIX,
    "No results found for your search criteria.",
    "We couldn't find any results for these airlines"]),
  { onResults: true, settled: true, loading: false, empty: false });

// Full-list authoritative count phrase.
check("rates: 'Showing the 60 lowest rates' -> settled",
  evalPredicate(["Showing the 60 lowest rates"]),
  { onResults: true, settled: true, loading: false, empty: false });

/* --- loading must not be mistaken for empty ----------------------------- */

check("loading + stale 'No results found': neither settled nor empty",
  evalPredicate(["Your results are loading", "No results found"]),
  { onResults: true, settled: false, loading: true, empty: false });

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("\nall passed");
