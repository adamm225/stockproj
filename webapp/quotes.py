"""Live quotes from Finnhub.

Why Finnhub and not Polygon: the Polygon key on this account is a Stocks Starter
plan, which is 15-minute delayed — its real-time endpoints (/v2/last/trade,
/v2/last/nbbo) both return 403 NOT_AUTHORIZED. Finnhub's /quote works on this
key, so it is the better source for a "what is it doing right now" number.

This is deliberately pull-only: nothing here runs on a timer. The page fetches
quotes when you tap Refresh, so a phone sitting in your pocket costs nothing.
The Finnhub tier here allows 60 calls/minute and each ticker is one call, so a
20-row strategy tab costs 20 of that budget per refresh.

Note these quotes are *not* what the strategies scored on. The scan's Price is
the setup anchor that Target and Stop derive from; this is a separate number
shown alongside it.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

FINNHUB_QUOTE = "https://finnhub.io/api/v1/quote"

TTL = 10             # seconds — guards against double-taps, not a polling cache
MAX_TICKERS = 60     # one refresh must not blow the per-minute budget
WORKERS = 8
TIMEOUT = 10

_cache = {}          # ticker -> (fetched_at, quote dict)
_lock = threading.Lock()


def _key() -> str:
    return os.environ.get("FIN_KEY", "")


def _fetch_one(session, ticker: str):
    """One Finnhub quote. Returns a dict, or None if it can't be had.

    Finnhub answers unknown symbols with c=0 rather than an error, so a zero
    current price is treated as no data.
    """
    try:
        r = session.get(FINNHUB_QUOTE, params={"symbol": ticker, "token": _key()},
                        timeout=TIMEOUT)
        if r.status_code == 429:
            return {"error": "rate_limited"}
        if not r.ok:
            return None
        q = r.json()
        if not q or not q.get("c"):
            return None
        return {
            "price": q.get("c"),           # current
            "prev_close": q.get("pc"),
            "change": q.get("d"),          # vs prev close, absolute
            "change_pct": q.get("dp"),     # vs prev close, percent
            "high": q.get("h"),
            "low": q.get("l"),
            "open": q.get("o"),
            "ts": q.get("t"),              # epoch seconds of the last trade
        }
    except Exception:
        return None


def get_quotes(tickers) -> dict:
    """Live quotes for up to MAX_TICKERS symbols, keyed by ticker."""
    if not _key():
        return {"_error": "FIN_KEY is not set"}

    wanted, now = [], time.time()
    out = {}
    seen = set()
    for t in tickers:
        t = (t or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        with _lock:
            hit = _cache.get(t)
        if hit and now - hit[0] < TTL:
            out[t] = hit[1]
        else:
            wanted.append(t)
        if len(seen) >= MAX_TICKERS:
            break

    if wanted:
        with requests.Session() as session:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                results = list(ex.map(lambda t: (t, _fetch_one(session, t)), wanted))
        stamp = time.time()
        for t, q in results:
            if not q:
                continue
            if q.get("error"):
                out["_error"] = "Finnhub rate limit hit — wait a moment and refresh again."
                continue
            out[t] = q
            with _lock:
                _cache[t] = (stamp, q)

    return out
