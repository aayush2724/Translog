"""adapters.webcargo

Will implement RateSearchPort.

MockWebCargoAdapter reads fixture rate sets. RealWebCargoAdapter is a stub that
raises; its RealRateMapper raises UnresolvedFieldMapping for transit time (AMB-1)
rather than guessing a field.
"""
