"""Run one scan straight into the database, without going through the server.

Normally cron POSTs to /api/scan so the running server owns the single worker
slot and a scheduled scan can never collide with one you started from your
phone. This module is the fallback for when the server is down:

    .venv/bin/python -m webapp.scan_once --morning

It writes to the same SQLite file, so whenever the server comes back the results
are already there.
"""

import argparse
import sys
from datetime import datetime

import pandas as pd

from screener import engine

from . import db

PRESETS = {
    "morning": [1, 2, 3],
    "overnight": [4, 5, 6],
    "ten-am": [7],
    "swing": [2, 4, 8],
    "all": engine.ALL_STRATEGIES,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run a scan directly into the screener DB.")
    ap.add_argument("--preset", choices=sorted(PRESETS), default="all")
    ap.add_argument("--strategies", type=int, nargs="+", choices=engine.ALL_STRATEGIES,
                    help="Explicit strategy list; overrides --preset.")
    ap.add_argument("--sources", nargs="+", default=None)
    ap.add_argument("--max", type=int, default=600)
    ap.add_argument("--sleepers", type=int, default=150)
    ap.add_argument("--large", action="store_true")
    ap.add_argument("--override_polygon", action="store_true",
                    help="Skip Polygon and use yfinance only.")
    ap.add_argument("--export", action="store_true",
                    help="Also write the per-strategy CSVs, as the old CLI did.")
    args = ap.parse_args(argv)

    strategies = args.strategies or PRESETS[args.preset]
    params = {
        "sources": args.sources or list(engine.DEFAULT_SOURCES),
        "strategies": list(strategies),
        "max_tickers": args.max,
        "skip_polygon": args.override_polygon,
        "large": args.large,
        "sleepers": args.sleepers,
        "via": "scan_once",
    }

    db.init()
    run_id = db.save_run(params)
    try:
        exports = engine.run_scan(
            sources=params["sources"],
            max_tickers=params["max_tickers"],
            strategies=params["strategies"],
            skip_polygon=params["skip_polygon"],
            sleepers=params["sleepers"],
            large=params["large"],
            progress=lambda phase, pct, detail="": (
                print(f"[{pct * 100:5.1f}%] {detail}", flush=True) if detail else None),
        )
    except Exception as e:
        db.finish_run(run_id, "error", f"{type(e).__name__}: {e}")
        print(f"scan failed: {e}", file=sys.stderr)
        return 1

    db.save_results(run_id, exports)
    db.finish_run(run_id, "done")
    db.prune_runs()

    if args.export:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        for name, data in exports.items():
            if name == "heat" or not data:   # heat is a dict of lists, not a table
                continue
            fname = f"{name}_{ts}.csv"
            pd.DataFrame(data).to_csv(fname, index=False)
            print(f"wrote {fname}")
    counts = {k: len(v) for k, v in exports.items() if isinstance(v, list)}
    print(f"run {run_id} saved: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
