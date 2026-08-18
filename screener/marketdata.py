"""Market data layer: Polygon REST (daily bars, short interest, gainers/losers,
premarket snapshot, 1-min intraday, news) with a yfinance fallback for prices
and fundamentals."""

import random
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import pytz
import requests
import yfinance as yf
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from .config import console, POLYGON_KEY, POLYGON_BASE


# ──────────────────────────────────────────────
#  POLYGON DATA FETCHER  (Stocks Starter REST API)
# ──────────────────────────────────────────────

def _polygon_get(session, url: str, params: dict, retries: int = 3):
    """GET a Polygon endpoint with API-key auth and 429 back-off. Returns parsed JSON or None."""
    params = {**params, "apiKey": POLYGON_KEY}
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 ** attempt + random.uniform(0.2, 0.8))
                continue
            return None
        except requests.exceptions.RequestException:
            time.sleep(0.5 * (attempt + 1))
    return None


def fetch_polygon_daily(tickers: list, days_back: int = 400) -> dict:
    """
    Fetch split-adjusted daily OHLCV bars from Polygon's aggregates endpoint,
    one ticker per request, concurrently.

    Returns:
      {ticker: DataFrame} with Open/High/Low/Close/Volume columns and a
      DatetimeIndex — same shape yfinance/the strategies expect.
    """
    if not POLYGON_KEY:
        console.print(
            "[yellow]⚠ POLYGON_KEY not in environment — falling back to yfinance[/yellow]\n"
            "[dim]Add POLYGON_KEY=... to your .env to use Polygon for price data.[/dim]\n"
        )
        return {}

    end_date   = datetime.now().date()
    start_date = end_date - timedelta(days=days_back)
    start, end = start_date.isoformat(), end_date.isoformat()
    data_map   = {}

    def fetch_one(session, ticker):
        url = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
        data = _polygon_get(session, url, {"adjusted": "true", "sort": "asc", "limit": 50000})
        if not data or data.get("status") not in ("OK", "DELAYED") or not data.get("results"):
            return ticker, None
        rows = data["results"]
        if len(rows) < 15:
            return ticker, None
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
        df = df.set_index("timestamp")
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close", "Volume"])
        return (ticker, df) if len(df) >= 15 else (ticker, None)

    session = requests.Session()
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TaskProgressColumn(), console=console) as prog:
        task = prog.add_task(f"[cyan]Downloading {len(tickers)} tickers (Polygon)...", total=len(tickers))
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_one, session, t): t for t in tickers}
            for fut in as_completed(futures):
                try:
                    ticker, df = fut.result()
                    if df is not None:
                        data_map[ticker] = df
                except Exception:
                    pass
                prog.advance(task)

    if data_map:
        console.print(f"[green]✔ Polygon: {len(data_map)}/{len(tickers)} tickers loaded[/green]")
    else:
        console.print("[yellow]⚠ Polygon returned 0 tickers[/yellow]")
    return data_map


def fetch_polygon_short_interest(tickers: list, max_workers: int = 20) -> dict:
    """
    Pull the most recent FINRA short-interest record per ticker from Polygon.

    Returns:
      {ticker: {"short_interest": int, "avg_daily_volume": float, "days_to_cover": float}}
    """
    if not POLYGON_KEY:
        return {}

    result = {}

    def fetch_one(session, ticker):
        url = f"{POLYGON_BASE}/stocks/v1/short-interest"
        data = _polygon_get(session, url, {"ticker": ticker, "limit": 1000})
        if not data or not data.get("results"):
            return ticker, None
        latest = max(data["results"], key=lambda r: r.get("settlement_date", ""))
        return ticker, {
            "short_interest":   latest.get("short_interest"),
            "avg_daily_volume": latest.get("avg_daily_volume"),
            "days_to_cover":    latest.get("days_to_cover"),
        }

    session = requests.Session()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_one, session, t): t for t in tickers}
        for fut in as_completed(futures):
            try:
                ticker, rec = fut.result()
                if rec:
                    result[ticker] = rec
            except Exception:
                pass
    return result


def enrich_short_interest(info_map: dict) -> None:
    """
    Compute shortPercentOfFloat from Polygon short interest and write it into
    info_map in place (Polygon data overrides yfinance, which is often empty).
    Float base = yfinance floatShares, else sharesOutstanding.
    """
    if not POLYGON_KEY or not info_map:
        return
    si_map = fetch_polygon_short_interest(list(info_map.keys()))
    if not si_map:
        return
    enriched = 0
    for ticker, rec in si_map.items():
        info  = info_map.get(ticker, {})
        si    = rec.get("short_interest")
        base  = info.get("floatShares") or info.get("sharesOutstanding")
        if si and base and base > 0:
            info["shortPercentOfFloat"] = si / base
            enriched += 1
        if rec.get("days_to_cover") is not None:
            info["daysToCover"] = rec["days_to_cover"]
        info_map[ticker] = info
    console.print(f"[green]✔ Polygon short interest: {enriched} tickers enriched[/green]")


def fetch_polygon_movers() -> dict:
    """
    Polygon snapshot gainers + losers as a popularity/universe source.
    Top movers get a score weighted by absolute % change.

    Returns:
      {ticker: score}
    """
    if not POLYGON_KEY:
        return {}

    results = {}
    session = requests.Session()
    for direction in ("gainers", "losers"):
        url  = f"{POLYGON_BASE}/v2/snapshot/locale/us/markets/stocks/{direction}"
        data = _polygon_get(session, url, {})
        if not data or not data.get("tickers"):
            continue
        for obj in data["tickers"]:
            ticker = (obj.get("ticker") or "").upper()
            chg    = abs(obj.get("todaysChangePerc", 0) or 0)
            if ticker:
                results[ticker] = results.get(ticker, 0) + min(chg, 30)
        console.print(f"  [green]✔ Polygon {direction}: {len(data['tickers'])} tickers[/green]")
    return results


def fetch_polygon_premarket(tickers: list) -> dict:
    """
    Pull the full-market snapshot (one call) and extract live/extended-hours
    session state for our universe:
      pm_gap     — % change vs prior close (the premarket/live gap)
      pm_price   — last traded price (includes pre/post-market trades)
      pm_vol     — volume accumulated so far today (premarket volume before 9:30)
      prev_close — prior regular-session close

    Polygon's snapshot reflects extended-hours trades, so this is the piece the
    daily bars miss. Most meaningful in the premarket (6:00–9:30 ET) and
    after-hours windows; during the regular session it mirrors the live day bar.
    """
    if not POLYGON_KEY:
        return {}
    want    = set(tickers)
    url     = f"{POLYGON_BASE}/v2/snapshot/locale/us/markets/stocks/tickers"
    session = requests.Session()
    data    = _polygon_get(session, url, {})
    if not data or not data.get("tickers"):
        console.print("[yellow]⚠ Polygon premarket snapshot returned 0 tickers[/yellow]")
        return {}

    result = {}
    for obj in data["tickers"]:
        ticker = (obj.get("ticker") or "").upper()
        if ticker not in want:
            continue
        prev       = obj.get("prevDay") or {}
        day        = obj.get("day") or {}
        minbar     = obj.get("min") or {}
        ltrade     = obj.get("lastTrade") or {}
        prev_close = prev.get("c") or 0
        pm_price   = ltrade.get("p") or minbar.get("c") or day.get("c") or 0
        pm_vol     = day.get("v") or minbar.get("av") or 0
        if not pm_price or not prev_close:
            continue
        pm_gap = obj.get("todaysChangePerc")
        if pm_gap is None:
            pm_gap = (pm_price - prev_close) / prev_close * 100
        result[ticker] = {
            "pm_gap":     float(pm_gap or 0),
            "pm_price":   float(pm_price),
            "pm_vol":     float(pm_vol or 0),
            "prev_close": float(prev_close),
        }
    console.print(f"[green]✔ Polygon premarket snapshot: {len(result)} tickers (live gap + premarket volume)[/green]")
    return result


def fetch_polygon_intraday(tickers: list) -> dict:
    """
    Fetch 1-minute aggregate bars for the most recent trading session, used by
    Strategy 1's intraday_phase() model.

    Queries a short range (last 5 calendar days) so weekends/holidays resolve to
    the latest session with data, then keeps only that session's bars.

    Returns {ticker: DataFrame[Open,High,Low,Close,Volume]} with a DatetimeIndex.
    Note: Stocks Starter minute data is 15-min delayed — fine for screening the
    intraday structure, not for live order fills.
    """
    if not POLYGON_KEY or not tickers:
        return {}

    end_date   = datetime.now().date()
    start_date = end_date - timedelta(days=5)
    start, end = start_date.isoformat(), end_date.isoformat()
    data_map   = {}

    def fetch_one(session, ticker):
        url  = f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}"
        data = _polygon_get(session, url, {"adjusted": "true", "sort": "asc", "limit": 50000})
        if not data or data.get("status") not in ("OK", "DELAYED") or not data.get("results"):
            return ticker, None
        rows = data["results"]
        if len(rows) < 15:
            return ticker, None
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
        df = df.set_index("timestamp")
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close", "Volume"])
        # Keep only the most recent session present in the range.
        last_day = df.index.normalize().max()
        df = df[df.index.normalize() == last_day]
        return (ticker, df) if len(df) >= 15 else (ticker, None)

    session = requests.Session()
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(fetch_one, session, t): t for t in tickers}
        for fut in as_completed(futures):
            try:
                ticker, df = fut.result()
                if df is not None:
                    data_map[ticker] = df
            except Exception:
                pass

    if data_map:
        console.print(f"[green]✔ Polygon intraday: {len(data_map)}/{len(tickers)} tickers (1-min bars)[/green]")
    else:
        console.print("[yellow]⚠ Polygon intraday: 0 tickers (market may be pre-open)[/yellow]")
    return data_map


def fetch_polygon_news(tickers: list, hours: int = 48, max_workers: int = 20) -> dict:
    """
    Pull recent Polygon ticker-news per symbol — the piece that tells you whether
    there's a REAL catalyst behind a move, versus a random volume spike or a pump.
    A fresh headline (earnings, FDA, contract, upgrade) is what separates a runner
    that keeps going from one that fades by lunch.

    Returns {ticker: {"count": int, "title": str, "age_h": float}} for names with
    at least one article inside the window (default last 48h). Names with no recent
    news simply don't appear — which is itself a signal (an unexplained spike).
    """
    if not POLYGON_KEY or not tickers:
        return {}

    now_utc = datetime.now(pytz.utc)
    cutoff  = now_utc - timedelta(hours=hours)
    result  = {}

    def fetch_one(session, ticker):
        url  = f"{POLYGON_BASE}/v2/reference/news"
        data = _polygon_get(session, url,
                            {"ticker": ticker, "limit": 10,
                             "order": "desc", "sort": "published_utc"})
        if not data or not data.get("results"):
            return ticker, None
        recent = []
        for a in data["results"]:
            ts = a.get("published_utc")
            if not ts:
                continue
            try:
                dt = pd.to_datetime(ts, utc=True).to_pydatetime()
            except Exception:
                continue
            if dt >= cutoff:
                recent.append((dt, a.get("title", "") or ""))
        if not recent:
            return ticker, None
        recent.sort(reverse=True)
        latest_dt, latest_title = recent[0]
        age_h = (now_utc - latest_dt).total_seconds() / 3600
        return ticker, {"count": len(recent), "title": latest_title[:70], "age_h": round(age_h, 1)}

    session = requests.Session()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_one, session, t): t for t in tickers}
        for fut in as_completed(futures):
            try:
                ticker, d = fut.result()
                if d:
                    result[ticker] = d
            except Exception:
                pass

    if result:
        console.print(f"[green]✔ Polygon news: fresh catalyst found for {len(result)}/{len(tickers)} candidates[/green]")
    else:
        console.print("[yellow]⚠ Polygon news: no recent headlines (or endpoint not on plan)[/yellow]")
    return result


# ──────────────────────────────────────────────
#  DATA FETCHER
# ──────────────────────────────────────────────

def fetch_price_data(tickers: list, period: str = "1y", skip_polygon: bool = False) -> dict:
    console.print(f"[dim][DEBUG] fetch_price_data: starting with {len(tickers)} tickers[/dim]")
    # Try Polygon first (if key available and not overridden)
    polygon_map = {} if skip_polygon else fetch_polygon_daily(tickers, days_back=400)
    if skip_polygon:
        console.print(f"[dim][DEBUG] Polygon skipped (--override_polygon)[/dim]")
    else:
        console.print(f"[dim][DEBUG] Polygon returned {len(polygon_map)} tickers[/dim]")

    if polygon_map:
        # Polygon succeeded for some; use it and only yfinance for missing
        missing = [t for t in tickers if t not in polygon_map]
        console.print(f"[dim][DEBUG] Missing from Polygon: {len(missing)} tickers[/dim]")
        if not missing:
            console.print(f"[green]✔ Price data: {len(polygon_map):,} tickers loaded (Polygon)[/green]")
            return polygon_map
        # Fall through: fetch missing via yfinance
        tickers_to_fetch = missing
    else:
        # Polygon not available; fetch all from yfinance
        console.print(f"[dim][DEBUG] Polygon returned empty, fetching all {len(tickers)} from yfinance[/dim]")
        tickers_to_fetch = tickers

    # Fetch from yfinance (full set or missing set)
    data_map = polygon_map.copy() if polygon_map else {}
    batch_size = 30
    batches = [tickers_to_fetch[i:i+batch_size] for i in range(0, len(tickers_to_fetch), batch_size)]

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TaskProgressColumn(), console=console) as prog:
        task = prog.add_task(f"[cyan]Downloading {len(tickers_to_fetch)} tickers (yfinance)...",
                             total=len(batches))
        for batch in batches:
            try:
                raw = yf.download(batch, period=period, interval="1d",
                                  group_by="ticker", auto_adjust=True,
                                  progress=False, threads=True)
                for ticker in batch:
                    try:
                        df = raw[ticker] if len(batch) > 1 else raw
                        if df is not None and len(df) >= 15:
                            df = df.dropna(subset=["Close", "Volume"])
                            if len(df) >= 15:
                                data_map[ticker] = df
                    except Exception:
                        pass
            except Exception:
                pass
            prog.advance(task)
            time.sleep(0.2)

    source = "Polygon+yfinance" if polygon_map else "yfinance"
    console.print(f"[green]✔ Price data: {len(data_map):,} tickers loaded ({source})[/green]")
    return data_map


def fetch_fundamentals(tickers: list, limit: int = 300) -> dict:
    info_map = {}
    sample   = tickers[:limit]
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=console) as prog:
        task = prog.add_task(f"[cyan]Fetching fundamentals ({len(sample)})...", total=len(sample))
        for ticker in sample:
            try:
                info_map[ticker] = yf.Ticker(ticker).info
            except Exception:
                pass
            prog.advance(task)
            time.sleep(0.08)
    console.print(f"[green]✔ Fundamentals: {len(info_map):,} tickers[/green]")
    return info_map

