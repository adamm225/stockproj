"""
╔══════════════════════════════════════════════════════════════════════╗
║         NASDAQ POPULARITY-DRIVEN SCREENER  v4.0                     ║
║                                                                      ║
║  Universe built from REAL retail + comprehensive coverage:           ║
║                                                                      ║
║  1. Finviz Most Active / Top Volume export  (no login, free)         ║
║  2. Yahoo Finance screener feeds  (most_actives, day_gainers,        ║
║         small_cap_gainers, growth_technology_stocks, etc.)           ║
║  3. Reddit WSB + r/stocks + r/investing mention scraper              ║
║         (free PRAW API — needs a Reddit app, setup below)            ║
║  4. Finviz Trending / News Heat  (scrape trending page)              ║
║  5. Finnhub earnings calendar  (stocks announcing next 30 days)       ║
║  6. NASDAQ FTP full list  (fallback for broad coverage)              ║
║  7. StockTwits trending + most active + bullish sentiment            ║
║                                                                      ║
║  All sources are deduplicated + scored by POPULARITY RANK,           ║
║  then ENRICHED with Finnhub fundamentals/insider/momentum,           ║
║  then fed into the 7 strategy screeners.                             ║
║                                                                      ║
║  Price Data: Polygon (Stocks Starter) → yfinance fallback           ║
║  Fundamental Data: yfinance (.info) + Polygon short interest         ║
║  Install:                                                            ║
║    pip install yfinance pandas numpy requests rich pytz              ║
║               beautifulsoup4 lxml praw vaderSentiment                ║
╚══════════════════════════════════════════════════════════════════════╝

POLYGON SETUP (primary price + reference data):
  1. Sign up at https://polygon.io and subscribe to a Stocks plan
  2. Copy your API key from the dashboard
  3. Set it in your .env (only the key is needed — name is not used):
       POLYGON_KEY="your_key_here"
  Provides: daily OHLCV bars, ticker details, short interest, gainers/losers.
  If POLYGON_KEY is missing or a ticker isn't covered, yfinance is used.

REDDIT SETUP (one-time, free):
  1. Go to https://www.reddit.com/prefs/apps
  2. Click "Create App" → choose "script"
  3. Name it anything, redirect URI = http://localhost:8080
  4. Copy your client_id (under app name) and client_secret
  5. Set them in the REDDIT CONFIG section below OR via env vars:
       export REDDIT_CLIENT_ID=xxxx
       export REDDIT_CLIENT_SECRET=xxxx

Usage:
  python nasdaq_screener_v4.py                    # all 7 strategies (incl. Finnhub)
  python nasdaq_screener_v4.py --strategy 1       # specific strategy (1-7)
  python nasdaq_screener_v4.py --overnight        # strategies 4,5,6 (night prep)
  python nasdaq_screener_v4.py --morning          # strategies 1,2,3 (premarket)
  python nasdaq_screener_v4.py --ten-am           # strategy 7 (10 AM ORB)
  python nasdaq_screener_v4.py --sources finviz yahoo reddit finnhub
  python nasdaq_screener_v4.py --max 300          # cap tickers to screen
  python nasdaq_screener_v4.py --export           # save CSV output
  python nasdaq_screener_v4.py --show-universe    # print full ranked universe
"""

import argparse
import io
import os
import re
import sys
import time
import random
import warnings
from collections import defaultdict
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf

load_dotenv()

# ──────────────────────────────────────────────
#  POLYGON CONFIG  (primary price + reference data)
#  Stocks Starter plan — only the API key is needed.
# ──────────────────────────────────────────────
POLYGON_KEY  = os.environ.get("POLYGON_KEY", "")
POLYGON_BASE = "https://api.polygon.io"

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

warnings.filterwarnings("ignore")
console = Console(width=None)

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
                   large: bool = False) -> tuple:
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

    if "finviz" in sources:
        console.print("[cyan]📊 Finviz active/volume/gainer feeds...[/cyan]")
        fv = fetch_finviz_active(top_n=active_topn, max_pages=active_pages)
        if fv:
            finviz_hit = True
            for t, s in fv.items():
                combined[t] += s * 1.2
        else:
            console.print("  [yellow]⚠ Finviz returned 0 tickers — adding NASDAQ FTP fallback[/yellow]")
            for t, s in fetch_nasdaq_ftp().items():
                combined[t] += s

        console.print("[cyan]📰 Finviz news trending...[/cyan]")
        for t, s in fetch_finviz_trending().items():
            combined[t] += s

    if "yahoo" in sources:
        console.print("[cyan]📈 Yahoo Finance screener feeds...[/cyan]")
        for t, s in fetch_yahoo_screens().items():
            combined[t] += s

        console.print("[cyan]🔥 Yahoo Finance real-time movers...[/cyan]")
        for t, s in fetch_yahoo_movers().items():
            combined[t] += s * 1.5

    console.print("[cyan]💬 StockTwits sentiment (trending + suggested + per-symbol)...[/cyan]")
    for t, s in fetch_stocktwits_sentiment().items():
        combined[t] += s * 1.3

    if "movers" in sources:
        console.print("[cyan]🔥 Polygon gainers/losers snapshot...[/cyan]")
        for t, s in fetch_polygon_movers().items():
            combined[t] += s * 1.5

    if "reddit" in sources:
        console.print("[cyan]🤖 Reddit WSB + stocks mention scraper...[/cyan]")
        for t, s in fetch_reddit_mentions().items():
            combined[t] += s * 2.0

    if "insider" in sources:
        console.print("[cyan]🕵  OpenInsider cluster buys + officer purchases...[/cyan]")
        for t, s in fetch_openinsider_clusters().items():
            combined[t] += s * 2.2  # strongest single fundamental signal

    if "quality" in sources:
        console.print("[cyan]💎 Finviz quality screen (growth + margin + near 52wH)...[/cyan]")
        for t, s in fetch_finviz_quality(top_n=quality_topn, max_pages=quality_pages).items():
            combined[t] += s * 1.6

    if "momentum" in sources:
        console.print("[cyan]🚀 Finviz momentum (low-float NASDAQ, relvol>1.5, vol>3%w)...[/cyan]")
        for t, s in fetch_finviz_momentum(top_n=moment_topn, max_pages=moment_pages).items():
            combined[t] += s * 2.0  # high-conviction setup screen for 10-15% swings

    if "finnhub" in sources:
        console.print("[cyan]📅 Finnhub earnings calendar (next 30 days)...[/cyan]")
        for t, s in fetch_finnhub_earnings_calendar().items():
            combined[t] += s * 1.1

    if "nasdaq" in sources or (not combined and "finnhub" not in sources):
        console.print("[cyan]🗄  NASDAQ FTP full list (coverage fallback)...[/cyan]")
        for t, s in fetch_nasdaq_ftp().items():
            if t not in combined:
                combined[t] += s

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

    console.print(f"\n[bold green]Final Universe: {len(top):,} tickers (ranked by popularity + fundamentals)[/bold green]\n")

    if show:
        console.print("[bold]Top 50 Most Popular Tickers:[/bold]")
        max_score = max(s for _, s in top[:50]) if top else 1
        for i, (ticker, score) in enumerate(top[:50], 1):
            bar = "█" * min(int(score / max_score * 30), 30)
            console.print(f"  {i:3}. [cyan]{ticker:6}[/cyan] {bar} {score:.0f}")
        console.print()

    return [t for t, _ in top], dict(top)


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


# ──────────────────────────────────────────────
#  TECHNICAL HELPERS
# ──────────────────────────────────────────────

def rvol(v):
    avg = v.iloc[-21:-1].mean()
    return round(float(v.iloc[-1]) / avg, 2) if avg > 0 else 0.0

def rsi(c, p=14):
    d = c.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return float((100 - 100 / (1 + g / (l + 1e-9))).iloc[-1])

def rsi_series(c, p=14):
    d = c.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (l + 1e-9))

def rsi_inflection(rsi_ser, lookback=10):
    """Detect that RSI bottomed N days ago and is now climbing.
    Returns (days_since_bottom, rsi_at_bottom, rsi_now_minus_bottom)."""
    if rsi_ser is None or len(rsi_ser) < lookback + 1:
        return None, None, None
    window = rsi_ser.iloc[-lookback:].dropna()
    if window.empty:
        return None, None, None
    min_pos = int(window.values.argmin())
    days_ago = len(window) - 1 - min_pos
    rsi_min = float(window.iloc[min_pos])
    rsi_now = float(window.iloc[-1])
    return days_ago, rsi_min, rsi_now - rsi_min

def bullish_rsi_divergence(c, rsi_ser, lookback=15):
    """Price made a lower low, RSI made a higher low — classic exhaustion signal."""
    if len(c) < lookback + 1 or rsi_ser is None or len(rsi_ser) < lookback + 1:
        return False
    p = c.iloc[-lookback:].values.astype(float)
    r = rsi_ser.iloc[-lookback:].values.astype(float)
    mid = lookback // 2
    p1 = int(np.argmin(p[:mid]))
    p2 = mid + int(np.argmin(p[mid:]))
    if p2 <= p1 or not (p[p2] < p[p1]):
        return False
    if np.isnan(r[p1]) or np.isnan(r[p2]):
        return False
    return r[p2] > r[p1] + 2  # need a meaningful gap, not noise

def macd_histogram_turning_up(c):
    """MACD histogram rising for the last 3 bars (still negative is OK)."""
    if len(c) < 35:
        return False
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9, adjust=False).mean()
    h     = (macd - sig).values
    if len(h) < 4 or np.isnan(h[-3]):
        return False
    return bool(h[-1] > h[-2] > h[-3])

def ema_reclaim(c, span=10, min_below_days=5):
    """Stock was below EMA(span) for most of the prior `min_below_days` and
    just closed back above it — trend shift from down to up."""
    if len(c) < span * 3:
        return False
    e = c.ewm(span=span, adjust=False).mean()
    if float(c.iloc[-1]) <= float(e.iloc[-1]):
        return False
    prior_c = c.iloc[-(min_below_days+1):-1]
    prior_e = e.iloc[-(min_below_days+1):-1]
    below = (prior_c < prior_e).sum()
    return int(below) >= min_below_days - 1

def higher_low_structure(l_ser, lookback=15):
    """Most recent swing low > prior swing low — base is firming."""
    if len(l_ser) < lookback:
        return False
    ll = l_ser.iloc[-lookback:].values.astype(float)
    mid = lookback // 2
    return float(np.min(ll[mid:])) > float(np.min(ll[:mid]))

def up_vol_expansion(c, v, lookback=10):
    """Avg volume on green days > 1.3× avg volume on red days within window."""
    if len(c) < lookback + 1:
        return False
    d = c.diff().iloc[-lookback:]
    vols = v.iloc[-lookback:]
    up = vols[d > 0]
    dn = vols[d < 0]
    if len(up) == 0 or len(dn) == 0:
        return False
    return float(up.mean()) > float(dn.mean()) * 1.3

def closes_upper_half(h, l, c, days=2):
    """Last N sessions closed in the upper half of their daily range — buyers won."""
    if len(c) < days:
        return False
    for i in range(-days, 0):
        rng = float(h.iloc[i]) - float(l.iloc[i])
        if rng <= 0:
            return False
        if (float(c.iloc[i]) - float(l.iloc[i])) / rng < 0.5:
            return False
    return True

def mfi(h, l, c, v, p=14):
    """Money Flow Index — RSI weighted by dollar volume."""
    if len(c) < p + 1:
        return 50.0
    tp = (h + l + c) / 3
    mf = tp * v
    pos_mf = mf.copy()
    pos_mf[tp.diff() <= 0] = 0
    pos_sum = pos_mf.rolling(p).sum()
    total_sum = mf.rolling(p).sum()
    raw_mfi = pos_sum / total_sum
    return float((100 * raw_mfi).iloc[-1]) if total_sum.iloc[-1] > 0 else 50.0

def catalyst_quality_score(info, c, h, l, o, v, bench_c, rvol_val, price):
    """Score the quality of a catalyst move: float, short interest, RS, MFI, gap."""
    scores = {}

    # float-aware RVOL: small float amplifies the move
    float_sh = info.get('floatShares')
    if float_sh and float_sh > 0:
        if float_sh < 50e6 and rvol_val >= 2.0:
            scores['flt_rvol'] = 1.0
        elif float_sh < 100e6 and rvol_val >= 1.5:
            scores['flt_rvol'] = 0.7
        elif rvol_val >= 3.0:
            scores['flt_rvol'] = 0.5
        else:
            scores['flt_rvol'] = 0.0
    else:
        scores['flt_rvol'] = 0.0

    # short interest: squeeze potential
    short_pct = info.get('shortPercentOfFloat')
    if short_pct:
        if short_pct >= 0.30:
            scores['short'] = 1.0
        elif short_pct >= 0.15:
            scores['short'] = 0.7
        elif short_pct >= 0.10:
            scores['short'] = 0.4
        else:
            scores['short'] = 0.0
    else:
        scores['short'] = 0.0

    # RS vs benchmark: moving up while market is flat = real money
    rs = rs_vs_benchmark(c, bench_c, periods=(5, 10)) if bench_c is not None else None
    if rs is not None:
        if rs > 5:
            scores['rs'] = 1.0
        elif rs > 2:
            scores['rs'] = 0.7
        elif rs > 0:
            scores['rs'] = 0.4
        else:
            scores['rs'] = 0.0
    else:
        scores['rs'] = 0.0

    # MFI: volume-weighted momentum, less noisy than RSI
    mfi_val = mfi(h, l, c, v, p=14)
    if mfi_val > 65:
        scores['mfi'] = 1.0
    elif mfi_val > 55:
        scores['mfi'] = 0.7
    elif mfi_val > 45:
        scores['mfi'] = 0.4
    else:
        scores['mfi'] = 0.0

    # gap quality: gap up and held above open
    if len(o) > 0 and len(c) > 1:
        gap_pct = (float(o.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
        close_rng = (float(c.iloc[-1]) - float(l.iloc[-1])) / max(0.01, float(h.iloc[-1]) - float(l.iloc[-1]))
        if gap_pct > 2.0 and close_rng > 0.6:
            scores['gap'] = 1.0
        elif gap_pct > 1.0 and close_rng > 0.5:
            scores['gap'] = 0.6
        elif gap_pct > 0.0 and close_rng > 0.5:
            scores['gap'] = 0.3
        else:
            scores['gap'] = 0.0
    else:
        scores['gap'] = 0.0

    return round(sum(scores.values()) / len(scores) * 100) if scores else 0

def atr(h, l, c, p=14):
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(p).mean().iloc[-1])

def ema(s, span):
    return s.ewm(span=span, adjust=False).mean()

def up_dn_vol(c, v, d=10):
    diffs = c.diff().iloc[-d:]
    vols  = v.iloc[-d:]
    up    = vols[diffs > 0].sum()
    dn    = vols[diffs <= 0].sum()
    return round(float(up) / float(dn), 2) if dn > 0 else 2.0

def sig_score(sigs):
    return round(sum(sigs.values()) / len(sigs) * 100) if sigs else 0


# ──────────────────────────────────────────────
#  QUALITY SCORING — institutional-flow proxies,
#  relative strength, breakout-phase tagging
# ──────────────────────────────────────────────

def rvol50(v):
    """Relative volume vs trailing 50-day average (excluding today).
    3x+ = genuine institutional interest; 1.5x = elevated."""
    if len(v) < 51:
        return rvol(v)  # fall back to 20d if insufficient history
    avg = float(v.iloc[-51:-1].mean())
    return round(float(v.iloc[-1]) / avg, 2) if avg > 0 else 0.0


def obv_slope(c, v, lookback=20):
    """Slope of On-Balance-Volume over `lookback` days, normalized by
    average daily volume. Positive = accumulation, negative = distribution.
    Returns value in roughly [-1, +1]."""
    if len(c) < lookback + 2:
        return 0.0
    direction = np.sign(c.diff().fillna(0))
    obv = (direction * v).cumsum()
    seg = obv.iloc[-lookback:].values.astype(float)
    if np.std(seg) == 0:
        return 0.0
    x = np.arange(len(seg))
    slope = float(np.polyfit(x, seg, 1)[0])
    avg_v = float(v.iloc[-lookback:].mean())
    if avg_v <= 0:
        return 0.0
    return max(-1.5, min(1.5, slope / avg_v))


def cmf(h, l, c, v, p=20):
    """Chaikin Money Flow — closes near highs on volume = accumulation.
    >0.15 = strong inflow; <-0.10 = strong outflow."""
    if len(c) < p:
        return 0.0
    rng = (h - l).replace(0, np.nan)
    mfm = ((c - l) - (h - c)) / rng
    mfv = mfm * v
    vol_sum = float(v.iloc[-p:].sum())
    return float(mfv.iloc[-p:].sum() / vol_sum) if vol_sum > 0 else 0.0


def rs_vs_benchmark(c, bench_c, periods=(21, 63)):
    """IBD-style relative strength: stock return minus benchmark return
    (in %), averaged across the given periods. Positive = outperforming."""
    if bench_c is None or len(c) < max(periods) + 1 or len(bench_c) < max(periods) + 1:
        return None
    scores = []
    for p in periods:
        try:
            s_ret = float(c.iloc[-1]) / float(c.iloc[-p-1]) - 1
            b_ret = float(bench_c.iloc[-1]) / float(bench_c.iloc[-p-1]) - 1
            scores.append((s_ret - b_ret) * 100)
        except Exception:
            continue
    return round(sum(scores) / len(scores), 1) if scores else None


def weinstein_stage(c):
    """Stan Weinstein stage classification using the 30-week (150d) SMA.
    Stage 2 = price above a rising 30w SMA = where the 10-15%/week moves live.
    Stage 4 = below a falling 30w SMA = avoid."""
    if len(c) < 150:
        return "Unknown"
    sma = c.rolling(150).mean()
    sma_now = float(sma.iloc[-1])
    look_back = min(21, len(sma) - 1)
    sma_ago = float(sma.iloc[-look_back])
    if np.isnan(sma_now) or np.isnan(sma_ago):
        return "Unknown"
    price = float(c.iloc[-1])
    rising  = sma_now > sma_ago * 1.005
    falling = sma_now < sma_ago * 0.995
    above   = price > sma_now
    if above and rising:    return "Stage2"
    if above and falling:   return "Stage3"
    if (not above) and falling: return "Stage4"
    return "Stage1"


def base_tightness(h, l, c, lookback=10):
    """ATR contraction ratio: current 14d ATR / 14d ATR `lookback` days ago.
    <0.85 = base is tightening (volatility contraction precedes expansion)."""
    if len(c) < lookback + 15:
        return 1.0
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr_ser = tr.rolling(14).mean()
    atr_now  = float(atr_ser.iloc[-1])
    atr_then = float(atr_ser.iloc[-lookback])
    return round(atr_now / atr_then, 2) if atr_then > 0 else 1.0


def pivot_distance(c, h, lookback=20):
    """% above/below the recent `lookback`-day pivot high.
    ~0 = at the breakout point (best R:R). >5% = extended, late entry."""
    if len(c) < lookback + 1:
        return 0.0
    pivot = float(h.iloc[-lookback-1:-1].max())
    price = float(c.iloc[-1])
    return round((price - pivot) / pivot * 100, 1) if pivot > 0 else 0.0


def breakout_phase(c, h, l, lookback=20, window=10):
    """Tag where the stock is in the breakout process.
    Base        — still under a tight pivot, primed
    Breakout    — broke pivot in last 1-2 days (best entry)
    Continuation— 3-5 days post-breakout, holding
    Extended    — far above pivot, chasing risk
    Failed      — broke pivot then fell back below"""
    if len(c) < lookback + window + 1:
        return "Unknown"
    pivot_series = h.iloc[-lookback-window-1:-window-1]
    if len(pivot_series) == 0:
        return "Unknown"
    pivot = float(pivot_series.max())
    if pivot <= 0:
        return "Unknown"
    recent = c.iloc[-window:]
    price = float(c.iloc[-1])
    above_mask = (recent > pivot).values
    pct_above = (price - pivot) / pivot * 100

    if not above_mask.any():
        return "Base" if base_tightness(h, l, c, 10) < 0.88 else "—"
    if price < pivot * 0.985:
        return "Failed"
    first_break = int(np.argmax(above_mask))   # first True index
    days_since  = (len(above_mask) - 1) - first_break
    if days_since <= 1:
        return "Breakout"
    if days_since <= 5 and pct_above < 10:
        return "Continuation"
    return "Extended"


def setup_quality_score(df, bench_c, pop=0):
    """Unified 0-100 quality score for swing setups targeting 10-15% in a week.
    Combines: RVOL vs 50d, OBV/CMF inflow proxies, RS vs SPY, Weinstein
    stage, pivot proximity, base tightness, gap-vs-ATR, popularity.
    Returns (score, phase, meta_dict)."""
    if df is None or len(df) < 25:
        return 0, "Unknown", {}
    c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
    try:
        price = float(c.iloc[-1])
        if price <= 0:
            return 0, "Unknown", {}
        a   = atr(h, l, c)
        atp = (a / price) * 100 if price > 0 else 0

        rv50      = rvol50(v)
        obvs      = obv_slope(c, v, 20)
        cmf20     = cmf(h, l, c, v, 20)
        rs        = rs_vs_benchmark(c, bench_c)
        stage     = weinstein_stage(c)
        pivot_d   = pivot_distance(c, h, 20)
        tightness = base_tightness(h, l, c, 10)
        phase     = breakout_phase(c, h, l, 20, 10)
        gap_pct   = (float(o.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100 if len(c) >= 2 else 0
        gap_atr   = gap_pct / atp if atp > 0 else 0

        # Each sub-score is 0..10
        comps = {
            "RVOL50":   10 if rv50  >= 3.0 else (7 if rv50  >= 2.0 else (4 if rv50  >= 1.3 else 0)),
            "OBV":      10 if obvs  >  0.5 else (6 if obvs  >  0.0 else 0),
            "CMF":      10 if cmf20 >  0.15 else (6 if cmf20 > 0.05 else (3 if cmf20 > 0 else 0)),
            "RS":       (10 if rs >  5 else (7 if rs > 0 else (3 if rs > -5 else 0))) if rs is not None else 5,
            "Stage":    10 if stage == "Stage2" else (5 if stage == "Stage1" else (2 if stage == "Stage3" else 0)),
            "Pivot":    10 if -2 <= pivot_d <= 3 else (6 if -5 <= pivot_d < -2 else (2 if pivot_d > 5 else 0)),
            "Tight":    10 if tightness < 0.80 else (6 if tightness < 0.90 else 0),
            "GapATR":   10 if gap_atr >= 1.5 else (6 if gap_atr >= 0.8 else (3 if gap_atr > 0 else 0)),
            "Phase":    10 if phase == "Breakout" else (8 if phase == "Continuation" else (6 if phase == "Base" else (2 if phase == "Extended" else 0))),
            "Pop":      10 if pop > 100 else (6 if pop > 30 else (2 if pop > 5 else 0)),
        }
        raw = sum(comps.values())
        score = round(raw / (len(comps) * 10) * 100)
        meta = {
            "rvol50": rv50, "obv_slope": round(obvs, 3), "cmf": round(cmf20, 3),
            "rs": rs, "stage": stage, "pivot_dist": pivot_d, "tightness": tightness,
            "gap_atr": round(gap_atr, 2),
        }
        return score, phase, meta
    except Exception:
        return 0, "Unknown", {}


# ──────────────────────────────────────────────
#  COUNTRY FILTER
# ──────────────────────────────────────────────

_CHINA_MARKERS  = {"china", "hong kong", "cayman islands", "british virgin islands"}
_ISRAEL_MARKERS = {"israel"}

def _company_country(ticker: str, info_map: dict) -> str:
    """Return lowercase country string from yfinance info, or empty string."""
    info = info_map.get(ticker, {})
    return str(info.get("country", "") or "").lower().strip()

def _is_blocked(ticker: str, info_map: dict, strategy: int) -> bool:
    """Return True if this ticker should be excluded for this strategy."""
    country = _company_country(ticker, info_map)
    if any(m in country for m in _ISRAEL_MARKERS):
        return True
    if strategy == 1 and any(m in country for m in _CHINA_MARKERS):
        return True
    return False


# ──────────────────────────────────────────────
#  STRATEGY 1: HIGH RVOL / CATALYST
# ──────────────────────────────────────────────

def s1_catalyst(data_map, info_map, pop_scores, bench_c=None):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 1):
                continue
            if len(df) < 20:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price = float(c.iloc[-1])
            if not (0.50 <= price <= 10.0):
                continue

            rv      = rvol(v)
            r       = rsi(c)
            a       = atr(h, l, c)
            atp     = (a / price) * 100
            day_chg = (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
            v5avg   = v.iloc[-6:-1].mean()
            vspike  = float(v.iloc[-1]) / v5avg if v5avg > 0 else 0
            h20     = float(h.iloc[-21:-1].max())
            near_brk= price >= h20 * 0.97
            mom3    = (float(c.iloc[-1]) - float(c.iloc[-4])) / float(c.iloc[-4]) * 100
            body    = abs(float(c.iloc[-1]) - float(o.iloc[-1]))
            rng     = float(h.iloc[-1]) - float(l.iloc[-1])
            body_r  = body / rng if rng > 0 else 0
            pop     = pop_scores.get(ticker, 0)

            sigs = {
                "rvol":        1.0 if rv >= 2.5 else (0.5 if rv >= 1.5 else 0.0),
                "price_range": 1.0,
                "rsi_zone":    1.0 if 42 <= r <= 72 else 0.0,
                "big_move":    1.0 if abs(day_chg) >= 5 else (0.5 if abs(day_chg) >= 3 else 0.0),
                "breakout":    1.0 if near_brk else 0.0,
                "vol_spike":   1.0 if vspike >= 3 else (0.5 if vspike >= 2 else 0.0),
                "momentum":    1.0 if mom3 > 0 else 0.0,
                "high_atr":    1.0 if atp >= 5 else (0.5 if atp >= 3 else 0.0),
                "bull_candle": 1.0 if (body_r > 0.55 and c.iloc[-1] > o.iloc[-1]) else 0.0,
                "popular":     1.0 if pop > 100 else (0.5 if pop > 20 else 0.0),
            }

            conf   = sig_score(sigs)
            if conf < 40:
                continue

            setupq, phase, _meta = setup_quality_score(df, bench_c, pop)
            info   = info_map.get(ticker, {})
            catq   = catalyst_quality_score(info, c, h, l, o, v, bench_c, rv, price)

            target = round(price * (1 + atp / 100 * 2.2), 2)
            stop   = round(price * (1 - atp / 100 * 0.9), 2)
            rr     = round((target - price) / (price - stop), 2) if price > stop else 0

            cap    = info.get("marketCap", 0)
            cap_lbl= "Micro" if cap < 300e6 else ("Small" if cap < 2e9 else "Mid")
            industry = info.get("industry", "—")
            country = info.get("country", "—")

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}", RVOL=f"{rv}x",
                DayChg=f"{day_chg:+.1f}%", RSI=round(r, 1),
                ATR_pct=f"{atp:.1f}%", Cap=cap_lbl,
                Phase=phase, SetupQ=setupq, CatQ=catq,
                Breakout="✅" if near_brk else "—",
                PopScore=round(pop), Target=f"${target}",
                Stop=f"${stop}", RR=f"1:{rr}",
                Confidence=conf, Industry=industry, Country=country,
                _score=(conf + setupq + catq) / 3,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 2: MOMENTUM SWING
# ──────────────────────────────────────────────

def s2_swing(data_map, info_map, pop_scores, bench_c=None):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 2):
                continue
            if len(df) < 55:
                continue
            c, v, h, l = df["Close"], df["Volume"], df["High"], df["Low"]
            price = float(c.iloc[-1])
            if price < 5:
                continue

            e20  = float(ema(c, 20).iloc[-1])
            e50  = float(ema(c, 50).iloc[-1])
            e200 = float(ema(c, 200).iloc[-1]) if len(c) >= 200 else None
            r    = rsi(c)
            a    = atr(h, l, c)
            atp  = (a / price) * 100
            hh   = float(h.iloc[-10:].iloc[-1]) > float(h.iloc[-10:].iloc[0])
            hl   = float(l.iloc[-10:].iloc[-1]) > float(l.iloc[-10:].iloc[0])
            uvr  = up_dn_vol(c, v)
            h52  = float(h.rolling(252).max().iloc[-1]) if len(h) >= 252 else float(h.max())
            m1m  = (float(c.iloc[-1]) - float(c.iloc[-22])) / float(c.iloc[-22]) * 100 if len(c) >= 22 else 0
            m3m  = (float(c.iloc[-1]) - float(c.iloc[-63])) / float(c.iloc[-63]) * 100 if len(c) >= 63 else 0
            vol_rising = float(v.iloc[-5:].mean()) > float(v.iloc[-20:-5].mean())
            pop  = pop_scores.get(ticker, 0)

            info    = info_map.get(ticker, {})
            rev_g   = info.get("revenueGrowth", None)
            fwd_eps = info.get("forwardEps", None)
            industry = info.get("industry", "—")
            country = info.get("country", "—")

            sigs = {
                "above_e20":  1.0 if price > e20 else 0.0,
                "above_e50":  1.0 if price > e50 else 0.0,
                "ema_stack":  1.0 if e20 > e50 else 0.0,
                "above_e200": 1.0 if (e200 and price > e200) else 0.0,
                "rsi_zone":   1.0 if 50 <= r <= 68 else (0.5 if 45 <= r < 50 else 0.0),
                "hh":         1.0 if hh else 0.0,
                "hl":         1.0 if hl else 0.0,
                "up_vol":     1.0 if uvr >= 1.3 else (0.5 if uvr >= 1.0 else 0.0),
                "mom_1m":     1.0 if m1m > 5 else (0.5 if m1m > 0 else 0.0),
                "vol_rising": 1.0 if vol_rising else 0.0,
                "rev_growth": 1.0 if (rev_g and rev_g > 0.1) else 0.0,
                "pos_eps":    1.0 if (fwd_eps and fwd_eps > 0) else 0.0,
                "near_52h":   1.0 if price >= h52 * 0.90 else 0.0,
                "popular":    1.0 if pop > 50 else (0.5 if pop > 10 else 0.0),
            }

            conf = sig_score(sigs)
            if conf < 45:
                continue

            setupq, phase, _meta = setup_quality_score(df, bench_c, pop)

            tgt_pct = max(15, min(35, m1m * 1.5 + 10))
            target  = round(price * (1 + tgt_pct / 100), 2)
            stop    = round(max(e50, price * 0.90), 2)
            tf      = "4–8 wks" if conf >= 72 else "8–16 wks"

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}", RSI=round(r, 1),
                vsEMA20=f"{((price/e20)-1)*100:+.1f}%",
                vsEMA50=f"{((price/e50)-1)*100:+.1f}%",
                Mom1M=f"{m1m:+.1f}%", Mom3M=f"{m3m:+.1f}%",
                UpDnVol=f"{uvr}x",
                HH_HL="✅" if (hh and hl) else ("⚠️" if (hh or hl) else "❌"),
                Phase=phase, SetupQ=setupq,
                PopScore=round(pop),
                Target=f"${target}(+{tgt_pct:.0f}%)",
                Stop=f"${stop}", Timeframe=tf,
                Confidence=conf, Industry=industry, Country=country,
                _score=(conf + setupq) / 2,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 3: GAP & BREAKOUT
# ──────────────────────────────────────────────

def s3_breakout(data_map, info_map, pop_scores, bench_c=None):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 3):
                continue
            if len(df) < 30:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price   = float(c.iloc[-1])
            if price < 1:
                continue

            gap_pct = (float(o.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
            h20     = float(h.iloc[-21:-1].max())
            h52     = float(h.rolling(252).max().iloc[-1]) if len(h) >= 252 else float(h.max())
            rv      = rvol(v)
            r       = rsi(c)
            a       = atr(h, l, c)
            atp     = (a / price) * 100
            p_range = (float(h.iloc[-11:-1].max()) - float(l.iloc[-11:-1].min())) / float(c.iloc[-11])
            flat    = p_range < 0.12
            atr_ser = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1).rolling(14).mean()
            vcon    = float(atr_ser.iloc[-1]) < float(atr_ser.iloc[-6:-1].mean()) * 0.85
            day_chg = (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
            body    = abs(float(c.iloc[-1]) - float(o.iloc[-1]))
            rng_c   = float(h.iloc[-1]) - float(l.iloc[-1])
            body_r  = body / rng_c if rng_c > 0 else 0
            pop     = pop_scores.get(ticker, 0)

            sigs = {
                "gap_up":       1.0 if gap_pct >= 3 else (0.5 if gap_pct >= 1 else 0.0),
                "h20_break":    1.0 if price >= h20 * 0.98 else 0.0,
                "h52_break":    1.0 if price >= h52 * 0.97 else 0.0,
                "rvol":         1.0 if rv >= 2.5 else (0.5 if rv >= 1.5 else 0.0),
                "flat_base":    1.0 if flat else 0.0,
                "vol_contract": 1.0 if vcon else 0.0,
                "rsi_ok":       1.0 if r < 76 else 0.0,
                "bull_candle":  1.0 if (body_r > 0.6 and c.iloc[-1] > o.iloc[-1]) else 0.0,
                "day_chg":      1.0 if day_chg > 2 else (0.5 if day_chg > 0 else 0.0),
                "atr_expand":   1.0 if atp > 3 else 0.0,
                "popular":      1.0 if pop > 80 else (0.5 if pop > 20 else 0.0),
            }

            conf   = sig_score(sigs)
            if conf < 42:
                continue

            setupq, phase, _meta = setup_quality_score(df, bench_c, pop)

            tgt_pct = gap_pct * 1.5 + 5
            target  = round(price * (1 + tgt_pct / 100), 2)
            stop    = round(price * 0.94, 2)
            rr      = round((target - price) / (price - stop), 2) if price > stop else 0

            info    = info_map.get(ticker, {})
            cap     = info.get("marketCap", 0)
            cap_lbl = "Micro" if cap < 300e6 else ("Small" if cap < 2e9 else ("Mid" if cap < 10e9 else "Large"))
            industry = info.get("industry", "—")
            country = info.get("country", "—")

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}",
                Gap=f"{gap_pct:+.1f}%", RVOL=f"{rv}x",
                RSI=round(r, 1), DayChg=f"{day_chg:+.1f}%",
                FlatBase="✅" if flat else "—",
                H52Break="✅" if price >= h52*0.97 else "—",
                Phase=phase, SetupQ=setupq,
                Cap=cap_lbl, PopScore=round(pop),
                Target=f"${target}(+{tgt_pct:.0f}%)",
                Stop=f"${stop}", RR=f"1:{rr}",
                Confidence=conf, Industry=industry, Country=country,
                _score=(conf + setupq) / 2,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 4: EARNINGS VOLATILITY SETUP
# ──────────────────────────────────────────────

def s4_earnings_setup(data_map, info_map, pop_scores):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 4):
                continue
            if len(df) < 20:
                continue
            c, v, h, l = df["Close"], df["Volume"], df["High"], df["Low"]
            price = float(c.iloc[-1])
            if price < 1:
                continue

            info     = info_map.get(ticker, {})
            earn_date = None
            try:
                t_obj = yf.Ticker(ticker)
                cal   = t_obj.calendar
                if cal is not None and not cal.empty:
                    if "Earnings Date" in cal.columns:
                        earn_date = pd.to_datetime(cal["Earnings Date"].iloc[0])
                    elif hasattr(cal, "T") and "Earnings Date" in cal.T.columns:
                        earn_date = pd.to_datetime(cal.T["Earnings Date"].iloc[0])
            except Exception:
                pass

            if earn_date is None:
                continue
            et_tz  = pytz.timezone("America/New_York")
            now_et = datetime.now(et_tz)
            earn_dt = earn_date if earn_date.tzinfo else et_tz.localize(earn_date)
            days_to = (earn_dt.date() - now_et.date()).days
            if not (0 <= days_to <= 3):
                continue

            e20    = float(ema(c, 20).iloc[-1])
            e50    = float(ema(c, 50).iloc[-1])
            r      = rsi(c)
            a      = atr(h, l, c)
            atp    = (a / price) * 100
            rv     = rvol(v)
            m1m    = (float(c.iloc[-1]) - float(c.iloc[-22])) / float(c.iloc[-22]) * 100 if len(c) >= 22 else 0
            h52    = float(h.rolling(252).max().iloc[-1]) if len(h) >= 252 else float(h.max())
            range_5d   = (float(h.iloc[-5:].max()) - float(l.iloc[-5:].min())) / price * 100
            compressed = range_5d < 6.0
            eps_surprise = info.get("earningsQuarterlyGrowth", None)
            fwd_eps      = info.get("forwardEps", None)
            rev_growth   = info.get("revenueGrowth", None)
            beat_history = 1.0 if (eps_surprise and eps_surprise > 0.05) else 0.0
            trending_into = price > e20 > e50 and r > 52 and m1m > 5
            play_type = "📈 Momentum into earnings" if trending_into else ("📉 IV crush / sell spike" if compressed else "⚠️ Speculative")

            sigs = {
                "earnings_soon":    1.0,
                "above_e20":        1.0 if price > e20 else 0.0,
                "rsi_healthy":      1.0 if 45 <= r <= 72 else 0.0,
                "positive_1m_mom":  1.0 if m1m > 3 else 0.0,
                "near_52w_high":    1.0 if price >= h52 * 0.88 else 0.0,
                "compressed_range": 1.0 if compressed else 0.0,
                "beat_history":     beat_history,
                "positive_fwd_eps": 1.0 if (fwd_eps and fwd_eps > 0) else 0.0,
                "rev_growth":       1.0 if (rev_growth and rev_growth > 0.05) else 0.0,
                "popular":          1.0 if pop_scores.get(ticker, 0) > 30 else 0.0,
            }

            conf = sig_score(sigs)
            if conf < 40:
                continue

            exp_move = round(atp * 2.5, 1)
            target   = round(price * (1 + exp_move / 100), 2)
            stop     = round(price * (1 - atp / 100 * 1.2), 2)
            industry = info.get("industry", "—")
            country = info.get("country", "—")

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}", EarnIn=f"{days_to}d",
                PlayType=play_type, RSI=round(r, 1), Mom1M=f"{m1m:+.1f}%",
                Range5D=f"{range_5d:.1f}%", RVOL=f"{rv}x",
                ExpMove=f"±{exp_move}%", Target=f"${target}", Stop=f"${stop}",
                PopScore=round(pop_scores.get(ticker, 0)),
                Confidence=conf, Industry=industry, Country=country,
                _score=conf,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 5: OVERSOLD REVERSAL HUNTER
# ──────────────────────────────────────────────

def s5_oversold_reversal(data_map, info_map, pop_scores):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 5):
                continue
            if len(df) < 40:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price = float(c.iloc[-1])
            if price < 2:
                continue

            # — was it oversold? (gate: currently OR within last 10 days)
            r_ser = rsi_series(c)
            r = float(r_ser.iloc[-1])
            rsi_min_10d = float(r_ser.iloc[-10:].min()) if len(r_ser) >= 10 else r
            if not (r < 40 or rsi_min_10d < 32):
                continue

            a    = atr(h, l, c)
            atp  = (a / price) * 100
            e20  = float(ema(c, 20).iloc[-1])
            e50  = float(ema(c, 50).iloc[-1])
            e200 = float(ema(c, 200).iloc[-1]) if len(c) >= 200 else None

            last5_chg  = [float(c.iloc[i] - c.iloc[i-1]) for i in range(-5, 0)]
            red_streak = sum(1 for x in last5_chg if x < 0)
            loss_5d    = (float(c.iloc[-1]) - float(c.iloc[-6])) / float(c.iloc[-6]) * 100

            body    = abs(float(c.iloc[-1]) - float(o.iloc[-1]))
            rng_c   = float(h.iloc[-1]) - float(l.iloc[-1])
            lower_w = float(o.iloc[-1] if c.iloc[-1] > o.iloc[-1] else c.iloc[-1]) - float(l.iloc[-1])
            body_r  = body / rng_c if rng_c > 0 else 0
            hammer  = lower_w > body * 2 and body_r < 0.4

            vol_dry   = float(v.iloc[-1]) < float(v.iloc[-6:-1].mean()) * 0.75
            above_200 = (e200 is not None and price > e200 * 0.92)
            l52       = float(l.rolling(252).min().iloc[-1]) if len(l) >= 252 else float(l.min())
            pct_off_low = (price - l52) / l52 * 100

            # — reversal confirmation (the "it's actually climbing back" signals)
            bot_days, _rsi_low, rsi_up_from_low = rsi_inflection(r_ser, lookback=10)
            rsi_turning = (bot_days is not None
                           and 1 <= bot_days <= 7
                           and rsi_up_from_low is not None
                           and rsi_up_from_low >= 3)
            divergence  = bullish_rsi_divergence(c, r_ser, lookback=15)
            macd_up     = macd_histogram_turning_up(c)
            reclaim10   = ema_reclaim(c, span=10, min_below_days=5)
            reclaim20   = ema_reclaim(c, span=20, min_below_days=5)
            hl_struct   = higher_low_structure(l, lookback=15)
            up_vol_exp  = up_vol_expansion(c, v, lookback=10)
            upper_half  = closes_upper_half(h, l, c, days=2)

            reversal_flags = {
                "RSI↑":   rsi_turning,
                "Div":    divergence,
                "MACD↑":  macd_up,
                "E10":    reclaim10,
                "E20":    reclaim20 and not reclaim10,  # don't double-count
                "HL":     hl_struct,
                "UpV":    up_vol_exp,
                "UpHalf": upper_half,
            }
            rev_count = sum(1 for x in reversal_flags.values() if x)
            # — hard gate: must show proven movement, not just oversold
            if rev_count < 2:
                continue

            info    = info_map.get(ticker, {})
            fwd_eps = info.get("forwardEps", None)
            cap     = info.get("marketCap", 0)
            cap_lbl = "Micro" if cap < 300e6 else ("Small" if cap < 2e9 else ("Mid" if cap < 10e9 else "Large"))

            sigs = {
                # — oversold context (was the selloff real?)
                "deep_oversold":    1.0 if rsi_min_10d < 25 else (0.7 if rsi_min_10d < 30 else 0.4),
                "red_streak":       1.0 if red_streak >= 3 else (0.5 if red_streak >= 2 else 0.0),
                "big_selloff":      1.0 if loss_5d < -10 else (0.5 if loss_5d < -6 else 0.0),
                "vol_drying":       1.0 if vol_dry else 0.0,
                # — reversal confirmation (is it actually climbing?)
                "rsi_turning_up":   1.0 if rsi_turning else 0.0,
                "rsi_divergence":   1.0 if divergence else 0.0,
                "macd_hist_up":     1.0 if macd_up else 0.0,
                "ema_reclaim":      1.0 if reclaim10 else (0.5 if reclaim20 else 0.0),
                "higher_low":       1.0 if hl_struct else 0.0,
                "up_vol_expand":    1.0 if up_vol_exp else 0.0,
                "close_upper_half": 1.0 if upper_half else 0.0,
                "hammer_candle":    1.0 if hammer else 0.0,
                # — structural / quality
                "above_200":        1.0 if above_200 else 0.0,
                "holding_support":  1.0 if pct_off_low > 5 else 0.0,
                "positive_eps":     1.0 if (fwd_eps and fwd_eps > 0) else 0.0,
                "popular":          1.0 if pop_scores.get(ticker, 0) > 20 else 0.0,
            }

            conf = sig_score(sigs)
            if conf < 42:
                continue

            target     = round(max(e20, price * (1 + atp / 100 * 1.5)), 2)
            stop       = round(price * (1 - atp / 100 * 0.8), 2)
            bounce_pct = round((target - price) / price * 100, 1)
            rr         = round((target - price) / (price - stop), 2) if price > stop else 0
            industry   = info.get("industry", "—")
            country    = info.get("country", "—")
            signs_str  = " ".join(k for k, x in reversal_flags.items() if x) or "—"

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}",
                RSI=round(r, 1), RSImin=round(rsi_min_10d, 1),
                Loss5D=f"{loss_5d:.1f}%", RedDays=red_streak,
                RevSigns=signs_str, Rev=rev_count,
                Hammer="✅" if hammer else "—",
                VolDry="✅" if vol_dry else "—",
                Cap=cap_lbl,
                Target=f"${target}(+{bounce_pct}%)",
                Stop=f"${stop}", RR=f"1:{rr}",
                PopScore=round(pop_scores.get(ticker, 0)),
                Confidence=conf, Industry=industry, Country=country,
                _score=conf + rev_count * 2,  # bias toward setups with more confirmation
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 6: SECTOR ROTATION TRACKER
# ──────────────────────────────────────────────

SECTOR_ETFS = {
    "Technology":       "XLK",
    "Semiconductors":   "SOXX",
    "Biotech":          "XBI",
    "Energy":           "XLE",
    "Financials":       "XLF",
    "Consumer Discret": "XLY",
    "Industrials":      "XLI",
    "Healthcare":       "XLV",
    "Real Estate":      "XLRE",
    "Utilities":        "XLU",
    "Small Cap":        "IWM",
    "ARK Innovation":   "ARKK",
}

def s6_sector_rotation(data_map, info_map, pop_scores):
    sector_scores = {}
    sector_data   = {}
    for sector_name, etf_ticker in SECTOR_ETFS.items():
        try:
            etf_df = yf.download(etf_ticker, period="30d", interval="1d",
                                  auto_adjust=True, progress=False)
            if etf_df is None or len(etf_df) < 10:
                continue
            ec  = etf_df["Close"]
            ev  = etf_df["Volume"]
            m5d  = (float(ec.iloc[-1]) - float(ec.iloc[-6])) / float(ec.iloc[-6]) * 100
            m20d = (float(ec.iloc[-1]) - float(ec.iloc[-21])) / float(ec.iloc[-21]) * 100 if len(ec) >= 21 else 0
            rv   = rvol(ev)
            r    = rsi(ec)
            above_e20 = float(ec.iloc[-1]) > float(ema(ec, 20).iloc[-1])
            score = m5d * 2 + m20d + (rv * 5 if rv > 1.2 else 0) + (10 if above_e20 else 0)
            sector_scores[sector_name] = round(score, 1)
            sector_data[sector_name]   = {"m5d": m5d, "m20d": m20d, "rvol": rv, "rsi": round(r,1), "etf": etf_ticker}
        except Exception:
            pass
        time.sleep(0.15)

    if not sector_scores:
        return []

    hot_sectors  = sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)
    top_sectors  = [s for s, _ in hot_sectors[:4]]
    sector_leaderboard = []
    for sector_name, score in hot_sectors:
        d = sector_data.get(sector_name, {})
        sector_leaderboard.append(dict(
            Sector=sector_name, ETF=d.get("etf", ""),
            Mom5D=f"{d.get('m5d', 0):+.1f}%", Mom20D=f"{d.get('m20d', 0):+.1f}%",
            RVOL=f"{d.get('rvol', 0)}x", RSI=d.get("rsi", 0),
            HeatScore=score,
            Trend="🔥 HOT" if score > 15 else ("📈 Warm" if score > 5 else ("❄️ Cold" if score < -5 else "Neutral")),
        ))

    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 6):
                continue
            info   = info_map.get(ticker, {})
            sector = info.get("sector", "")
            if not sector:
                continue
            sector_map = {
                "Technology": "Technology", "Semiconductors": "Semiconductors",
                "Healthcare": "Healthcare", "Energy": "Energy",
                "Financial Services": "Financials", "Consumer Cyclical": "Consumer Discret",
                "Industrials": "Industrials", "Real Estate": "Real Estate",
                "Utilities": "Utilities",
            }
            mapped = sector_map.get(sector, sector)
            if mapped not in top_sectors:
                continue
            if len(df) < 30:
                continue
            c, v, h, l = df["Close"], df["Volume"], df["High"], df["Low"]
            price = float(c.iloc[-1])
            if price < 2:
                continue

            e20  = float(ema(c, 20).iloc[-1])
            e50  = float(ema(c, 50).iloc[-1])
            r    = rsi(c)
            a    = atr(h, l, c)
            atp  = (a / price) * 100
            rv   = rvol(v)
            m5d  = (float(c.iloc[-1]) - float(c.iloc[-6])) / float(c.iloc[-6]) * 100
            m20d = (float(c.iloc[-1]) - float(c.iloc[-21])) / float(c.iloc[-21]) * 100 if len(c) >= 21 else 0
            h52  = float(h.rolling(252).max().iloc[-1]) if len(h) >= 252 else float(h.max())
            s_score = sector_scores.get(mapped, 0)

            sigs = {
                "hot_sector":   1.0 if s_score > 15 else (0.7 if s_score > 5 else 0.3),
                "above_e20":    1.0 if price > e20 else 0.0,
                "above_e50":    1.0 if price > e50 else 0.0,
                "rsi_zone":     1.0 if 48 <= r <= 70 else 0.0,
                "stock_5d_mom": 1.0 if m5d > 3 else (0.5 if m5d > 0 else 0.0),
                "rvol_confirm": 1.0 if rv >= 1.5 else 0.0,
                "near_52h":     1.0 if price >= h52 * 0.90 else 0.0,
                "popular":      1.0 if pop_scores.get(ticker, 0) > 20 else 0.0,
            }

            conf = sig_score(sigs)
            if conf < 45:
                continue

            target  = round(price * (1 + atp / 100 * 2), 2)
            stop    = round(max(e50, price * 0.92), 2)
            tgt_pct = round((target - price) / price * 100, 1)
            industry = info.get("industry", "—")
            country = info.get("country", "—")

            results.append(dict(
                Ticker=ticker, Sector=mapped, SectorHeat=f"{s_score:+.0f}",
                Price=f"${price:.2f}", RSI=round(r, 1),
                Mom5D=f"{m5d:+.1f}%", Mom20D=f"{m20d:+.1f}%", RVOL=f"{rv}x",
                Target=f"${target}(+{tgt_pct}%)", Stop=f"${stop}",
                PopScore=round(pop_scores.get(ticker, 0)),
                Confidence=conf, Industry=industry, Country=country,
                _score=conf + s_score * 0.3,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return sector_leaderboard, results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 7: OPENING RANGE BREAKOUT (ORB)
# ──────────────────────────────────────────────

def s7_orb(data_map, info_map, pop_scores, bench_c=None):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 7):
                continue
            if len(df) < 21:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price   = float(c.iloc[-1])
            if price < 1:
                continue

            today_o = float(o.iloc[-1])
            today_h = float(h.iloc[-1])
            today_l = float(l.iloc[-1])
            prev_c  = float(c.iloc[-2])
            prev_h  = float(h.iloc[-2])

            gap_pct     = (today_o - prev_c) / prev_c * 100
            gap_up      = gap_pct > 0.5
            above_open  = price > today_o
            orb_break   = price >= prev_h * 0.99
            rng_c       = today_h - today_l
            pos_in_range = (price - today_l) / rng_c if rng_c > 0 else 0.5
            strong_open  = pos_in_range > 0.65

            rv          = rvol(v)
            r           = rsi(c)
            a           = atr(h, l, c)
            atp         = (a / price) * 100
            e20         = float(ema(c, 20).iloc[-1])
            e50         = float(ema(c, 50).iloc[-1])
            day_chg     = (price - prev_c) / prev_c * 100
            vol_vs_yday = float(v.iloc[-1]) / float(v.iloc[-2]) if float(v.iloc[-2]) > 0 else 1.0
            clean_trend = gap_up and above_open and strong_open

            info    = info_map.get(ticker, {})
            cap     = info.get("marketCap", 0)
            cap_lbl = "Micro" if cap < 300e6 else ("Small" if cap < 2e9 else ("Mid" if cap < 10e9 else "Large"))

            sigs = {
                "gap_up":          1.0 if gap_pct > 1 else (0.5 if gap_pct > 0 else 0.0),
                "above_open":      1.0 if above_open else 0.0,
                "orb_break":       1.0 if orb_break else 0.0,
                "strong_position": 1.0 if strong_open else 0.0,
                "rvol_high":       1.0 if rv >= 2.0 else (0.5 if rv >= 1.3 else 0.0),
                "vol_surge_today": 1.0 if vol_vs_yday >= 1.5 else (0.5 if vol_vs_yday >= 1.1 else 0.0),
                "rsi_zone":        1.0 if 48 <= r <= 72 else 0.0,
                "above_e20":       1.0 if price > e20 else 0.0,
                "above_e50":       1.0 if price > e50 else 0.0,
                "clean_trend":     1.0 if clean_trend else 0.0,
                "popular":         1.0 if pop_scores.get(ticker, 0) > 20 else 0.0,
            }

            conf = sig_score(sigs)
            if conf < 45:
                continue

            pop = pop_scores.get(ticker, 0)
            setupq, phase, _meta = setup_quality_score(df, bench_c, pop)

            orb_size = today_h - today_l
            target   = round(today_h + orb_size, 2)
            target2  = round(today_h + orb_size * 1.5, 2)
            stop     = round(today_o * 0.985, 2)
            rr       = round((target - price) / (price - stop), 2) if price > stop else 0
            industry = info.get("industry", "—")
            country = info.get("country", "—")

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}",
                Gap=f"{gap_pct:+.1f}%", DayChg=f"{day_chg:+.1f}%",
                PosInRange=f"{pos_in_range*100:.0f}%", RVOL=f"{rv}x",
                VolVsYday=f"{vol_vs_yday:.1f}x", RSI=round(r, 1), Cap=cap_lbl,
                OrbBreak="✅" if orb_break else "—",
                Phase=phase, SetupQ=setupq,
                Target1=f"${target}", Target2=f"${target2}",
                Stop=f"${stop}", RR=f"1:{rr}",
                PopScore=round(pop),
                Confidence=conf, Industry=industry, Country=country,
                _score=(conf + setupq) / 2,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  DISPLAY
# ──────────────────────────────────────────────

def display(title, subtitle, rows, color):
    if not rows:
        console.print(f"[yellow]No results for {title}[/yellow]\n")
        return
    console.print(Panel.fit(f"[bold]{title}[/bold]\n[dim]{subtitle}[/dim]",
                             border_style=color, padding=(0, 2)))
    col_widths = {
        "Ticker": 7, "Price": 9, "RVOL": 7, "DayChg": 8,
        "RSI": 6, "ATR_pct": 8, "Cap": 6, "Breakout": 9,
        "PopScore": 9, "Target": 14, "Stop": 10, "RR": 7,
        "Confidence": 11, "vsEMA20": 9, "vsEMA50": 9, "Mom1M": 8,
        "Mom3M": 8, "UpDnVol": 9, "HH_HL": 7, "Timeframe": 10,
        "Gap": 7, "FlatBase": 9, "H52Break": 10,
        "Phase": 13, "SetupQ": 8,
        "RSImin": 8, "RevSigns": 28, "Rev": 5, "CatQ": 6,
        "Industry": 20, "Country": 12,
    }
    t = Table(box=box.SIMPLE_HEAVY, header_style=f"bold {color}",
              show_lines=True, expand=True)
    for col in rows[0]:
        mw = col_widths.get(col, 8)
        t.add_column(col, no_wrap=True, min_width=mw)
    for row in rows:
        conf  = row.get("Confidence", 0)
        style = "bold green" if conf >= 75 else ("yellow" if conf >= 55 else "dim white")
        t.add_row(*[str(v) for v in row.values()], style=style)
    console.print(t)
    console.print()


def timing_banner():
    et   = pytz.timezone("America/New_York")
    now  = datetime.now(et)
    hour = now.hour + now.minute / 60
    if 20 <= hour <= 24 or hour < 2:
        window = "🌙 Overnight Prep Window"
        best   = "Strategies 4 (Earnings), 5 (Oversold), 6 (Sector Rotation)  →  --overnight"
        tip    = "Build your watchlist now. Know your setups before you sleep."
    elif 6 <= hour < 9.5:
        window = "Premarket"
        best   = "Strategies 1 (Catalyst), 3 (Gap/Breakout)  →  --morning"
        tip    = "Catch gap-ups and news plays before the open crowd."
    elif 9.5 <= hour < 10.0:
        window = "🔔 Market Open — First 30 Min (Chaotic)"
        best   = "Strategy 1 (RVOL/Catalyst) — be careful, wait for confirmation"
        tip    = "Wild first 30 min. Let price settle before entering most plays."
    elif 10.0 <= hour < 10.5:
        window = "⏰ 10 AM Window — PRIME ORB TIME"
        best   = "Strategy 7 (ORB) + Strategies 1, 3  →  --ten-am"
        tip    = "Direction is set. Opening range is established. Best entries here."
    elif 10.5 <= hour < 13:
        window = "Midday"
        best   = "Strategy 2 (Swing Momentum)"
        tip    = "Noise dies down. Swing setups consolidate near key levels."
    elif 15 <= hour < 16:
        window = "⚡ Power Hour"
        best   = "Strategies 2 (Swing) + 3 (Breakout)"
        tip    = "Institutional rebalancing. Best EOD entries for tomorrow."
    else:
        window = "After Hours"
        best   = "Review today + prep overnight  →  --overnight"
        tip    = "Good time to run overnight strategies for tomorrow."
    console.print(Panel.fit(
        f"[bold cyan]{now.strftime('%A %b %d, %Y')}  |  {now.strftime('%I:%M %p')} ET[/bold cyan]\n"
        f"Window: [green]{window}[/green]\n"
        f"Best now: [yellow]{best}[/yellow]\n"
        f"[dim]{tip}[/dim]",
        title="MARKET CLOCK", border_style="cyan"))
    console.print()


# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy",      type=int, choices=[1,2,3,4,5,6,7])
    ap.add_argument("--overnight",     action="store_true")
    ap.add_argument("--morning",       action="store_true")
    ap.add_argument("--ten-am",        action="store_true")
    ap.add_argument("--sources",       nargs="+",
                    default=["finviz", "yahoo", "reddit", "insider", "quality", "momentum", "finnhub", "movers"],
                    choices=["finviz", "yahoo", "reddit", "nasdaq", "insider", "quality", "momentum", "finnhub", "movers"])
    ap.add_argument("--max",           type=int, default=400)
    ap.add_argument("--large_universe", action="store_true",
                    help="Fetch a wider universe by pulling deeper from the quality-filtered "
                         "Finviz screens (more pages, higher cap). Defaults the cap to 1000.")
    ap.add_argument("--export",        action="store_true")
    ap.add_argument("--show-universe", action="store_true")
    ap.add_argument("--override_polygon", action="store_true", help="Skip Polygon and use yfinance only for price data")
    args = ap.parse_args()

    # In large mode, raise the cap unless the user explicitly set --max
    max_tickers = args.max
    if args.large_universe and args.max == 400:
        max_tickers = 1000

    console.print(Panel.fit(
        "[bold white]NASDAQ POPULARITY SCREENER  v4.0[/bold white]\n"
        "[dim]Finviz + Yahoo + StockTwits + Reddit + OpenInsider + Quality → ranked universe → 7 strategies[/dim]\n"
        "[dim]Strategies 1–3: Premarket/Open  |  4–6: Overnight Prep  |  7: 10 AM ORB[/dim]",
        border_style="bright_blue", padding=(1, 4)))
    timing_banner()

    if args.strategy:
        run = {args.strategy}
    elif args.overnight:
        run = {4, 5, 6}
    elif args.morning:
        run = {1, 2, 3}
    elif getattr(args, "ten_am", False):
        run = {7}
    else:
        run = {1, 2, 3, 4, 5, 6, 7}

    tickers, pop_scores = build_universe(
        sources=args.sources,
        max_tickers=max_tickers,
        show=args.show_universe,
        large=args.large_universe,
    )

    data_map = fetch_price_data(tickers, skip_polygon=args.override_polygon)
    info_map = fetch_fundamentals(list(data_map.keys()))
    enrich_short_interest(info_map)

    # SPY benchmark for relative-strength scoring
    bench_c = None
    try:
        bench_df = yf.download("SPY", period="1y", interval="1d",
                               auto_adjust=True, progress=False)
        if bench_df is not None and len(bench_df) > 60:
            bench_c = bench_df["Close"]
            console.print(f"[green]✔ Benchmark: SPY ({len(bench_c)} bars) loaded for RS scoring[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠ SPY benchmark failed: {e} — RS score will be neutral[/yellow]")
    console.print()

    exports = {}

    if 1 in run:
        r1 = s1_catalyst(data_map, info_map, pop_scores, bench_c=bench_c)
        display("STRATEGY 1 — CATALYST / HIGH RVOL",
                "Price <$10 | RVOL >2x | Explosive intraday | SetupQ blends RVOL50/OBV/CMF/RS/Stage/Phase",
                r1, "red")
        exports["s1_catalyst"] = r1

    if 2 in run:
        r2 = s2_swing(data_map, info_map, pop_scores, bench_c=bench_c)
        display("STRATEGY 2 — MOMENTUM SWING",
                "EMA stacked | RSI 50–68 | HH+HL | SetupQ favors Stage2 + RS leaders + accumulation",
                r2, "green")
        exports["s2_swing"] = r2

    if 3 in run:
        r3 = s3_breakout(data_map, info_map, pop_scores, bench_c=bench_c)
        display("STRATEGY 3 — GAP & BREAKOUT",
                "Gap up | Flat base break | Volume confirm | Phase tag flags Base/Breakout/Continuation/Extended",
                r3, "yellow")
        exports["s3_breakout"] = r3

    if 4 in run:
        r4 = s4_earnings_setup(data_map, info_map, pop_scores)
        display("STRATEGY 4 — EARNINGS VOLATILITY SETUP",
                "Earnings in 1–3 days | IV setup | Momentum into catalyst | Run: Night before",
                r4, "magenta")
        exports["s4_earnings"] = r4

    if 5 in run:
        r5 = s5_oversold_reversal(data_map, info_map, pop_scores)
        display("STRATEGY 5 — OVERSOLD REVERSAL HUNTER",
                "Was oversold + ≥2 reversal signs (RSI↑/Div/MACD↑/EMA reclaim/HL/UpV/UpHalf) | Run: Night before",
                r5, "cyan")
        exports["s5_oversold"] = r5

    if 6 in run:
        result6 = s6_sector_rotation(data_map, info_map, pop_scores)
        if result6 and len(result6) == 2:
            sector_lb, r6 = result6
            display("SECTOR HEAT MAP — Which sectors have money flowing in",
                    "Sorted by momentum score — top 4 sectors feed stock picks below",
                    sector_lb, "bright_magenta")
            display("STRATEGY 6 — TOP STOCKS IN HOT SECTORS",
                    "Stocks inside the strongest rotating sectors | EMA aligned | Run: Night before",
                    r6, "bright_magenta")
            exports["s6_sectors"]       = sector_lb
            exports["s6_sector_stocks"] = r6

    if 7 in run:
        r7 = s7_orb(data_map, info_map, pop_scores, bench_c=bench_c)
        display("STRATEGY 7 — OPENING RANGE BREAKOUT (10 AM)",
                "Gap held | Above open | Volume surging | First 30-min high broken | Run: 10 AM",
                r7, "bright_cyan")
        exports["s7_orb"] = r7

    if args.export:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        for name, data in exports.items():
            if data:
                fname = f"{name}_{ts}.csv"
                pd.DataFrame(data).to_csv(fname, index=False)
                console.print(f"[green]💾 {fname}[/green]")

    console.print(Panel(
        "[bold]WHEN TO RUN WHAT[/bold]\n\n"
        "[cyan]8 PM – Midnight (night before)[/cyan]\n"
        "  --overnight  →  Strategies 4 (Earnings), 5 (Oversold), 6 (Sector Rotation)\n\n"
        "[green]6:00 – 9:30 AM  (premarket)[/green]\n"
        "  --morning    →  Strategies 1 (Catalyst/RVOL), 2 (Swing), 3 (Gap/Breakout)\n\n"
        "[yellow]10:00 – 10:30 AM  (after open dust settles)[/yellow]\n"
        "  --ten-am     →  Strategy 7 (ORB)\n\n"
        "[dim]Best days: Tue/Wed/Thu for cleanest setups. Mon for gap plays. Thu for earnings.[/dim]",
        border_style="blue", padding=(0, 2)
    ))

    console.print(Panel.fit(
        "[bold yellow]⚠  DISCLAIMER[/bold yellow]\n"
        "[dim]Educational use only. Not financial advice. Trade at your own risk.[/dim]",
        border_style="yellow"))

    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    console.print(f"\n[dim]Completed: {end_time}[/dim]")


if __name__ == "__main__":
    main()


'''

Now I have bought the Stocks Starter on polygon for using the api,

the name and key are 
POLYGON_NAME=
POLYGON_KEY=
respectively (i don't knnow if polygone_name is needed or not)

Now instead of using yfinance and alpaca, I want to use polygon api for fetching the data, can you please modify the code accordingly?
- if I use yfinacne or aplaca for retrieving tickers for the universe that is fine

Polygon should be used for fetching all the data we use for calculating, and additionally see what more it can do

Also I would like ot have this as a dashboard too.

'''