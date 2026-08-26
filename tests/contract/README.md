# Contract tests

One shared suite per port, run against **every** implementation of it.

This is the load-bearing test tier. A single suite defines what `RateSearchPort`
means and runs against the mock today and the real adapter later, so the mock
cannot drift into being easier to satisfy than reality. Without it, "we swapped in
the real adapter and everything broke" is discovered at integration time instead
of at design time.

Empty until Phase 4, when the first adapter exists to run a contract against.
