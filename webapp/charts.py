"""Daily candle charts, proxied.

The old dashboard pointed an <img> straight at finviz's chart.ashx. That URL
now 301s through two hops to a different host, and the browser refuses the
result (ERR_BLOCKED_BY_RESPONSE.NotSameOrigin), so the images silently fail.

Fetching server-side and serving the bytes from our own origin fixes that,
and buys three other things:
  * one place to update when finviz moves the endpoint again,
  * a short cache, so scrolling a 20-row list doesn't re-hit finviz per tap,
  * the phone never talks to a third party directly.
"""

import threading
import time

import requests

# Resolved final endpoint (chart.ashx redirects here). Sized 2x for retina.
CHART_ENDPOINT = "https://charts2-node.finviz.com/chart"
CHART_PARAMS = {
    "w": 932, "h": 438, "bw": 2, "bm": 1, "bb": 1,
    "tf": "d", "s": "linear", "pm": 0, "am": 0, "ct": "candle_stick",
    "o[0][ot]": "sma", "o[0][op]": 20, "o[0][oc]": "DC32B363",
    "o[1][ot]": "sma", "o[1][op]": 50, "o[1][oc]": "FF8F33C6",
    "o[2][ot]": "sma", "o[2][op]": 200, "o[2][oc]": "DCB3326D",
}

TTL = 300           # seconds; intraday candles move, but not every tap
MAX_ENTRIES = 200
TIMEOUT = 12

_cache = {}         # ticker -> (fetched_at, content_type, bytes)
_lock = threading.Lock()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def get_chart(ticker: str):
    """Return (content_type, png_bytes) or (None, None) if it can't be fetched."""
    ticker = ticker.upper()
    now = time.time()

    with _lock:
        hit = _cache.get(ticker)
        if hit and now - hit[0] < TTL:
            return hit[1], hit[2]

    try:
        r = requests.get(CHART_ENDPOINT, params={**CHART_PARAMS, "t": ticker},
                         headers={"User-Agent": UA, "Referer": "https://finviz.com/"},
                         timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200 or not r.content:
            return None, None
        ctype = r.headers.get("Content-Type", "image/png")
        if "image" not in ctype:
            return None, None
    except Exception:
        return None, None

    with _lock:
        if len(_cache) >= MAX_ENTRIES:            # cheap eviction: drop the oldest
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[ticker] = (now, ctype, r.content)
    return ctype, r.content
