"""SQLite persistence for scan results.

Strategy rows are flat dicts of already-formatted display strings, and the keys
differ per strategy (S2 has 19, S4 has 25). Rather than model 25 columns x 8
strategies, each row is stored whole as JSON; only `ticker` and `confidence` are
lifted into real columns because those are the two things we sort and look up by.

WAL mode lets the HTTP layer read while a scan is mid-write.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "SCREENER_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "screener.db"),
)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,          -- running | done | error
    params_json TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS results (
    run_id     INTEGER NOT NULL,
    strategy   TEXT    NOT NULL,        -- export key, e.g. "s2_swing"
    rank       INTEGER NOT NULL,
    ticker     TEXT,
    confidence REAL,
    row_json   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_results_run_strat ON results(run_id, strategy);
CREATE INDEX IF NOT EXISTS idx_runs_status       ON runs(status, finished_at);

CREATE TABLE IF NOT EXISTS ratings (
    ticker       TEXT PRIMARY KEY,
    rated_at     TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    """One connection per thread — the worker and the server each get their own."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def reset_connection() -> None:
    """Forget this thread's cached handle so the next conn() opens a fresh one.

    Called at the top of a forked job process: the child inherits the parent's
    SQLite connection object, and two processes sharing one file descriptor is
    a corruption hazard. Opening our own is the fix.
    """
    _local.conn = None


def init() -> None:
    c = conn()
    c.executescript(SCHEMA)
    c.commit()
    # A server killed mid-scan leaves a run stuck in "running" forever. Nothing
    # is executing at import time, so any such row is a leftover.
    c.execute("UPDATE runs SET status='error', error='interrupted', finished_at=? "
              "WHERE status='running'", (now(),))
    c.commit()


# ──────────────────────────────────────────────
#  RUNS
# ──────────────────────────────────────────────

def save_run(params: dict) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO runs (started_at, status, params_json) VALUES (?, 'running', ?)",
        (now(), json.dumps(params)))
    c.commit()
    return cur.lastrowid


def finish_run(run_id: int, status: str = "done", error: str = None) -> None:
    c = conn()
    c.execute("UPDATE runs SET status=?, finished_at=?, error=? WHERE id=?",
              (status, now(), error, run_id))
    c.commit()


def list_runs(limit: int = 30) -> list:
    rows = conn().execute(
        "SELECT id, started_at, finished_at, status, params_json, error "
        "FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_run_dict(r) for r in rows]


def latest_run_id() -> int | None:
    r = conn().execute(
        "SELECT id FROM runs WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
    return r["id"] if r else None


def get_run(run_id: int) -> dict | None:
    r = conn().execute(
        "SELECT id, started_at, finished_at, status, params_json, error "
        "FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run_dict(r) if r else None


def _run_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
        "status": r["status"],
        "error": r["error"],
        "params": json.loads(r["params_json"]) if r["params_json"] else {},
    }


# ──────────────────────────────────────────────
#  RESULTS
# ──────────────────────────────────────────────

def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def save_results(run_id: int, exports: dict) -> None:
    """Persist an engine.run_scan() exports dict.

    Skips "heat" — it is a dict of two lists (industry gainers/losers), not a
    pick table, so it is stored under its own pseudo-strategy keys.
    """
    c = conn()
    payload = []
    for strategy, rows in exports.items():
        if strategy == "heat":
            if isinstance(rows, dict):
                for side in ("gainers", "losers"):
                    for i, row in enumerate(rows.get(side) or []):
                        payload.append((run_id, f"heat_{side}", i, None, None,
                                        json.dumps(row, default=str)))
            continue
        for i, row in enumerate(rows or []):
            if not isinstance(row, dict):
                continue
            payload.append((run_id, strategy, i, row.get("Ticker"),
                            _as_float(row.get("Confidence")),
                            json.dumps(row, default=str)))
    if payload:
        c.executemany(
            "INSERT INTO results (run_id, strategy, rank, ticker, confidence, row_json) "
            "VALUES (?,?,?,?,?,?)", payload)
    c.commit()


def get_results(run_id: int, strategy: str = None) -> dict:
    """Rows for a run, grouped by strategy key and ordered by original rank."""
    if strategy:
        rows = conn().execute(
            "SELECT strategy, row_json FROM results WHERE run_id=? AND strategy=? "
            "ORDER BY rank", (run_id, strategy)).fetchall()
    else:
        rows = conn().execute(
            "SELECT strategy, row_json FROM results WHERE run_id=? "
            "ORDER BY strategy, rank", (run_id,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["strategy"], []).append(json.loads(r["row_json"]))
    return out


def prune_runs(keep: int = 60) -> int:
    """Drop all but the newest `keep` runs so the DB stays small on an SD card."""
    c = conn()
    old = c.execute("SELECT id FROM runs ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (keep,)).fetchall()
    if not old:
        return 0
    ids = [(r["id"],) for r in old]
    c.executemany("DELETE FROM results WHERE run_id=?", ids)
    c.executemany("DELETE FROM runs WHERE id=?", ids)
    c.commit()
    return len(ids)


# ──────────────────────────────────────────────
#  RATINGS
# ──────────────────────────────────────────────

def save_rating(ticker: str, payload: dict) -> None:
    c = conn()
    c.execute("INSERT INTO ratings (ticker, rated_at, payload_json) VALUES (?,?,?) "
              "ON CONFLICT(ticker) DO UPDATE SET rated_at=excluded.rated_at, "
              "payload_json=excluded.payload_json",
              (ticker.upper(), now(), json.dumps(payload, default=str)))
    c.commit()


def get_rating(ticker: str) -> dict | None:
    r = conn().execute("SELECT ticker, rated_at, payload_json FROM ratings WHERE ticker=?",
                       (ticker.upper(),)).fetchone()
    if not r:
        return None
    return {"ticker": r["ticker"], "rated_at": r["rated_at"],
            **json.loads(r["payload_json"])}
