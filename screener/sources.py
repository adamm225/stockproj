"""Universe construction: every popularity/quality source (Finviz, Yahoo,
StockTwits, Webull, Robinhood, Reddit, Finnhub, OpenInsider, NASDAQ FTP) plus
build_universe(), which fans them out in parallel and merges weighted scores."""

import os
import re
import time
import random
from collections import defaultdict
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from .config import console
from .marketdata import fetch_polygon_movers


# ──────────────────────────────────────────────
#  REDDIT CONFIG  (edit here or use env vars)
# ──────────────────────────────────────────────
REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID", "YOUR_CLIENT_ID_HERE")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "YOUR_SECRET_HERE")
REDDIT_USER_AGENT    = "stock_screener_v4 by /u/your_username"

REDDIT_SUBS = ["wallstreetbets", "stocks", "investing", "StockMarket", "pennystocks", "Daytrading"]

TICKER_BLACKLIST = {
    "A", "I", "IT", "IS", "BE", "AM", "AN", "AT", "BY", "DO", "GO", "HE", "IF",
    "IN", "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE",
    "ALL", "AND", "ARE", "BUT", "CAN", "CEO", "CFO", "COO", "CTO", "DD", "DID",
    "DUE", "EPS", "ETF", "FOR", "GET", "GOT", "HAS", "HIT", "HOW", "ITS", "LOL",
    "LOW", "NEW", "NOT", "NOW", "OTC", "OUT", "OWN", "PAY", "PE", "PER", "PIN",
    "PUTS", "RH", "RIP", "SEC", "SET", "SHE", "SOLD", "SOME", "SPY", "THE",
    "TOO", "TWO", "USE", "WAS", "WHO", "WHY", "WTF", "YOY", "YOU", "YOUR",
    "YOLO", "MOON", "BULL", "BEAR", "GAIN", "LOSS", "HODL", "FOMO", "FMOC",
    "FED", "GDP", "IMO", "IRA", "QQQ", "DIA", "VIX", "UVXY", "TVIX",
    "EOD", "ATH", "ATL", "LMAO", "TLDR", "WSB", "APES", "IIRC", "IMO",
    "TBH", "AH", "PM", "ER", "PT", "SL", "TP", "RR", "DD", "OI", "IV",
}


def _load_valid_tickers() -> set:
    """The real NASDAQ+NYSE ticker universe (newPlan/scripta.py's output) — used
    to drop garbage/typo'd/delisted symbols BEFORE spending API calls on them.
    Returns an empty set (no filtering applied) if the file hasn't been generated."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "newPlan", "stockTickers.txt")
    if not os.path.exists(path):
        console.print(f"[yellow]⚠ {path} not found — skipping ticker validity filter "
                      f"(run `python newPlan/scripta.py` to generate it)[/yellow]")
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip().upper() for line in f if line.strip()}


VALID_TICKERS = _load_valid_tickers()


# ──────────────────────────────────────────────
#  FINVIZ  (fixed v4 — proper selectors + retry)
# ──────────────────────────────────────────────

# Rotate User-Agents to avoid blocks
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

def _finviz_headers() -> dict:
    return {
        "User-Agent": random.choice(_UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://finviz.com/",
    }


def _finviz_parse_tickers(html: str) -> list:
    """
    Parse tickers from Finviz screener HTML.
    Handles both the legacy table layout and the newer React-rendered one.
    Strategy: find all <a> tags whose href matches quote.ashx?t=TICKER
    which is the most stable selector across Finviz redesigns.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    tickers = []

    # Method 1: quote links  href="/quote.ashx?t=AAPL..."
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r'[?&]t=([A-Z]{1,5})(?:&|$)', href)
        if m:
            t = m.group(1)
            if t not in TICKER_BLACKLIST and t.isalpha():
                tickers.append(t)

    if tickers:
        return tickers

    # Method 2: data-boxover attributes (Finviz tooltip links)
    for tag in soup.find_all(attrs={"data-boxover": True}):
        text = tag.get_text(strip=True).upper()
        if 1 <= len(text) <= 5 and text.isalpha() and text not in TICKER_BLACKLIST:
            tickers.append(text)

    if tickers:
        return tickers

    # Method 3: table cells — look for cell with class "screener-link-primary"
    for a in soup.find_all("a", class_=re.compile(r"screener.link", re.I)):
        t = a.get_text(strip=True).upper()
        if 1 <= len(t) <= 5 and t.isalpha() and t not in TICKER_BLACKLIST:
            tickers.append(t)

    return tickers


def fetch_finviz_active(top_n: int = 300, max_pages: int = 5) -> dict:
    """
    Scrape Finviz screener using the v=111 (overview) layout.
    Uses proper URL encoding, rotated UA, and exponential back-off on 429/403.

    Screener filters used:
      exch_nasd           = NASDAQ only
      o=-volume           = sort by volume desc
      o=-change           = sort by % change desc
      cap_smallover       = small cap and above (avoids sub-penny trash)
    """
    results = {}
    screens = [
        # (filter_string,          label,            weight)
        ("exch_nasd&o=-volume",               "NASDAQ Volume",      1.2),
        ("exch_nasd&cap_smallover&o=-volume", "NASDAQ Small+ Vol",  1.1),
        ("exch_nasd&o=-change",               "NASDAQ % Gainers",   1.3),
        ("exch_nasd&o=change",                "NASDAQ % Losers",    0.8),  # bounce plays
        ("exch_nasd&cap_micro&o=-volume",     "NASDAQ Micro Vol",   0.9),
        ("exch_nasd&cap_nano&o=-volume",      "NASDAQ Nano Vol",    0.7),
    ]

    session = requests.Session()
    # Warm up the session with a GET on the homepage so cookies are set
    try:
        session.get("https://finviz.com/", headers=_finviz_headers(), timeout=8)
        time.sleep(random.uniform(0.8, 1.5))
    except Exception:
        pass

    base_url = "https://finviz.com/screener.ashx?v=111&f={f}&r={r}"

    for f_str, label, weight in screens:
        page_tickers = []
        consecutive_empty = 0

        for page in range(max_pages):   # 20 tickers per page per filter
            row_start = page * 20 + 1
            url = base_url.format(f=f_str, r=row_start)

            html = None
            for attempt in range(3):
                try:
                    resp = session.get(url, headers=_finviz_headers(), timeout=12)
                    if resp.status_code == 200:
                        html = resp.text
                        break
                    elif resp.status_code in (403, 429):
                        wait = 2 ** attempt + random.uniform(1, 3)
                        console.print(f"  [yellow]⚠ Finviz {resp.status_code} on {label} p{page+1} — waiting {wait:.1f}s[/yellow]")
                        time.sleep(wait)
                    elif resp.status_code == 404:
                        break  # no more pages
                    else:
                        break
                except requests.exceptions.Timeout:
                    time.sleep(2 ** attempt)
                except Exception:
                    break

            if html is None:
                break

            page_result = _finviz_parse_tickers(html)
            if not page_result:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0
                page_tickers.extend(page_result)

            # Polite crawl delay — critical to not get IP-banned
            time.sleep(random.uniform(1.0, 2.2))

            if len(page_tickers) >= top_n // len(screens):
                break

        # Dedupe preserving order
        seen = set()
        unique = []
        for t in page_tickers:
            if t not in seen:
                seen.add(t)
                unique.append(t)

        for rank, ticker in enumerate(unique):
            base_score = (len(unique) - rank) * weight
            results[ticker] = results.get(ticker, 0) + base_score

        if unique:
            console.print(f"  [green]✔ Finviz {label}: {len(unique)} tickers[/green]")
        else:
            console.print(f"  [yellow]⚠ Finviz {label}: 0 tickers (likely blocked or market closed)[/yellow]")

        time.sleep(random.uniform(1.5, 3.0))   # between screens

    return results


def fetch_finviz_trending() -> dict:
    """
    Scrape Finviz /news.ashx and /quote.ashx pages for trending tickers.
    Also hits the Finviz /elite/screener page for trending signals.
    """
    from bs4 import BeautifulSoup
    results = {}
    urls_to_try = [
        "https://finviz.com/news.ashx",
        "https://finviz.com/",       # homepage ticker widgets
    ]

    ticker_re = re.compile(r'\b([A-Z]{2,5})\b')

    for url in urls_to_try:
        try:
            r = requests.get(url, headers=_finviz_headers(), timeout=10)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            freq = defaultdict(int)

            # All ticker links on the page
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                m = re.search(r'[?&]t=([A-Z]{1,5})(?:&|$)', href)
                if m:
                    t = m.group(1)
                    if t not in TICKER_BLACKLIST:
                        freq[t] += 8   # direct link = strong signal

            # Also scan text of headlines for $TICKER style or plain caps
            for tag in soup.find_all(["a", "td", "span"]):
                text = tag.get_text(" ", strip=True)
                for t in ticker_re.findall(text.upper()):
                    if t not in TICKER_BLACKLIST and 2 <= len(t) <= 5:
                        freq[t] += 2

            for t, cnt in freq.items():
                results[t] = results.get(t, 0) + cnt

            time.sleep(random.uniform(1.0, 2.0))
        except Exception:
            pass

    if results:
        console.print(f"  [green]✔ Finviz news/trending: {len(results)} tickers[/green]")
    else:
        console.print("  [yellow]⚠ Finviz news: 0 tickers[/yellow]")

    return results


# ──────────────────────────────────────────────
#  STOCKTWITS  (v4 — multiple endpoints)
# ──────────────────────────────────────────────

def fetch_stocktwits_sentiment(top_n: int = 100) -> dict:
    """
    StockTwits free API — hits 4 endpoints to maximize coverage:

    1. /trending/symbols          — overall trending (sometimes empty)
    2. /streams/suggested.json    — editor-curated hot symbols
    3. /streams/watchlist.json    — most-watched
    4. Per-symbol sentiment scan  — for the tickers we already know are hot
       from other sources (Finviz/Yahoo), fetch their StockTwits sentiment
       and boost the ones with heavy bullish bias.

    Scoring:
      trending rank  → base score
      watchers       → additive
      bullish_count  → multiplier
      bearish_count  → penalty
    """
    results = {}
    headers = {
        "User-Agent": random.choice(_UA_POOL),
        "Accept": "application/json",
    }

    # ── Endpoint 1: trending symbols ──
    try:
        r = requests.get(
            "https://api.stocktwits.com/api/2/trending/symbols.json?limit=30",
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            symbols = data.get("symbols", [])
            for i, obj in enumerate(symbols[:top_n]):
                ticker  = obj.get("symbol", "").upper()
                watch   = obj.get("watchlist_count", 0)
                if 1 <= len(ticker) <= 5 and ticker.isalpha() and ticker not in TICKER_BLACKLIST:
                    score = (top_n - i) * 12 + (watch // 1000)
                    results[ticker] = results.get(ticker, 0) + score
            if symbols:
                console.print(f"  [green]✔ StockTwits trending: {len(symbols)} symbols[/green]")
            else:
                console.print("  [yellow]⚠ StockTwits trending: empty (market closed or rate-limited)[/yellow]")
        time.sleep(0.5)
    except Exception as e:
        console.print(f"  [yellow]⚠ StockTwits trending error: {str(e)[:60]}[/yellow]")

    # ── Endpoint 2: suggested (editor picks) ──
    try:
        r = requests.get(
            "https://api.stocktwits.com/api/2/streams/suggested.json?limit=30",
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            messages = data.get("messages", [])
            seen = set()
            count = 0
            for msg in messages:
                sym = msg.get("symbols", [])
                sentiment = msg.get("entities", {}).get("sentiment", {})
                bull = sentiment.get("basic") == "Bullish" if sentiment else False
                for s in sym:
                    ticker = s.get("symbol", "").upper()
                    if ticker and ticker not in TICKER_BLACKLIST and ticker not in seen:
                        seen.add(ticker)
                        score = 40 + (20 if bull else 0)
                        results[ticker] = results.get(ticker, 0) + score
                        count += 1
            if count:
                console.print(f"  [green]✔ StockTwits suggested stream: {count} symbols[/green]")
        time.sleep(0.5)
    except Exception as e:
        console.print(f"  [yellow]⚠ StockTwits suggested error: {str(e)[:60]}[/yellow]")

    # ── Endpoint 3: most active via undocumented path ──
    try:
        r = requests.get(
            "https://api.stocktwits.com/api/2/trending/symbols/equities.json?limit=30",
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            symbols = data.get("symbols", [])
            for i, obj in enumerate(symbols[:top_n]):
                ticker = obj.get("symbol", "").upper()
                watch  = obj.get("watchlist_count", 0)
                if 1 <= len(ticker) <= 5 and ticker.isalpha() and ticker not in TICKER_BLACKLIST:
                    score = (top_n - i) * 10 + (watch // 500)
                    results[ticker] = results.get(ticker, 0) + score
            if symbols:
                console.print(f"  [green]✔ StockTwits equities trending: {len(symbols)} symbols[/green]")
        time.sleep(0.5)
    except Exception:
        pass

    # ── Endpoint 4: per-symbol sentiment for top known movers ──
    # Pull from Yahoo Finance most active to seed the sentiment check
    _sentiment_seeds = _get_yahoo_most_active_quick(limit=20)
    if _sentiment_seeds:
        console.print(f"  [cyan]Checking StockTwits sentiment for {len(_sentiment_seeds)} known movers...[/cyan]")
        bull_count = 0
        for ticker in _sentiment_seeds:
            if ticker in TICKER_BLACKLIST:
                continue
            try:
                r = requests.get(
                    f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json?limit=30",
                    headers=headers, timeout=6
                )
                if r.status_code == 200:
                    data = r.json()
                    messages = data.get("messages", [])
                    bullish  = sum(1 for m in messages
                                   if m.get("entities", {}).get("sentiment", {}) and
                                   m["entities"]["sentiment"].get("basic") == "Bullish")
                    bearish  = sum(1 for m in messages
                                   if m.get("entities", {}).get("sentiment", {}) and
                                   m["entities"]["sentiment"].get("basic") == "Bearish")
                    total    = max(bullish + bearish, 1)
                    bull_ratio = bullish / total

                    if bull_ratio > 0.55 and bullish >= 3:
                        score = int(bull_ratio * 100) + bullish * 5
                        results[ticker] = results.get(ticker, 0) + score
                        bull_count += 1

                elif r.status_code == 429:
                    console.print("  [yellow]⚠ StockTwits rate-limited on per-symbol calls — stopping early[/yellow]")
                    break

                time.sleep(random.uniform(0.4, 0.8))
            except Exception:
                time.sleep(0.3)

        if bull_count:
            console.print(f"  [green]✔ StockTwits per-symbol: {bull_count} bullish signals added[/green]")

    if results:
        console.print(f"  [green]✔ StockTwits total: {len(results)} unique symbols[/green]")
    else:
        console.print("  [yellow]⚠ StockTwits: 0 symbols found across all endpoints[/yellow]")

    return results


def _get_yahoo_most_active_quick(limit: int = 20) -> list:
    """
    Lightweight Yahoo Finance most-active scrape used to seed StockTwits per-symbol checks.
    Returns a plain list of tickers.
    """
    from bs4 import BeautifulSoup
    try:
        r = requests.get(
            "https://finance.yahoo.com/most-active",
            headers={"User-Agent": random.choice(_UA_POOL)},
            timeout=8
        )
        soup = BeautifulSoup(r.text, "lxml")
        tickers = []
        seen = set()
        for a in soup.find_all("a", href=re.compile(r"/quote/")):
            m = re.search(r'/quote/([A-Z]{1,5})(?:/|\?|$)', a.get("href", ""))
            if m:
                t = m.group(1)
                if t not in seen and t not in TICKER_BLACKLIST and t.isalpha():
                    seen.add(t)
                    tickers.append(t)
                    if len(tickers) >= limit:
                        break
        return tickers
    except Exception:
        return []


# ──────────────────────────────────────────────
#  YAHOO FINANCE
# ──────────────────────────────────────────────

YAHOO_SCREENS = [
    "most_actives",
    "day_gainers",
    "day_losers",
    "growth_technology_stocks",
    "small_cap_gainers",
    "undervalued_growth_stocks",
    "aggressive_small_caps",
    "high_yield_bond",
]

def fetch_yahoo_screens(top_n: int = 100) -> dict:
    results = {}
    for screen in YAHOO_SCREENS:
        try:
            raw = yf.screen(screen)
            if isinstance(raw, dict):
                quotes = raw.get("quotes", raw.get("body", []))
                if not quotes:
                    raise ValueError("empty quotes list")
                df = pd.DataFrame(quotes)
            elif isinstance(raw, pd.DataFrame):
                df = raw
            else:
                raise ValueError(f"unexpected type: {type(raw)}")

            sym_col = next((c for c in df.columns if c.lower() in ("symbol", "ticker")), None)
            if sym_col is None:
                raise ValueError("no symbol column found")
            df = df.rename(columns={sym_col: "symbol"})

            if "exchange" in df.columns:
                df = df[df["exchange"].isin(["NMS", "NGM", "NCM", "NasdaqGS", "NasdaqCM", "NasdaqGM"])]

            tickers = df["symbol"].dropna().str.strip().str.upper().tolist()[:top_n]
            weight  = 50 if screen in ("most_actives", "day_gainers", "small_cap_gainers") else 30
            for rank, ticker in enumerate(tickers):
                score = weight + (top_n - rank)
                results[ticker] = results.get(ticker, 0) + score
            console.print(f"  [green]✔ Yahoo '{screen}': {len(tickers)} tickers[/green]")
        except Exception as e:
            console.print(f"  [yellow]⚠ Yahoo '{screen}' failed: {e}[/yellow]")
        time.sleep(0.2)
    return results


def fetch_yahoo_movers(top_n: int = 50) -> dict:
    from bs4 import BeautifulSoup
    results = {}
    try:
        r = requests.get(
            "https://finance.yahoo.com/most-active",
            headers={"User-Agent": random.choice(_UA_POOL)},
            timeout=10
        )
        soup = BeautifulSoup(r.text, "lxml")
        ticker_links = soup.find_all("a", href=re.compile(r"/quote/"))
        tickers_found = []
        for link in ticker_links:
            href = link.get("href", "")
            m = re.search(r'/quote/([A-Z]{1,5})(?:/|\?|$)', href)
            if m:
                ticker = m.group(1)
                if ticker not in TICKER_BLACKLIST and ticker.isalpha():
                    tickers_found.append(ticker)

        for rank, ticker in enumerate(list(dict.fromkeys(tickers_found))[:top_n]):
            score = top_n - rank + 20
            results[ticker] = score

        if results:
            console.print(f"  [green]✔ Yahoo Finance movers: {len(results)} tickers[/green]")
    except Exception:
        pass
    return results


# ──────────────────────────────────────────────
#  WEBULL + ROBINHOOD  (retail-popularity universe sources)
# ──────────────────────────────────────────────

def fetch_webull_actives(max_tickers: int = 400) -> dict:
    """
    Scrape Webull US 'most active' via a headless browser running in the
    background (no visible window). Webull renders a *virtualized* list — only
    ~50 rows live in the DOM at once and scrolling recycles them — so we
    intercept the JSON API responses (which carry "disSymbol") as we scroll;
    each scroll makes Webull fetch the next batch.

    Returns {ticker: score} scored by volume rank (earlier = higher score).
    Requires:  pip install playwright  &&  playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        console.print("  [yellow]⚠ Webull: Playwright not installed — skipping. "
                      "Run: pip install playwright && playwright install chromium[/yellow]")
        return {}

    captured = []  # symbols in API-arrival order (== volume rank)

    def handle_response(response):
        try:
            if "json" not in response.headers.get("content-type", ""):
                return
            body = response.text()
            if "disSymbol" not in body and '"symbol"' not in body:
                return
            for m in re.finditer(r'"(?:disSymbol|symbol|tickerSymbol)"\s*:\s*"([A-Z.]{1,6})"', body):
                sym = m.group(1).upper().split(".")[0]
                if 1 <= len(sym) <= 5 and sym.isalpha() and sym not in TICKER_BLACKLIST:
                    captured.append(sym)
        except Exception:
            pass

    results = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                                        args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(
                user_agent=random.choice(_UA_POOL),
                viewport={"width": 1920, "height": 1080},
            )
            page.on("response", handle_response)
            page.goto("https://www.webull.com/quote/us/actives/",
                      wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("table tr", timeout=30000)
            except Exception:
                pass
            time.sleep(3)

            last_len, stagnant = 0, 0
            for _ in range(40):
                page.mouse.wheel(0, 3000)
                time.sleep(0.7)
                uniq = len(dict.fromkeys(captured))
                if uniq == last_len:
                    stagnant += 1
                    if stagnant >= 5:
                        break
                else:
                    stagnant = 0
                last_len = uniq
                if uniq >= max_tickers:
                    break
            browser.close()

        ordered = list(dict.fromkeys(captured))[:max_tickers]
        for rank, sym in enumerate(ordered):
            results[sym] = len(ordered) - rank   # rank-1 = highest score

        if results:
            console.print(f"  [green]✔ Webull actives: {len(results)} tickers[/green]")
        else:
            console.print("  [yellow]⚠ Webull: 0 tickers captured[/yellow]")
    except Exception as e:
        console.print(f"  [yellow]⚠ Webull failed: {str(e)[:80]}[/yellow]")

    return results


def fetch_robinhood_popular(top_n: int = 100) -> dict:
    """
    Scrape stockscan.io's 'Robinhood popular' list (top 100 retail holdings).
    Tickers live in href links like /stocks/TSLA, so a plain request works.

    Returns {ticker: score} scored by list position (earlier = higher score).
    """
    from bs4 import BeautifulSoup
    results = {}
    try:
        r = requests.get("https://stockscan.io/100-popular-stocks-robinhood",
                         headers={"User-Agent": random.choice(_UA_POOL)}, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        ordered = []
        for a in soup.find_all("a", href=True):
            m = re.search(r'/stocks?/([A-Z]{1,5})(?:/|$|\?)', a["href"], re.I)
            if m:
                sym = m.group(1).upper()
                if sym.isalpha() and sym not in TICKER_BLACKLIST:
                    ordered.append(sym)

        ordered = list(dict.fromkeys(ordered))[:top_n]
        for rank, sym in enumerate(ordered):
            results[sym] = len(ordered) - rank

        if results:
            console.print(f"  [green]✔ Robinhood popular: {len(results)} tickers[/green]")
        else:
            console.print("  [yellow]⚠ Robinhood: 0 tickers found[/yellow]")
    except Exception as e:
        console.print(f"  [yellow]⚠ Robinhood failed: {str(e)[:80]}[/yellow]")

    return results


# ──────────────────────────────────────────────
#  REDDIT
# ──────────────────────────────────────────────

def fetch_reddit_mentions(hours_back: int = 24, post_limit: int = 500) -> dict:
    if REDDIT_CLIENT_ID == "YOUR_CLIENT_ID_HERE":
        console.print("  [yellow]⚠ Reddit: No API credentials set — skipping. See setup instructions.[/yellow]")
        return {}

    try:
        import praw
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        vader = SentimentIntensityAnalyzer()
    except ImportError:
        console.print("  [yellow]⚠ Reddit: praw/vaderSentiment not installed. Run: pip install praw vaderSentiment[/yellow]")
        return {}

    try:
        reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=REDDIT_USER_AGENT,
            check_for_async=False,
        )

        ticker_pattern = re.compile(r'\b([A-Z]{2,5})\b')
        scores = defaultdict(float)
        mention_counts = defaultdict(int)
        cutoff = datetime.utcnow() - timedelta(hours=hours_back)

        for sub_name in REDDIT_SUBS:
            try:
                sub   = reddit.subreddit(sub_name)
                posts = list(sub.hot(limit=post_limit // 2)) + list(sub.new(limit=post_limit // 2))
                for post in posts:
                    post_time = datetime.utcfromtimestamp(post.created_utc)
                    if post_time < cutoff:
                        continue
                    text = f"{post.title} {post.selftext}"
                    tickers_found = [t for t in ticker_pattern.findall(text.upper())
                                     if t not in TICKER_BLACKLIST]
                    sentiment     = vader.polarity_scores(text)
                    compound      = sentiment["compound"]
                    upvotes       = max(post.score, 1)
                    sent_mult     = 1 + max(compound, 0)
                    for ticker in set(tickers_found):
                        mention_counts[ticker] += 1
                        scores[ticker] += upvotes * sent_mult
                console.print(f"  [green]✔ Reddit r/{sub_name}: scraped {len(posts)} posts[/green]")
            except Exception as e:
                console.print(f"  [yellow]⚠ Reddit r/{sub_name}: {e}[/yellow]")
            time.sleep(1)

        import math
        result = {}
        for ticker, score in scores.items():
            mentions = mention_counts[ticker]
            if mentions < 2:
                continue
            result[ticker] = round(mentions * 10 + math.log1p(score) * 5, 1)

        console.print(f"  [green]✔ Reddit total: {len(result)} unique tickers with 2+ mentions[/green]")
        return result

    except Exception as e:
        console.print(f"  [red]✘ Reddit scraper error: {e}[/red]")
        return {}


# ──────────────────────────────────────────────
#  NASDAQ FTP FULL LIST  (fallback)
# ──────────────────────────────────────────────

def fetch_finnhub_earnings_calendar(days_ahead: int = 30, top_n: int = 150) -> dict:
    """
    Fetch upcoming earnings from Finnhub's free earnings calendar.
    Earnings announcements often precede significant moves.

    Free tier: 60 reqs/min.
    Docs: https://finnhub.io/docs/api/earnings-calendar
    """
    api_key = os.environ.get("FIN_KEY")
    if not api_key:
        console.print("  [yellow]⚠ Finnhub API key not set (FIN_KEY env var)[/yellow]")
        return {}

    result = {}
    today = datetime.now().date()
    from_date = today.isoformat()
    to_date = (today + timedelta(days=days_ahead)).isoformat()

    url = f"https://finnhub.io/api/v1/calendar/earnings?from={from_date}&to={to_date}&token={api_key}"

    try:
        resp = requests.get(url, timeout=10)

        if resp.status_code == 401:
            console.print(f"  [yellow]⚠ Finnhub earnings calendar: API key not authorized[/yellow]")
            return {}
        elif resp.status_code != 200:
            console.print(f"  [yellow]⚠ Finnhub earnings calendar returned {resp.status_code}[/yellow]")
            return {}

        data = resp.json()

        if "earningsCalendar" in data:
            for earning in data["earningsCalendar"][:top_n]:
                symbol = earning.get("symbol", "").strip()
                if symbol and len(symbol) <= 5 and symbol.isalpha() and symbol not in TICKER_BLACKLIST:
                    result[symbol] = result.get(symbol, 0) + 1.0

            console.print(f"  [green]✔ Finnhub Earnings Calendar: {len(result)} tickers[/green]")

    except Exception as e:
        console.print(f"  [yellow]⚠ Finnhub earnings calendar failed: {e}[/yellow]")

    return result


def _fetch_insider_single(ticker: str, api_key: str) -> tuple:
    """Fetch insider data for a single ticker."""
    try:
        url = f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={ticker}&token={api_key}"
        resp = requests.get(url, timeout=8)

        if resp.status_code == 200:
            data = resp.json()
            if "data" in data and isinstance(data["data"], list):
                buys = sum(1 for tx in data["data"][:20] if tx.get("change") and tx["change"] > 0)
                sells = sum(1 for tx in data["data"][:20] if tx.get("change") and tx["change"] < 0)

                if buys > sells:
                    return (ticker, float(buys) * 0.8)
    except Exception:
        pass
    return None


def fetch_finnhub_insider_buying(tickers: list, top_n: int = 100) -> dict:
    """
    Score tickers by insider transaction activity (buying > selling).
    Uses ThreadPoolExecutor for concurrent requests (5 workers, respects rate limits).
    Free tier: 60 reqs/min.
    Docs: https://finnhub.io/docs/api/insider-transactions
    """
    api_key = os.environ.get("FIN_KEY")
    if not api_key:
        return {}

    result = {}
    sample = tickers[:top_n]

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TaskProgressColumn(),
                  console=console) as prog:
        task = prog.add_task(f"[cyan]Finnhub insider activity ({len(sample)} tickers)...",
                             total=len(sample))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_fetch_insider_single, ticker, api_key): ticker for ticker in sample}

            for future in as_completed(futures):
                try:
                    res = future.result()
                    if res:
                        ticker, score = res
                        result[ticker] = score
                except Exception:
                    pass

                prog.advance(task)
                time.sleep(0.1)  # Rate limit friendly

    if result:
        console.print(f"  [green]✔ Finnhub Insider Buying: {len(result)} tickers with net buying[/green]")
    else:
        console.print(f"  [yellow]⚠ Finnhub Insider: no significant insider buying detected[/yellow]")

    return result


def _fetch_fundamentals_single(ticker: str, api_key: str) -> tuple:
    """Fetch fundamentals for a single ticker."""
    try:
        url = f"https://finnhub.io/api/v1/stock/metric?symbol={ticker}&metric=all&token={api_key}"
        resp = requests.get(url, timeout=8)

        if resp.status_code == 200:
            data = resp.json()
            if "metric" in data:
                m = data["metric"]
                score = 0.0

                pe = m.get("peNormalizedAnnual")
                if pe and 5 < pe < 25:
                    score += 1.0

                pb = m.get("pbAnnual")
                if pb and 0.5 < pb < 3.0:
                    score += 0.8

                roe = m.get("roeAnnual")
                if roe and roe > 0.15:
                    score += 1.2

                eps = m.get("epsAnnual")
                if eps and eps > 0:
                    score += 0.6

                if score > 1.5:
                    return (ticker, score)
    except Exception:
        pass
    return None


def fetch_finnhub_fundamentals_undervalued(tickers: list, top_n: int = 150) -> dict:
    """
    Score undervalued + quality stocks using Finnhub basic financials.
    Uses ThreadPoolExecutor for concurrent requests (5 workers, respects rate limits).
    Filters: Low PE, Low P/B, High ROE, Positive earnings.
    Free tier: 60 reqs/min.
    Docs: https://finnhub.io/docs/api/company-basic-financials
    """
    api_key = os.environ.get("FIN_KEY")
    if not api_key:
        return {}

    result = {}
    sample = tickers[:top_n]

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TaskProgressColumn(),
                  console=console) as prog:
        task = prog.add_task(f"[cyan]Finnhub fundamentals ({len(sample)} tickers)...",
                             total=len(sample))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_fetch_fundamentals_single, ticker, api_key): ticker for ticker in sample}

            for future in as_completed(futures):
                try:
                    res = future.result()
                    if res:
                        ticker, score = res
                        result[ticker] = score
                except Exception:
                    pass

                prog.advance(task)
                time.sleep(0.1)  # Rate limit friendly

    if result:
        console.print(f"  [green]✔ Finnhub Value Screen: {len(result)} undervalued + quality tickers[/green]")
    else:
        console.print(f"  [yellow]⚠ Finnhub Value: no undervalued stocks found[/yellow]")

    return result


def _fetch_quotes_single(ticker: str, api_key: str) -> tuple:
    """Fetch quote data for a single ticker."""
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={api_key}"
        resp = requests.get(url, timeout=8)

        if resp.status_code == 200:
            data = resp.json()
            score = 0.0

            change_pct = data.get("d", 0)
            if abs(change_pct) > 2:
                score += 0.5 * min(abs(change_pct) / 10, 2)

            current_vol = data.get("v", 0)
            prev_close_vol = data.get("prevV", 1)
            if prev_close_vol > 0:
                rel_vol = current_vol / prev_close_vol
                if rel_vol > 1.2:
                    score += 0.6 * min(rel_vol, 2)

            bid = data.get("bid", 0)
            ask = data.get("ask", 0)
            if bid > 0 and ask > bid:
                spread_pct = ((ask - bid) / bid) * 100
                if spread_pct < 1.0:
                    score += 0.4

            if score > 0.5:
                return (ticker, score)
    except Exception:
        pass
    return None


def fetch_finnhub_quotes_momentum(tickers: list, top_n: int = 150) -> dict:
    """
    Score stocks by momentum signals from quote data.
    Uses ThreadPoolExecutor for concurrent requests (5 workers, respects rate limits).
    Filters: % change, relative volume, bid-ask spread.
    Free tier: 60 reqs/min.
    Docs: https://finnhub.io/docs/api/quote
    """
    api_key = os.environ.get("FIN_KEY")
    if not api_key:
        return {}

    result = {}
    sample = tickers[:top_n]

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TaskProgressColumn(),
                  console=console) as prog:
        task = prog.add_task(f"[cyan]Finnhub quotes & momentum ({len(sample)} tickers)...",
                             total=len(sample))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_fetch_quotes_single, ticker, api_key): ticker for ticker in sample}

            for future in as_completed(futures):
                try:
                    res = future.result()
                    if res:
                        ticker, score = res
                        result[ticker] = score
                except Exception:
                    pass

                prog.advance(task)
                time.sleep(0.1)  # Rate limit friendly

    if result:
        console.print(f"  [green]✔ Finnhub Momentum: {len(result)} tickers with positive momentum[/green]")
    else:
        console.print(f"  [yellow]⚠ Finnhub Momentum: no momentum signals detected[/yellow]")

    return result


def fetch_nasdaq_ftp() -> dict:
    url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        lines  = r.text.strip().split("\n")
        result = {}
        for line in lines[1:]:
            parts = line.split("|")
            if len(parts) < 7:
                continue
            symbol = parts[0].strip()
            etf    = parts[6].strip()
            if etf == "Y":
                continue
            if any(c in symbol for c in ["W", "U", "R", "$", "^", "~", "+"]):
                continue
            if len(symbol) > 5 or not symbol.isalpha():
                continue
            result[symbol] = 1
        console.print(f"  [green]✔ NASDAQ FTP: {len(result)} tickers[/green]")
        return result
    except Exception as e:
        console.print(f"  [yellow]⚠ NASDAQ FTP failed: {e}[/yellow]")
        return {}


# ──────────────────────────────────────────────
#  OPENINSIDER — cluster insider buys (free, no account)
# ──────────────────────────────────────────────

def fetch_openinsider_clusters(top_n: int = 150) -> dict:
    """
    Scrape openinsider.com/latest-cluster-buys.

    A 'cluster buy' = multiple insiders at the same company buying within a
    short window. Historically the single strongest insider signal: when 3+
    insiders buy together, the next 6m return outperforms by a wide margin.

    Score weighted by:
      - $ value of cluster (log-scaled)
      - recency (last 7 days gets full weight, older decays)
      - number of insiders (more = stronger signal)
    """
    from bs4 import BeautifulSoup
    results = {}
    urls = [
        ("http://openinsider.com/latest-cluster-buys",      "Cluster Buys",    2.0),
        ("http://openinsider.com/top-officer-purchases-of-the-month", "Officer Buys", 1.2),
    ]

    for url, label, weight in urls:
        try:
            resp = requests.get(url, headers={"User-Agent": random.choice(_UA_POOL)}, timeout=12)
            if resp.status_code != 200:
                console.print(f"  [yellow]⚠ OpenInsider {label}: HTTP {resp.status_code}[/yellow]")
                continue

            soup  = BeautifulSoup(resp.text, "lxml")
            table = soup.find("table", class_="tinytable")
            if table is None:
                console.print(f"  [yellow]⚠ OpenInsider {label}: table missing[/yellow]")
                continue

            rows = table.find_all("tr")[1:top_n+1]
            count = 0
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 13:
                    continue
                try:
                    ticker = cells[3].get_text(strip=True).upper()
                    if not (1 <= len(ticker) <= 5 and ticker.isalpha()):
                        continue
                    if ticker in TICKER_BLACKLIST:
                        continue
                    # Cluster value column (col 12 = "Value")
                    val_txt = cells[12].get_text(strip=True).replace("$", "").replace(",", "").replace("+", "")
                    try:
                        val = float(val_txt)
                    except ValueError:
                        val = 0.0
                    # log-scale: $10k=1, $100k=2, $1M=3, $10M=4
                    val_score = max(0.5, np.log10(max(val, 10_000)) - 3)
                    results[ticker] = results.get(ticker, 0) + val_score * weight
                    count += 1
                except Exception:
                    continue

            if count:
                console.print(f"  [green]✔ OpenInsider {label}: {count} rows → {len(set(results))} tickers[/green]")
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            console.print(f"  [yellow]⚠ OpenInsider {label} failed: {e}[/yellow]")

    return results


# ──────────────────────────────────────────────
#  FINVIZ QUALITY  (fundamentals + near-breakout filter)
# ──────────────────────────────────────────────

def fetch_finviz_quality(top_n: int = 200, max_pages: int = 3) -> dict:
    """
    Pull a 'quality + setup' universe from Finviz's free screener.

    Filter codes (see finviz.com/screener.ashx?ft=4):
      fa_epsyoy_o15        EPS growth YoY > 15%
      fa_grossmargin_o25   Gross margin > 25%
      fa_roe_o10           ROE > 10%
      sh_price_o5          Price > $5    (no penny trash)
      sh_avgvol_o500       Avg volume > 500k (liquidity floor)
      sh_curvol_o1000      Current vol > 1M (today's interest)
      ta_highlow52w_b10h   Within 10% below 52-week high

    Three blended screens so a single FA miss doesn't kill a candidate.
    """
    results = {}
    screens = [
        # (filter_string, label, weight)
        ("fa_epsyoy_o15,fa_grossmargin_o25,sh_price_o5,sh_avgvol_o500,ta_highlow52w_b10h&o=-change",
            "Growth+Margin near 52wH", 1.6),
        ("fa_roe_o15,fa_grossmargin_o25,sh_price_o5,sh_avgvol_o500,ta_highlow52w_b10h&o=-change",
            "ROE+Margin near 52wH",    1.4),
        ("sh_insiderown_o5,fa_epsyoy_o15,sh_price_o5,sh_avgvol_o500&o=-change",
            "Insider-own + Growth",    1.5),
    ]

    session = requests.Session()
    try:
        session.get("https://finviz.com/", headers=_finviz_headers(), timeout=8)
        time.sleep(random.uniform(0.6, 1.2))
    except Exception:
        pass

    base_url = "https://finviz.com/screener.ashx?v=111&f={f}&r={r}"

    for f_str, label, weight in screens:
        page_tickers = []
        for page in range(max_pages):  # 20 tickers per page per screen
            row_start = page * 20 + 1
            url = base_url.format(f=f_str, r=row_start)
            html = None
            for attempt in range(3):
                try:
                    resp = session.get(url, headers=_finviz_headers(), timeout=12)
                    if resp.status_code == 200:
                        html = resp.text
                        break
                    elif resp.status_code in (403, 429):
                        time.sleep(2 ** attempt + random.uniform(1, 2))
                    else:
                        break
                except Exception:
                    break
            if html is None:
                break

            page_result = _finviz_parse_tickers(html)
            if not page_result:
                break
            page_tickers.extend(page_result)
            time.sleep(random.uniform(1.0, 2.0))
            if len(page_tickers) >= top_n // len(screens):
                break

        seen = set()
        unique = [t for t in page_tickers if not (t in seen or seen.add(t))]
        for rank, ticker in enumerate(unique):
            results[ticker] = results.get(ticker, 0) + (len(unique) - rank) * weight
        if unique:
            console.print(f"  [green]✔ Finviz Quality [{label}]: {len(unique)} tickers[/green]")
        else:
            console.print(f"  [yellow]⚠ Finviz Quality [{label}]: 0 tickers[/yellow]")
        time.sleep(random.uniform(1.5, 2.5))

    return results


def fetch_finviz_momentum(top_n: int = 200, max_pages: int = 5) -> dict:
    """
    Pull a low-priced, high-volume momentum/breakout screen from Finviz.

    Exact filter URL the user provided:
      cap_midunder       Mid cap and under
      exch_nasd          NASDAQ-listed
      sh_avgvol_o400     Avg volume > 400k
      sh_curvol_o750     Current volume > 750k (today's interest)
      sh_float_u100      Float < 100M (room to run)
      sh_price_u15       Price < $15
      sh_relvol_o1.5     Relative volume > 1.5x
      ta_perf_dup        Performance: day up
      ta_rsi_nos50       RSI(14) not above 50 (not overbought)
      ta_sma20_pa        Price above SMA20
      ta_volatility_wo3  Weekly volatility > 3%

    Designed for swing trading targeting 10-15% weekly moves.
    """
    results = {}
    f_str = ("cap_midunder,exch_nasd,sh_avgvol_o400,sh_curvol_o750,"
             "sh_float_u100,sh_price_u15,sh_relvol_o1.5,ta_perf_dup,"
             "ta_rsi_nos50,ta_sma20_pa,ta_volatility_wo3")

    session = requests.Session()
    try:
        session.get("https://finviz.com/", headers=_finviz_headers(), timeout=8)
        time.sleep(random.uniform(0.6, 1.2))
    except Exception:
        pass

    base_url = "https://finviz.com/screener.ashx?v=111&f={f}&r={r}"
    all_tickers = []

    for page in range(max_pages):  # 20 tickers per page
        row_start = page * 20 + 1
        url = base_url.format(f=f_str, r=row_start)
        html = None
        for attempt in range(3):
            try:
                resp = session.get(url, headers=_finviz_headers(), timeout=12)
                if resp.status_code == 200:
                    html = resp.text
                    break
                elif resp.status_code in (403, 429):
                    time.sleep(2 ** attempt + random.uniform(1, 2))
                else:
                    break
            except Exception:
                break
        if html is None:
            break

        page_result = _finviz_parse_tickers(html)
        if not page_result:
            break
        all_tickers.extend(page_result)
        time.sleep(random.uniform(1.0, 2.0))
        if len(all_tickers) >= top_n:
            break

    seen = set()
    unique = [t for t in all_tickers if not (t in seen or seen.add(t))]
    for rank, ticker in enumerate(unique):
        results[ticker] = (len(unique) - rank)

    if unique:
        console.print(f"  [green]✔ Finviz Momentum [low-float breakout]: {len(unique)} tickers[/green]")
    else:
        console.print(f"  [yellow]⚠ Finviz Momentum [low-float breakout]: 0 tickers[/yellow]")

    return results


# ──────────────────────────────────────────────
#  UNIVERSE BUILDER
# ──────────────────────────────────────────────

def build_universe(sources: list, max_tickers: int = 500, show: bool = False,
                   large: bool = False, sleeper_quota: int = 150) -> tuple:
    console.print(Panel.fit("[bold]🌐 BUILDING POPULARITY UNIVERSE[/bold]", border_style="blue"))
    combined = defaultdict(float)
    finviz_hit = False

    # In large mode pull deeper from the *quality-filtered* Finviz screens
    # (more pages of the same screened results) rather than dumping the raw
    # NASDAQ FTP list — keeps signal quality high while widening the net.
    if large:
        active_topn, active_pages   = 600, 12
        quality_topn, quality_pages = 450, 8
        moment_topn, moment_pages   = 400, 10
        console.print(f"[bold magenta]🔭 LARGE UNIVERSE mode — deepening quality screens "
                      f"(cap {max_tickers:,})[/bold magenta]")
    else:
        active_topn, active_pages   = 300, 5
        quality_topn, quality_pages = 200, 3
        moment_topn, moment_pages   = 200, 5

    # ── Build the list of independent source fetches, then run them all
    # concurrently. Each is network/IO-bound, so threads give a big speedup
    # over the old sequential calls. Each task returns its own {ticker: score}
    # dict; we apply the per-source weight when merging after they complete.
    #   tasks[name] = (callable, weight, log_label)
    tasks = {}

    if "finviz" in sources:
        tasks["finviz_active"]   = (lambda: fetch_finviz_active(top_n=active_topn, max_pages=active_pages),
                                    1.2, "Finviz active/volume/gainer feeds")
        tasks["finviz_trending"] = (fetch_finviz_trending, 1.0, "Finviz news trending")
    if "yahoo" in sources:
        tasks["yahoo_screens"]   = (fetch_yahoo_screens, 1.0, "Yahoo screener feeds")
        tasks["yahoo_movers"]    = (fetch_yahoo_movers, 1.5, "Yahoo real-time movers")
    # StockTwits always contributes sentiment
    tasks["stocktwits"]          = (fetch_stocktwits_sentiment, 1.3, "StockTwits sentiment")
    if "webull" in sources:
        tasks["webull"]          = (fetch_webull_actives, 1.4, "Webull actives (headless)")
    if "robinhood" in sources:
        tasks["robinhood"]       = (fetch_robinhood_popular, 1.4, "Robinhood popular (stockscan)")
    if "movers" in sources:
        tasks["polygon_movers"]  = (fetch_polygon_movers, 1.5, "Polygon gainers/losers")
    if "reddit" in sources:
        tasks["reddit"]          = (fetch_reddit_mentions, 2.0, "Reddit WSB + stocks mentions")
    if "insider" in sources:
        tasks["insider"]         = (fetch_openinsider_clusters, 2.2, "OpenInsider cluster/officer buys")
    if "quality" in sources:
        tasks["quality"]         = (lambda: fetch_finviz_quality(top_n=quality_topn, max_pages=quality_pages),
                                    1.6, "Finviz quality screen")
    if "momentum" in sources:
        tasks["momentum"]        = (lambda: fetch_finviz_momentum(top_n=moment_topn, max_pages=moment_pages),
                                    2.0, "Finviz momentum screen")
    if "finnhub" in sources:
        tasks["finnhub_earn"]    = (fetch_finnhub_earnings_calendar, 1.1, "Finnhub earnings calendar")

    console.print(f"[cyan]⚡ Fetching {len(tasks)} universe sources in parallel...[/cyan]")
    raw_results = {}
    with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as ex:
        futures = {ex.submit(fn): name for name, (fn, _w, _lbl) in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            label = tasks[name][2]
            try:
                raw_results[name] = fut.result() or {}
            except Exception as e:
                raw_results[name] = {}
                console.print(f"  [yellow]⚠ {label} failed: {str(e)[:70]}[/yellow]")

    # ── Merge results with per-source weights ──
    for name, (_fn, weight, _lbl) in tasks.items():
        res = raw_results.get(name, {})
        if name == "finviz_active":
            if res:
                finviz_hit = True
                for t, s in res.items():
                    combined[t] += s * weight
            else:
                console.print("  [yellow]⚠ Finviz returned 0 tickers — adding NASDAQ FTP fallback[/yellow]")
                for t, s in fetch_nasdaq_ftp().items():
                    combined[t] += s
        else:
            for t, s in res.items():
                combined[t] += s * weight

    if "nasdaq" in sources or (not combined and "finnhub" not in sources):
        console.print("[cyan]🗄  NASDAQ FTP full list (coverage fallback)...[/cyan]")
        for t, s in fetch_nasdaq_ftp().items():
            if t not in combined:
                combined[t] += s

    # ── Validity filter — drop anything not a real NASDAQ/NYSE ticker BEFORE
    # ranking/capping, so the Finnhub enrichment calls below (and every API call
    # after this point) never get spent on garbage/delisted/typo'd symbols. ──
    if VALID_TICKERS:
        before_n = len(combined)
        combined = defaultdict(float, {t: s for t, s in combined.items() if t in VALID_TICKERS})
        dropped = before_n - len(combined)
        if dropped:
            console.print(f"[dim]🧹 Filtered {dropped} tickers not in stockTickers.txt "
                          f"(not real NASDAQ/NYSE symbols)[/dim]")

    ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)
    ranked = [(t, s) for t, s in ranked if 1 <= len(t) <= 5 and t.isalpha()]
    top    = ranked[:max_tickers]

    console.print(f"\n[bold]📊 Base Universe: {len(top):,} tickers[/bold]\n")

    # Enrich with Finnhub fundamentals + insider + momentum
    universe_tickers = [t for t, _ in top]

    console.print("[cyan]📈 Enriching universe with Finnhub data...[/cyan]")

    for t, s in fetch_finnhub_insider_buying(universe_tickers).items():
        combined[t] += s * 1.4

    for t, s in fetch_finnhub_fundamentals_undervalued(universe_tickers).items():
        combined[t] += s * 1.2

    for t, s in fetch_finnhub_quotes_momentum(universe_tickers).items():
        combined[t] += s * 0.9

    # Re-rank after Finnhub enrichment
    combined_final = sorted(combined.items(), key=lambda x: x[1], reverse=True)
    top = combined_final[:max_tickers]
    top_set = {t for t, _ in top}

    # ── SLEEPER PICKS ──────────────────────────────────────────────────────
    # The top-N-by-popularity cut systematically drops quietly-strong names that
    # aren't trending. So we reserve a quota for tickers that scored well on the
    # *quality* sources (insider cluster buys, Finviz quality + momentum screens)
    # but didn't make the popularity cut. This widens small/mid-cap variety and
    # surfaces sleepers — concretely and reproducibly, not by luck.
    QUALITY_TASKS = ("insider", "quality", "momentum")
    quality_pool  = defaultdict(float)
    for qname in QUALITY_TASKS:
        for t, s in raw_results.get(qname, {}).items():
            if (1 <= len(t) <= 5 and t.isalpha() and t not in TICKER_BLACKLIST
                    and (not VALID_TICKERS or t in VALID_TICKERS)):
                quality_pool[t] += s

    sleepers = []
    if sleeper_quota > 0 and quality_pool:
        sleepers = sorted(((t, sc) for t, sc in quality_pool.items() if t not in top_set),
                          key=lambda x: x[1], reverse=True)[:sleeper_quota]
        for t, sc in sleepers:
            combined[t] = combined.get(t, 0.0) + sc   # give sleepers a real pop score too
        top = top + [(t, combined[t]) for t, _ in sleepers]
        console.print(f"[bold green]💤 + {len(sleepers)} quality sleeper picks "
                      f"(strong on quality signals, below the popularity cut)[/bold green]")

    console.print(f"\n[bold green]Final Universe: {len(top):,} tickers "
                  f"({len(top)-len(sleepers)} popular + {len(sleepers)} sleepers, "
                  f"ranked by popularity + fundamentals)[/bold green]\n")

    if show:
        console.print("[bold]Top 50 Most Popular Tickers:[/bold]")
        max_score = max(s for _, s in top[:50]) if top else 1
        for i, (ticker, score) in enumerate(top[:50], 1):
            bar = "█" * min(int(score / max_score * 30), 30)
            console.print(f"  {i:3}. [cyan]{ticker:6}[/cyan] {bar} {score:.0f}")
        console.print()

    final_tickers = [t for t, _ in top]
    return final_tickers, {t: combined.get(t, 0.0) for t in final_tickers}

