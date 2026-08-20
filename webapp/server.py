"""Starlette app: JSON API over the scan database + the static frontend.

Deliberately not FastAPI — that would pull pydantic + pydantic-core onto the Pi
to validate nine endpoints that take at most four fields. Starlette is already
installed (it's FastAPI's foundation), so this adds no dependencies at all.

LAN-only by design: there is no authentication here. Do not forward port 8000
through the router.
"""

import contextlib
import os

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from screener import engine

from . import charts, db, jobs, quotes

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
INDEX = os.path.join(STATIC_DIR, "index.html")

VALID_SOURCES = {"finviz", "yahoo", "reddit", "nasdaq", "insider", "quality",
                 "momentum", "finnhub", "movers", "webull", "robinhood"}


# ──────────────────────────────────────────────
#  READ ROUTES  (all served straight from SQLite — single-digit ms)
# ──────────────────────────────────────────────

def _index_html() -> str:
    """index.html with {{V}} replaced by the newest asset mtime.

    app.css and app.js are then fetched as /static/app.js?v=<stamp>, so an
    edit to either produces a URL the browser has never seen. Without this a
    phone happily runs a cached script against a freshly deployed page — new
    markup, old code, controls that render as dead text.
    """
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    stamp = 0
    for name in ("app.js", "app.css"):
        try:
            stamp = max(stamp, int(os.path.getmtime(os.path.join(STATIC_DIR, name))))
        except OSError:
            pass
    return html.replace("{{V}}", str(stamp))


async def index(request):
    # no-store on the page itself: it is tiny, and it is what carries the
    # version stamps that bust everything else.
    return HTMLResponse(_index_html(), headers={"Cache-Control": "no-store"})


async def api_strategies(request):
    return JSONResponse({
        "strategies": engine.strategy_meta(),
        "sources": sorted(VALID_SOURCES),
        "default_sources": engine.DEFAULT_SOURCES,
    })


def _run_payload(run_id: int) -> dict:
    run = db.get_run(run_id)
    if not run:
        return None
    return {"run": run, "strategies": db.get_results(run_id)}


async def api_latest(request):
    run_id = db.latest_run_id()
    if run_id is None:
        return JSONResponse({"run": None, "strategies": {}})
    return JSONResponse(_run_payload(run_id))


async def api_runs(request):
    try:
        limit = min(int(request.query_params.get("limit", 30)), 200)
    except ValueError:
        limit = 30
    return JSONResponse({"runs": db.list_runs(limit)})


async def api_run(request):
    try:
        run_id = int(request.path_params["run_id"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "bad run id"}, status_code=400)
    payload = _run_payload(run_id)
    if payload is None:
        return JSONResponse({"error": "no such run"}, status_code=404)
    return JSONResponse(payload)


async def api_rating(request):
    ticker = request.path_params["ticker"].upper()
    rating = db.get_rating(ticker)
    if rating is None:
        return JSONResponse({"error": "not rated yet"}, status_code=404)
    return JSONResponse(rating)


# ──────────────────────────────────────────────
#  JOB ROUTES
# ──────────────────────────────────────────────

async def api_job(request):
    return JSONResponse(jobs.status())


MAX_BAND_PRICE = 100000.0


def _clean_band(raw):
    """Normalize a price band from the request body into (min, max) or None.

    Accepts {"min": 1, "max": 20} or [1, 20]; either end may be null for "no
    bound". None (or an all-null band) means "leave the strategy on its own
    default gate" — which is how Strategy 1 stays sub-$10 when the UI has no
    custom band set for it.
    """
    if isinstance(raw, dict):
        pair = (raw.get("min"), raw.get("max"))
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        pair = tuple(raw)
    else:
        return None

    out = []
    for v in pair:
        if v is None or v == "":
            out.append(None)
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        if v != v or v < 0:            # NaN or negative
            return None
        out.append(min(v, MAX_BAND_PRICE))

    lo, hi = out
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return [lo, hi]


def _clean_scan_params(body: dict) -> dict:
    sources = [s for s in body.get("sources") or engine.DEFAULT_SOURCES
               if s in VALID_SOURCES]
    strategies = [k for k in body.get("strategies") or engine.ALL_STRATEGIES
                  if k in engine.STRATEGIES]
    try:
        max_tickers = int(body.get("max_tickers", 600))
    except (TypeError, ValueError):
        max_tickers = 600
    return {
        "sources": sources or list(engine.DEFAULT_SOURCES),
        "strategies": strategies or list(engine.ALL_STRATEGIES),
        "max_tickers": max(50, min(max_tickers, 2000)),
        "skip_polygon": bool(body.get("skip_polygon", False)),
        "large": bool(body.get("large", False)),
        "s1_price": _clean_band(body.get("s1_price")),
        "other_price": _clean_band(body.get("other_price")),
    }


async def api_scan(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    job = jobs.start_scan(_clean_scan_params(body))
    if job is None:
        return JSONResponse({"error": "a job is already running",
                             "job": jobs.status()}, status_code=409)
    return JSONResponse(job, status_code=202)


async def api_job_stop(request):
    """Kill the running job.

    A real kill, not a polite request: the work runs in a child process, so it
    is signalled dead wherever it happens to be — mid-download included.
    Partial results are discarded, and the previous run stays the latest.
    """
    if not jobs.request_stop():
        return JSONResponse({"error": "no job is running", "job": jobs.status()},
                            status_code=409)
    return JSONResponse({"stopping": True, "job": jobs.status()}, status_code=202)


async def api_rate(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalpha():
        return JSONResponse({"error": "invalid ticker"}, status_code=400)
    job = jobs.start_rating(ticker)
    if job is None:
        return JSONResponse({"error": "a job is already running",
                             "job": jobs.status()}, status_code=409)
    return JSONResponse(job, status_code=202)


async def api_chart(request):
    """Daily candle PNG, fetched server-side. See webapp/charts.py for why this
    is proxied rather than pointed at finviz directly from the page."""
    ticker = request.path_params["ticker"].upper()
    if not ticker.replace(".", "").replace("-", "").isalpha() or len(ticker) > 6:
        return Response(status_code=400)
    ctype, body = await run_in_threadpool(charts.get_chart, ticker)
    if not body:
        return Response(status_code=404)
    return Response(body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=300"})


async def api_quotes(request):
    """Live Finnhub quotes for the tickers the page is currently showing.

    Pull-only — the page calls this when you tap Refresh, never on a timer.
    Polygon is not used here: this account's Starter plan is 15-minute delayed
    and its real-time endpoints return 403.
    """
    raw = (request.query_params.get("tickers") or "").strip()
    if not raw:
        return JSONResponse({"error": "no tickers"}, status_code=400)
    tickers = [t for t in (x.strip().upper() for x in raw.split(",")) if t]
    data = await run_in_threadpool(quotes.get_quotes, tickers)
    err = data.pop("_error", None)
    return JSONResponse({"quotes": data, "error": err,
                         "fetched_at": db.now()})


async def api_health(request):
    return JSONResponse({"ok": True, "db": db.DB_PATH,
                         "latest_run": db.latest_run_id(),
                         "job": jobs.status().get("status")})


routes = [
    Route("/", index),
    Route("/api/health", api_health),
    Route("/api/strategies", api_strategies),
    Route("/api/latest", api_latest),
    Route("/api/runs", api_runs),
    Route("/api/run/{run_id}", api_run),
    Route("/api/rating/{ticker}", api_rating),
    Route("/api/chart/{ticker}", api_chart),
    Route("/api/quotes", api_quotes),
    Route("/api/job", api_job),
    Route("/api/scan", api_scan, methods=["POST"]),
    Route("/api/job/stop", api_job_stop, methods=["POST"]),
    Route("/api/rate", api_rate, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]

@contextlib.asynccontextmanager
async def lifespan(app):
    # Creates the schema on first boot and clears any run left "running" by a
    # server that was killed mid-scan.
    db.init()
    yield


app = Starlette(
    routes=routes,
    middleware=[Middleware(GZipMiddleware, minimum_size=500)],
    lifespan=lifespan,
)
