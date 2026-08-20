"""Single-slot background worker.

Only one job runs at a time. The pipeline already fans out to
ThreadPoolExecutor(max_workers=20) against Polygon and yfinance, so two
concurrent scans would just trade rate-limit errors with each other.

The job runs in a **child process**, not a thread, for one reason: Stop has to
actually stop it. A Python thread cannot be killed from outside — you can only
ask it to notice a flag, which it will not do while it is blocked in a socket
read inside yfinance. A child process can be signalled dead mid-request, so the
Stop button is a real kill (SIGTERM, then SIGKILL if it lingers).

The job's progress lives in memory (it's ephemeral, and the page polls it every
couple of seconds) and reaches the parent over a queue, but its *results* go
straight to SQLite from the child. That's what makes closing your phone
mid-scan free: the browser can drop off entirely and the finished rows are
still on disk when it comes back.
"""

import multiprocessing as mp
import queue as queuelib
import signal
import threading
import traceback
import uuid
from datetime import datetime, timezone

from screener import engine

from . import db

# fork keeps startup instant (the child inherits pandas/yfinance already
# imported) — spawn would re-import the world on every run, which on a Pi costs
# several seconds per scan.
_mp = mp.get_context("fork")

_lock = threading.Lock()
_current = None          # dict, or None when nothing has ever run
_proc = None             # the running child process
_watcher = None          # thread draining the child's progress queue

MAX_LOG = 60
TERM_GRACE = 3.0         # seconds to wait after SIGTERM before SIGKILL


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status() -> dict:
    """Snapshot of the current/last job, safe to serialize."""
    with _lock:
        if _current is None:
            return {"status": "idle"}
        return dict(_current, log=list(_current["log"]))


def is_running() -> bool:
    with _lock:
        return _current is not None and _current["status"] == "running"


def _emit(phase: str, pct: float, detail: str = "") -> None:
    with _lock:
        if _current is None:
            return
        _current["phase"] = phase
        _current["pct"] = round(float(pct), 3)
        if detail:
            _current["detail"] = detail
            _current["log"].append({"t": _utcnow(), "msg": detail})
            del _current["log"][:-MAX_LOG]


def _begin(kind: str, params: dict) -> dict:
    global _current
    job = {
        "job_id": uuid.uuid4().hex[:12],
        "kind": kind,
        "status": "running",
        "phase": "starting",
        "pct": 0.0,
        "detail": "",
        "log": [],
        "started_at": _utcnow(),
        "finished_at": None,
        "run_id": None,
        "ticker": params.get("ticker"),
        "params": params,
        "stopping": False,
        "error": None,
    }
    _current = job
    return job


def _end(status_: str, error: str = None) -> None:
    with _lock:
        if _current is None:
            return
        _current["status"] = status_
        _current["finished_at"] = _utcnow()
        _current["error"] = error
        _current["stopping"] = False
        if status_ == "done":
            _current["pct"] = 1.0


# ──────────────────────────────────────────────
#  CHILD SIDE
#  Everything below _child_main runs in the forked process. It talks back over
#  `q` and writes its results to SQLite itself, so a kill can never leave the
#  parent holding half a scan.
# ──────────────────────────────────────────────

def _child_main(kind: str, params: dict, q) -> None:
    # uvicorn installs asyncio signal handlers in the parent, and fork copies
    # them. Restoring the defaults is what makes terminate() actually terminate
    # instead of running the parent's shutdown handler in here.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
    # Never reuse the parent's inherited SQLite handle across the fork.
    db.reset_connection()

    if kind == "scan":
        _child_scan(params, q)
    else:
        _child_rate(params, q)


def _child_scan(params: dict, q) -> None:
    run_id = None
    try:
        run_id = db.save_run(params)
        q.put(("run_id", run_id))
        exports = engine.run_scan(
            sources=params.get("sources"),
            max_tickers=params.get("max_tickers", 600),
            strategies=params.get("strategies"),
            skip_polygon=params.get("skip_polygon", False),
            sleepers=params.get("sleepers", 150),
            large=params.get("large", False),
            s1_price=params.get("s1_price"),
            other_price=params.get("other_price"),
            progress=lambda phase, pct, detail="": q.put(("progress", phase, pct, detail)),
        )
        db.save_results(run_id, exports)
        db.finish_run(run_id, "done")
        db.prune_runs()
        q.put(("done", None))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        if run_id is not None:
            db.finish_run(run_id, "error", msg)
        q.put(("error", msg))


def _child_rate(params: dict, q) -> None:
    ticker = params["ticker"]
    try:
        q.put(("progress", "rating", 0.1, f"Fetching data for {ticker}…"))
        ratings, info, err = engine.rate_ticker(ticker)
        if err:
            q.put(("error", err))
            return
        q.put(("progress", "rating", 0.9, f"Scored {ticker} across 8 strategies"))
        db.save_rating(ticker, {
            "ratings": ratings,
            "company": info.get("longName") or info.get("shortName") or ticker,
            "sector": info.get("sector"),
            "industry": info.get("industry"),
        })
        q.put(("done", None))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        q.put(("error", msg))


# ──────────────────────────────────────────────
#  PARENT SIDE
# ──────────────────────────────────────────────

def _apply(msg, run_id, result):
    """Fold one queued message from the child into the parent's job state."""
    tag = msg[0]
    if tag == "run_id":
        run_id = msg[1]
        with _lock:
            if _current is not None:
                _current["run_id"] = run_id
    elif tag == "progress":
        _emit(msg[1], msg[2], msg[3])
    else:                           # "done" / "error"
        result = (tag, msg[1])
    return run_id, result


def _watch(proc, q) -> None:
    """Follow one child to its end and settle the job's final status."""
    run_id, result = None, None
    while proc.is_alive():
        try:
            msg = q.get(timeout=0.25)
        except (queuelib.Empty, OSError, EOFError):
            continue
        run_id, result = _apply(msg, run_id, result)
    # The child can exit with messages still in the pipe — its verdict is
    # usually the last one, so drain before deciding what happened.
    while True:
        try:
            msg = q.get_nowait()
        except (queuelib.Empty, OSError, EOFError):
            break
        run_id, result = _apply(msg, run_id, result)
    proc.join(5)

    with _lock:
        killed = _current is not None and _current["stopping"]

    if killed:
        # The child was signalled mid-work, so whatever it had scored is
        # incomplete. Drop it: half a universe scored against a full one is
        # misleading, and the previous run is still on disk.
        if run_id is not None:
            db.finish_run(run_id, "cancelled", "stopped by user")
            db.prune_runs()
        _emit("cancelled", 1.0, "Scan stopped")
        _end("cancelled")
    elif result and result[0] == "done":
        _end("done")
    elif result and result[0] == "error":
        _emit("error", 1.0, result[1])
        _end("error", result[1])
    else:
        # No verdict and nobody asked for a stop — the child died on its own
        # (an OOM kill on the Pi looks exactly like this).
        msg = f"worker exited unexpectedly (code {proc.exitcode})"
        if run_id is not None:
            db.finish_run(run_id, "error", msg)
        _emit("error", 1.0, msg)
        _end("error", msg)


def _start(kind: str, params: dict) -> dict:
    """Fork a child for this job. Returns the job dict, or None if one is busy."""
    global _proc, _watcher
    with _lock:
        if _current is not None and _current["status"] == "running":
            return None
        job = _begin(kind, params)

    q = _mp.Queue()
    _proc = _mp.Process(target=_child_main, args=(kind, params, q),
                        name=f"{kind}-worker", daemon=True)
    _proc.start()
    _watcher = threading.Thread(target=_watch, args=(_proc, q),
                                name=f"{kind}-watch", daemon=True)
    _watcher.start()
    return job


def start_scan(params: dict) -> dict:
    """Kick off a background scan. Returns the job dict, or None if one is busy."""
    return _start("scan", params)


def start_rating(ticker: str) -> dict:
    """Score one ticker across all 8 strategies. Shorter (~10-20s) but uses the
    same slot, since it hits the same upstream APIs."""
    return _start("rate", {"ticker": (ticker or "").strip().upper()})


def request_stop() -> bool:
    """Kill the running job. False when there is nothing to kill.

    SIGTERM first so the child dies at once; if it is wedged in an
    uninterruptible spot, SIGKILL follows a few seconds later. _watch() sees the
    exit and settles the job as "cancelled".
    """
    with _lock:
        if _current is None or _current["status"] != "running" or _proc is None:
            return False
        _current["stopping"] = True
        _current["detail"] = "Stopping…"
        proc = _proc

    def kill():
        proc.terminate()
        proc.join(TERM_GRACE)
        if proc.is_alive():
            proc.kill()

    threading.Thread(target=kill, name="job-kill", daemon=True).start()
    return True
