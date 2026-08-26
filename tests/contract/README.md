# Contract tests

One shared suite per port, run against **every** implementation of it.

This is the load-bearing test tier. A single suite defines what `RateSearchPort`
means and runs against the mock today and the real adapter later, so the mock
cannot drift into being easier to satisfy than reality. Without it, "we swapped in
the real adapter and everything broke" is discovered at integration time instead
of at design time.

Still empty. One adapter exists so far — `FixtureEmailSource` (Phase 3) — and its
conformance to `EmailSource` is currently asserted in
`tests/unit/test_fixture_email_source.py`. A shared `EmailSource` contract suite
becomes worthwhile once a second implementation exists to run it against; the
`RateSearchPort` suite arrives with the mock WebCargo adapter in Phase 8.
