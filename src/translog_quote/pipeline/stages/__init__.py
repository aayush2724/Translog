"""One module per pipeline stage.

Populated as behaviour lands: ingest and correlate, extract and merge, validate,
clarify, search, filter, select, review, send, resolve intent. Each stage is a
separate module so it can be tested in isolation and so no function performs two
stages of the rate pipeline.
"""
