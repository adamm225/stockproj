"""Web UI for the stock screener.

A background worker runs screener.engine.run_scan() (~3 minutes) and writes the
results to SQLite; the HTTP layer only ever reads that database, so page loads
are milliseconds regardless of how long a scan takes.

    webapp.db      SQLite schema + read/write helpers
    webapp.jobs    single-slot background worker + progress state
    webapp.server  Starlette app (JSON API + static frontend)
"""
