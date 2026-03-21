"""
╔══════════════════════════════════════════════════════════════════════╗
║         NASDAQ POPULARITY-DRIVEN SCREENER  v3.0                     ║
║                                                                      ║
║  Universe built from REAL retail popularity signals:                 ║
║                                                                      ║
║  1. Finviz Most Active / Top Volume export  (no login, free)         ║
║  2. Yahoo Finance screener feeds  (most_actives, day_gainers,        ║
║         small_cap_gainers, growth_technology_stocks, etc.)           ║
║  3. Reddit WSB + r/stocks + r/investing mention scraper              ║
║         (free PRAW API — needs a Reddit app, setup below)            ║
║  4. Finviz Trending / News Heat  (scrape trending page)              ║
║  5. NASDAQ FTP full list  (fallback for broad coverage)              ║
║                                                                      ║
║  All sources are deduplicated + scored by POPULARITY RANK,           ║
║  then fed into the 3 strategy screeners.                             ║
║                                                                      ║
║  Install:                                                            ║
║    pip install yfinance pandas numpy requests rich pytz              ║
║               beautifulsoup4 lxml praw vaderSentiment               ║
╚══════════════════════════════════════════════════════════════════════╝

REDDIT SETUP (one-time, free):
  1. Go to https://www.reddit.com/prefs/apps
  2. Click "Create App" → choose "script"
  3. Name it anything, redirect URI = http://localhost:8080
  4. Copy your client_id (under app name) and client_secret
  5. Set them in the REDDIT CONFIG section below OR via env vars:
       export REDDIT_CLIENT_ID=xxxx
       export REDDIT_CLIENT_SECRET=xxxx

Usage:
  python nasdaq_screener_v3.py                    # all 7 strategies
  python nasdaq_screener_v3.py --strategy 1       # specific strategy (1-7)
  python nasdaq_screener_v3.py --overnight        # strategies 4,5,6 (night prep)
  python nasdaq_screener_v3.py --morning          # strategies 1,2,3 (premarket)
  python nasdaq_screener_v3.py --ten-am           # strategy 7 (10 AM ORB)
  python nasdaq_screener_v3.py --sources finviz yahoo reddit
  python nasdaq_screener_v3.py --max 300          # cap tickers to screen
  python nasdaq_screener_v3.py --export           # save CSV output
  python nasdaq_screener_v3.py --show-universe    # print full ranked universe
"""

import argparse
import io
import os
import re
import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz
import requests
import yfinance as yf
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

warnings.filterwarnings("ignore")
console = Console(width=None)   # None = auto-detect full terminal width, no artificial cap

# ──────────────────────────────────────────────
#  REDDIT CONFIG  (edit here or use env vars)
# ──────────────────────────────────────────────
REDDIT_CLIENT_ID     = os.environ.get("REDDIT_CLIENT_ID", "YOUR_CLIENT_ID_HERE")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "YOUR_SECRET_HERE")
REDDIT_USER_AGENT    = "stock_screener_v3 by /u/your_username"

# Subreddits to scrape for ticker mentions
REDDIT_SUBS = ["wallstreetbets", "stocks", "investing", "StockMarket", "pennystocks", "Daytrading"]

# Common false-positive tickers to ignore (words that match ticker patterns but aren't stocks)
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
}


# ──────────────────────────────────────────────
#  SOURCE 1: FINVIZ  (most active, top volume, gainers)
# ──────────────────────────────────────────────

def fetch_finviz_active(top_n: int = 300) -> dict:
    """
    Scrape Finviz screener HTML pages directly (their CSV export now requires login).
    Pulls most active, gainers, small cap, micro cap NASDAQ stocks.
    """
    from bs4 import BeautifulSoup
    results = {}
    sources = [
        ("exch_nasd&o=-volume",          "NASDAQ by Volume"),
        ("exch_nasd&cap_small&o=-volume","NASDAQ Small Cap Volume"),
        ("exch_nasd&o=-change",          "NASDAQ Gainers"),
        ("exch_nasd&cap_micro&o=-volume","NASDAQ Micro Cap"),
    ]
    # Use the screener HTML page instead of the broken export endpoint
    base_url = "https://finviz.com/screener.ashx?v=111&f={params}&r={row}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://finviz.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for params, label in sources:
        tickers_found = []
        try:
            # Finviz shows 20 rows per page; scrape first 5 pages = up to 100 tickers
            for page in range(5):
                row = page * 20 + 1
                url = base_url.format(params=params, row=row)
                r = requests.get(url, headers=headers, timeout=15)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "lxml")

                # Tickers are in <a> tags with class "screener-link-primary"
                for a in soup.select("a.screener-link-primary"):
                    t = a.text.strip().upper()
                    if 1 <= len(t) <= 5 and t.isalpha():
                        tickers_found.append(t)

                if len(tickers_found) >= top_n:
                    break
                time.sleep(0.4)  # polite scraping

            tickers_found = tickers_found[:top_n]
            for rank, ticker in enumerate(tickers_found):
                score = top_n - rank
                results[ticker] = results.get(ticker, 0) + score
            console.print(f"  [green]✔ Finviz {label}: {len(tickers_found)} tickers[/green]")
        except Exception as e:
            console.print(f"  [yellow]⚠ Finviz {label} failed: {e}[/yellow]")

    return results


def fetch_finviz_trending() -> dict:
    """
    Scrape Finviz news page for tickers mentioned in headlines.
    """
    results = {}
    try:
        from bs4 import BeautifulSoup
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        r = requests.get("https://finviz.com/news.ashx", headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "lxml")
        tickers_found = []
        # Try multiple selector patterns Finviz uses
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "quote.ashx?t=" in href:
                t = href.split("t=")[-1].split("&")[0].upper()
                if 1 <= len(t) <= 5 and t.isalpha():
                    tickers_found.append(t)
        freq = defaultdict(int)
        for t in tickers_found:
            freq[t] += 10
        results = dict(freq)
        console.print(f"  [green]✔ Finviz news trending: {len(results)} tickers[/green]")
    except Exception as e:
        console.print(f"  [yellow]⚠ Finviz trending scrape failed: {e}[/yellow]")
    return results


# ──────────────────────────────────────────────
#  SOURCE 2: YAHOO FINANCE SCREENER FEEDS
# ──────────────────────────────────────────────

YAHOO_SCREENS = [
    "most_actives",
    "day_gainers",
    "day_losers",           # useful for bounce plays
    "growth_technology_stocks",
    "small_cap_gainers",
    "undervalued_growth_stocks",
    "aggressive_small_caps",
    "high_yield_bond",
]

def fetch_yahoo_screens(top_n: int = 100) -> dict:
    """
    Pull from yfinance built-in screener feeds.
    yf.screen() now returns a dict like {'quotes': [...], 'total': N}
    — not a DataFrame directly. Handle both old and new API shapes.
    """
    results = {}
    for screen in YAHOO_SCREENS:
        try:
            raw = yf.screen(screen)

            # New yfinance API: returns dict with 'quotes' list
            if isinstance(raw, dict):
                quotes = raw.get("quotes", raw.get("body", []))
                if not quotes:
                    raise ValueError("empty quotes list")
                df = pd.DataFrame(quotes)
            elif isinstance(raw, pd.DataFrame):
                df = raw
            else:
                raise ValueError(f"unexpected type: {type(raw)}")

            # Normalise column name (symbol vs ticker)
            sym_col = next((c for c in df.columns if c.lower() in ("symbol", "ticker")), None)
            if sym_col is None:
                raise ValueError("no symbol column found")
            df = df.rename(columns={sym_col: "symbol"})

            # Filter to NASDAQ exchanges where available
            if "exchange" in df.columns:
                df = df[df["exchange"].isin(["NMS", "NGM", "NCM", "NasdaqGS", "NasdaqCM", "NasdaqGM"])]

            tickers = df["symbol"].dropna().str.strip().str.upper().tolist()[:top_n]
            weight = 50 if screen in ("most_actives", "day_gainers", "small_cap_gainers") else 30
            for rank, ticker in enumerate(tickers):
                score = weight + (top_n - rank)
                results[ticker] = results.get(ticker, 0) + score
            console.print(f"  [green]✔ Yahoo '{screen}': {len(tickers)} tickers[/green]")
        except Exception as e:
            console.print(f"  [yellow]⚠ Yahoo '{screen}' failed: {e}[/yellow]")
        time.sleep(0.2)
    return results


# ──────────────────────────────────────────────
#  SOURCE 3: REDDIT  (WSB + stocks subs)
# ──────────────────────────────────────────────

def fetch_reddit_mentions(hours_back: int = 24, post_limit: int = 500) -> dict:
    """
    Scrape Reddit for ticker mentions across WSB, r/stocks, r/investing etc.
    Weights by: mention_count * upvotes * sentiment_score.
    Returns {ticker: weighted_score}.
    """
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

        # Valid NASDAQ tickers (2–5 alpha chars, not in blacklist)
        ticker_pattern = re.compile(r'\b([A-Z]{2,5})\b')
        scores = defaultdict(float)
        mention_counts = defaultdict(int)

        cutoff = datetime.utcnow() - timedelta(hours=hours_back)

        for sub_name in REDDIT_SUBS:
            try:
                sub = reddit.subreddit(sub_name)
                # Get hot + new posts
                posts = list(sub.hot(limit=post_limit // 2)) + list(sub.new(limit=post_limit // 2))

                for post in posts:
                    # Skip old posts
                    post_time = datetime.utcfromtimestamp(post.created_utc)
                    if post_time < cutoff:
                        continue

                    text = f"{post.title} {post.selftext}"
                    tickers_found = ticker_pattern.findall(text.upper())
                    tickers_found = [t for t in tickers_found if t not in TICKER_BLACKLIST]

                    # Sentiment of post
                    sentiment = vader.polarity_scores(text)
                    compound = sentiment["compound"]  # -1 to +1

                    # Weight: upvotes * bullish_sentiment_bonus
                    upvotes = max(post.score, 1)
                    sentiment_multiplier = 1 + max(compound, 0)  # bullish = bonus, bearish = neutral

                    for ticker in set(tickers_found):  # dedupe within same post
                        mention_counts[ticker] += 1
                        scores[ticker] += upvotes * sentiment_multiplier

                console.print(f"  [green]✔ Reddit r/{sub_name}: scraped {len(posts)} posts[/green]")
            except Exception as e:
                console.print(f"  [yellow]⚠ Reddit r/{sub_name}: {e}[/yellow]")
            time.sleep(1)

        # Normalize and combine mention count + weighted score
        result = {}
        for ticker, score in scores.items():
            mentions = mention_counts[ticker]
            if mentions < 2:  # filter single-mention noise
                continue
            # Final score: log-weighted to prevent huge outliers
            import math
            result[ticker] = round(mentions * 10 + math.log1p(score) * 5, 1)

        console.print(f"  [green]✔ Reddit total: {len(result)} unique tickers with 2+ mentions[/green]")
        return result

    except Exception as e:
        console.print(f"  [red]✘ Reddit scraper error: {e}[/red]")
        return {}


# ──────────────────────────────────────────────
#  SOURCE 4: NASDAQ FTP FULL LIST  (fallback)
# ──────────────────────────────────────────────

def fetch_nasdaq_ftp() -> dict:
    """
    Pull NASDAQ full listed universe as a flat popularity dict.
    All tickers get equal weight of 1 (just ensures coverage).
    """
    url = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        lines = r.text.strip().split("\n")
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
            result[symbol] = 1  # base weight
        console.print(f"  [green]✔ NASDAQ FTP: {len(result)} tickers[/green]")
        return result
    except Exception as e:
        console.print(f"  [yellow]⚠ NASDAQ FTP failed: {e}[/yellow]")
        return {}


# ──────────────────────────────────────────────
#  UNIVERSE BUILDER  (combine all sources)
# ──────────────────────────────────────────────

def build_universe(sources: list, max_tickers: int = 500, show: bool = False) -> list:
    """
    Combine all sources into a single ranked list.
    Returns top max_tickers tickers sorted by combined popularity score.
    """
    console.print(Panel.fit("[bold]🌐 BUILDING POPULARITY UNIVERSE[/bold]", border_style="blue"))

    combined = defaultdict(float)

    if "finviz" in sources:
        console.print("[cyan]📊 Finviz active/volume/gainer feeds...[/cyan]")
        fv = fetch_finviz_active()
        if not fv:
            console.print("  [yellow]⚠ Finviz returned 0 tickers (market closed / blocked) — adding NASDAQ FTP fallback[/yellow]")
            fv = fetch_nasdaq_ftp()
        for t, s in fv.items():
            combined[t] += s * 1.2

        console.print("[cyan]📰 Finviz news trending...[/cyan]")
        for t, s in fetch_finviz_trending().items():
            combined[t] += s

    if "yahoo" in sources:
        console.print("[cyan]📈 Yahoo Finance screener feeds...[/cyan]")
        for t, s in fetch_yahoo_screens().items():
            combined[t] += s

    if "reddit" in sources:
        console.print("[cyan]🤖 Reddit WSB + stocks mention scraper...[/cyan]")
        for t, s in fetch_reddit_mentions().items():
            combined[t] += s * 2.0  # Reddit = strong retail signal, boost it

    if "nasdaq" in sources or not combined:
        console.print("[cyan]🗄  NASDAQ FTP full list (coverage fallback)...[/cyan]")
        for t, s in fetch_nasdaq_ftp().items():
            if t not in combined:  # only add if not already from better sources
                combined[t] += s

    # Sort by combined popularity score descending
    ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)
    ranked = [(t, s) for t, s in ranked if 1 <= len(t) <= 5 and t.isalpha()]
    top = ranked[:max_tickers]

    console.print(f"\n[bold green]Universe: {len(top):,} tickers (ranked by retail popularity)[/bold green]\n")

    if show:
        console.print("[bold]Top 50 Most Popular Tickers:[/bold]")
        for i, (ticker, score) in enumerate(top[:50], 1):
            bar = "█" * min(int(score / max(s for _, s in top[:50]) * 30), 30)
            console.print(f"  {i:3}. [cyan]{ticker:6}[/cyan] {bar} {score:.0f}")
        console.print()

    return [t for t, _ in top], dict(top)


# ──────────────────────────────────────────────
#  DATA FETCHER
# ──────────────────────────────────────────────

def fetch_price_data(tickers: list, period: str = "60d") -> dict:
    data_map = {}
    batch_size = 30
    batches = [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TaskProgressColumn(), console=console) as prog:
        task = prog.add_task(f"[cyan]Downloading {len(tickers)} tickers...", total=len(batches))
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

    console.print(f"[green]✔ Price data: {len(data_map):,} tickers loaded[/green]")
    return data_map


def fetch_fundamentals(tickers: list, limit: int = 300) -> dict:
    info_map = {}
    sample = tickers[:limit]
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
#  STRATEGY 1: HIGH RVOL / CATALYST
# ──────────────────────────────────────────────

def s1_catalyst(data_map, info_map, pop_scores):
    results = []
    for ticker, df in data_map.items():
        try:
            if len(df) < 20:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price = float(c.iloc[-1])
            if not (0.50 <= price <= 10.0):
                continue

            rv       = rvol(v)
            r        = rsi(c)
            a        = atr(h, l, c)
            atp      = (a / price) * 100
            day_chg  = (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
            v5avg    = v.iloc[-6:-1].mean()
            vspike   = float(v.iloc[-1]) / v5avg if v5avg > 0 else 0
            h20      = float(h.iloc[-21:-1].max())
            near_brk = price >= h20 * 0.97
            mom3     = (float(c.iloc[-1]) - float(c.iloc[-4])) / float(c.iloc[-4]) * 100
            body     = abs(float(c.iloc[-1]) - float(o.iloc[-1]))
            rng      = float(h.iloc[-1]) - float(l.iloc[-1])
            body_r   = body / rng if rng > 0 else 0
            pop      = pop_scores.get(ticker, 0)

            sigs = {
                "rvol":         1.0 if rv >= 2.5 else (0.5 if rv >= 1.5 else 0.0),
                "price_range":  1.0,
                "rsi_zone":     1.0 if 42 <= r <= 72 else 0.0,
                "big_move":     1.0 if abs(day_chg) >= 5 else (0.5 if abs(day_chg) >= 3 else 0.0),
                "breakout":     1.0 if near_brk else 0.0,
                "vol_spike":    1.0 if vspike >= 3 else (0.5 if vspike >= 2 else 0.0),
                "momentum":     1.0 if mom3 > 0 else 0.0,
                "high_atr":     1.0 if atp >= 5 else (0.5 if atp >= 3 else 0.0),
                "bull_candle":  1.0 if (body_r > 0.55 and c.iloc[-1] > o.iloc[-1]) else 0.0,
                "popular":      1.0 if pop > 100 else (0.5 if pop > 20 else 0.0),
            }

            conf = sig_score(sigs)
            if conf < 40:
                continue

            target = round(price * (1 + atp / 100 * 2.2), 2)
            stop   = round(price * (1 - atp / 100 * 0.9), 2)
            rr     = round((target - price) / (price - stop), 2) if price > stop else 0

            info   = info_map.get(ticker, {})
            cap    = info.get("marketCap", 0)
            cap_lbl= "Micro" if cap < 300e6 else ("Small" if cap < 2e9 else "Mid")

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}", RVOL=f"{rv}x",
                DayChg=f"{day_chg:+.1f}%", RSI=round(r, 1),
                ATR_pct=f"{atp:.1f}%", Cap=cap_lbl,
                Breakout="✅" if near_brk else "—",
                PopScore=round(pop), Target=f"${target}",
                Stop=f"${stop}", RR=f"1:{rr}",
                Confidence=conf, _score=conf,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 2: MOMENTUM SWING
# ──────────────────────────────────────────────

def s2_swing(data_map, info_map, pop_scores):
    results = []
    for ticker, df in data_map.items():
        try:
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

            info     = info_map.get(ticker, {})
            rev_g    = info.get("revenueGrowth", None)
            fwd_eps  = info.get("forwardEps", None)

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
                PopScore=round(pop),
                Target=f"${target}(+{tgt_pct:.0f}%)",
                Stop=f"${stop}", Timeframe=tf,
                Confidence=conf, _score=conf,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 3: GAP & BREAKOUT
# ──────────────────────────────────────────────

def s3_breakout(data_map, info_map, pop_scores):
    results = []
    for ticker, df in data_map.items():
        try:
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
                "gap_up":      1.0 if gap_pct >= 3 else (0.5 if gap_pct >= 1 else 0.0),
                "h20_break":   1.0 if price >= h20 * 0.98 else 0.0,
                "h52_break":   1.0 if price >= h52 * 0.97 else 0.0,
                "rvol":        1.0 if rv >= 2.5 else (0.5 if rv >= 1.5 else 0.0),
                "flat_base":   1.0 if flat else 0.0,
                "vol_contract":1.0 if vcon else 0.0,
                "rsi_ok":      1.0 if r < 76 else 0.0,
                "bull_candle": 1.0 if (body_r > 0.6 and c.iloc[-1] > o.iloc[-1]) else 0.0,
                "day_chg":     1.0 if day_chg > 2 else (0.5 if day_chg > 0 else 0.0),
                "atr_expand":  1.0 if atp > 3 else 0.0,
                "popular":     1.0 if pop > 80 else (0.5 if pop > 20 else 0.0),
            }

            conf   = sig_score(sigs)
            if conf < 42:
                continue

            tgt_pct = gap_pct * 1.5 + 5
            target  = round(price * (1 + tgt_pct / 100), 2)
            stop    = round(price * 0.94, 2)
            rr      = round((target - price) / (price - stop), 2) if price > stop else 0

            info    = info_map.get(ticker, {})
            cap     = info.get("marketCap", 0)
            cap_lbl = "Micro" if cap < 300e6 else ("Small" if cap < 2e9 else ("Mid" if cap < 10e9 else "Large"))

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}",
                Gap=f"{gap_pct:+.1f}%", RVOL=f"{rv}x",
                RSI=round(r, 1), DayChg=f"{day_chg:+.1f}%",
                FlatBase="✅" if flat else "—",
                H52Break="✅" if price >= h52*0.97 else "—",
                Cap=cap_lbl, PopScore=round(pop),
                Target=f"${target}(+{tgt_pct:.0f}%)",
                Stop=f"${stop}", RR=f"1:{rr}",
                Confidence=conf, _score=conf,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 4: EARNINGS VOLATILITY SETUP  🌙
#  OVERNIGHT — run 8 PM–midnight night before
#  Best days: Sun night, Mon–Thu nights
# ──────────────────────────────────────────────

def s4_earnings_setup(data_map, info_map, pop_scores):
    """
    Finds stocks with earnings TOMORROW that have strong pre-earnings setups.
    Two sub-plays:
      A) Momentum into earnings — strong trend, buy before the pop
      B) IV crush play — stock has been quiet, sell the spike after announcement
    Flags which type each candidate is.
    """
    results = []
    for ticker, df in data_map.items():
        try:
            if len(df) < 20:
                continue
            c, v, h, l = df["Close"], df["Volume"], df["High"], df["Low"]
            price = float(c.iloc[-1])
            if price < 1:
                continue

            # Check earnings date via yfinance calendar
            info = info_map.get(ticker, {})
            earn_date = None
            try:
                t_obj = yf.Ticker(ticker)
                cal = t_obj.calendar
                if cal is not None and not cal.empty:
                    # calendar returns a df with 'Earnings Date' column or similar
                    if "Earnings Date" in cal.columns:
                        earn_date = pd.to_datetime(cal["Earnings Date"].iloc[0])
                    elif hasattr(cal, "T") and "Earnings Date" in cal.T.columns:
                        earn_date = pd.to_datetime(cal.T["Earnings Date"].iloc[0])
            except Exception:
                pass

            # Only care about earnings within next 1–3 days
            if earn_date is None:
                continue
            et_tz   = pytz.timezone("America/New_York")
            now_et  = datetime.now(et_tz)
            earn_dt = earn_date if earn_date.tzinfo else et_tz.localize(earn_date)
            days_to = (earn_dt.date() - now_et.date()).days
            if not (0 <= days_to <= 3):
                continue

            # Technical context
            e20     = float(ema(c, 20).iloc[-1])
            e50     = float(ema(c, 50).iloc[-1])
            r       = rsi(c)
            a       = atr(h, l, c)
            atp     = (a / price) * 100
            rv      = rvol(v)
            m1m     = (float(c.iloc[-1]) - float(c.iloc[-22])) / float(c.iloc[-22]) * 100 if len(c) >= 22 else 0
            h52     = float(h.rolling(252).max().iloc[-1]) if len(h) >= 252 else float(h.max())

            # Pre-earnings range compression (last 5 days tight = IV building)
            range_5d = (float(h.iloc[-5:].max()) - float(l.iloc[-5:].min())) / price * 100
            compressed = range_5d < 6.0  # tight = IV about to spike

            # Fundamentals
            eps_surprise = info.get("earningsQuarterlyGrowth", None)
            fwd_eps      = info.get("forwardEps", None)
            rev_growth   = info.get("revenueGrowth", None)
            beat_history = 1.0 if (eps_surprise and eps_surprise > 0.05) else 0.0

            # Play type
            trending_into = price > e20 > e50 and r > 52 and m1m > 5
            play_type = "📈 Momentum into earnings" if trending_into else ("📉 IV crush / sell spike" if compressed else "⚠️ Speculative")

            sigs = {
                "earnings_soon":      1.0,
                "above_e20":          1.0 if price > e20 else 0.0,
                "rsi_healthy":        1.0 if 45 <= r <= 72 else 0.0,
                "positive_1m_mom":    1.0 if m1m > 3 else 0.0,
                "near_52w_high":      1.0 if price >= h52 * 0.88 else 0.0,
                "compressed_range":   1.0 if compressed else 0.0,
                "beat_history":       beat_history,
                "positive_fwd_eps":   1.0 if (fwd_eps and fwd_eps > 0) else 0.0,
                "rev_growth":         1.0 if (rev_growth and rev_growth > 0.05) else 0.0,
                "popular":            1.0 if pop_scores.get(ticker, 0) > 30 else 0.0,
            }

            conf = sig_score(sigs)
            if conf < 40:
                continue

            # Target: earnings pop estimate based on ATR
            exp_move = round(atp * 2.5, 1)  # expected move % = ~2.5x ATR
            target   = round(price * (1 + exp_move / 100), 2)
            stop     = round(price * (1 - atp / 100 * 1.2), 2)

            results.append(dict(
                Ticker=ticker,
                Price=f"${price:.2f}",
                EarnIn=f"{days_to}d",
                PlayType=play_type,
                RSI=round(r, 1),
                Mom1M=f"{m1m:+.1f}%",
                Range5D=f"{range_5d:.1f}%",
                RVOL=f"{rv}x",
                ExpMove=f"±{exp_move}%",
                Target=f"${target}",
                Stop=f"${stop}",
                PopScore=round(pop_scores.get(ticker, 0)),
                Confidence=conf,
                _score=conf,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 5: OVERSOLD REVERSAL HUNTER  🌙
#  OVERNIGHT — run 8 PM–midnight
#  Best days: After 2+ red days in a row, Mon/Tue nights
# ──────────────────────────────────────────────

def s5_oversold_reversal(data_map, info_map, pop_scores):
    """
    Finds stocks that have been beaten down hard (RSI < 32, multi-day selloff)
    but show early signs of reversal: hammer candles, volume drying up,
    holding above a key support level. These set up well for a bounce next morning.

    Not bottom-fishing trash — needs a real structure to bounce from.
    """
    results = []
    for ticker, df in data_map.items():
        try:
            if len(df) < 30:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price = float(c.iloc[-1])
            if price < 2:
                continue

            r       = rsi(c)
            if r > 35:  # must be genuinely oversold
                continue

            a       = atr(h, l, c)
            atp     = (a / price) * 100
            rv      = rvol(v)
            e50     = float(ema(c, 50).iloc[-1])
            e200    = float(ema(c, 200).iloc[-1]) if len(c) >= 200 else None

            # Consecutive red days (selloff streak)
            last5_chg = [float(c.iloc[i] - c.iloc[i-1]) for i in range(-5, 0)]
            red_streak = sum(1 for x in last5_chg if x < 0)

            # 5-day loss magnitude
            loss_5d = (float(c.iloc[-1]) - float(c.iloc[-6])) / float(c.iloc[-6]) * 100

            # Hammer / doji candle on last day (potential reversal candle)
            body    = abs(float(c.iloc[-1]) - float(o.iloc[-1]))
            rng_c   = float(h.iloc[-1]) - float(l.iloc[-1])
            lower_w = float(o.iloc[-1] if c.iloc[-1] > o.iloc[-1] else c.iloc[-1]) - float(l.iloc[-1])
            body_r  = body / rng_c if rng_c > 0 else 0
            hammer  = lower_w > body * 2 and body_r < 0.4  # long lower wick = buyers stepping in

            # Volume drying up (selling exhaustion)
            vol_dry = float(v.iloc[-1]) < float(v.iloc[-6:-1].mean()) * 0.75

            # Still above 200 EMA (not broken beyond repair)
            above_200 = (e200 is not None and price > e200 * 0.92)

            # Distance from 52-week low (not making new lows — holding support)
            l52 = float(l.rolling(252).min().iloc[-1]) if len(l) >= 252 else float(l.min())
            pct_off_low = (price - l52) / l52 * 100

            # Fundamentals
            info     = info_map.get(ticker, {})
            fwd_eps  = info.get("forwardEps", None)
            cap      = info.get("marketCap", 0)
            cap_lbl  = "Micro" if cap < 300e6 else ("Small" if cap < 2e9 else ("Mid" if cap < 10e9 else "Large"))

            sigs = {
                "oversold_rsi":     1.0 if r < 28 else 0.7,
                "red_streak":       1.0 if red_streak >= 3 else (0.5 if red_streak >= 2 else 0.0),
                "hammer_candle":    1.0 if hammer else 0.0,
                "vol_drying":       1.0 if vol_dry else 0.0,
                "above_200":        1.0 if above_200 else 0.0,
                "holding_support":  1.0 if pct_off_low > 5 else 0.0,  # not at 52w low
                "big_selloff":      1.0 if loss_5d < -10 else (0.5 if loss_5d < -6 else 0.0),
                "positive_eps":     1.0 if (fwd_eps and fwd_eps > 0) else 0.0,
                "popular":          1.0 if pop_scores.get(ticker, 0) > 20 else 0.0,
            }

            conf = sig_score(sigs)
            if conf < 42:
                continue

            # Bounce target: back to 20 EMA or +ATR*1.5
            e20       = float(ema(c, 20).iloc[-1])
            target    = round(max(e20, price * (1 + atp / 100 * 1.5)), 2)
            stop      = round(price * (1 - atp / 100 * 0.8), 2)
            bounce_pct = round((target - price) / price * 100, 1)
            rr        = round((target - price) / (price - stop), 2) if price > stop else 0

            results.append(dict(
                Ticker=ticker,
                Price=f"${price:.2f}",
                RSI=round(r, 1),
                Loss5D=f"{loss_5d:.1f}%",
                RedDays=red_streak,
                Hammer="✅" if hammer else "—",
                VolDry="✅" if vol_dry else "—",
                Cap=cap_lbl,
                Target=f"${target}(+{bounce_pct}%)",
                Stop=f"${stop}",
                RR=f"1:{rr}",
                PopScore=round(pop_scores.get(ticker, 0)),
                Confidence=conf,
                _score=conf,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 6: SECTOR ROTATION TRACKER  🌙
#  OVERNIGHT — run 8 PM–midnight
#  Best days: Any night, esp. after macro events
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
    """
    Identifies which sectors are getting money RIGHT NOW (last 5 days),
    then finds the top individual stock setups within those hot sectors.
    Money rotates — if biotech ETF just broke out, individual biotech names follow.
    """
    # Step 1: Score each sector ETF by recent momentum
    sector_scores = {}
    sector_data   = {}
    for sector_name, etf_ticker in SECTOR_ETFS.items():
        try:
            etf_df = yf.download(etf_ticker, period="30d", interval="1d",
                                  auto_adjust=True, progress=False)
            if etf_df is None or len(etf_df) < 10:
                continue
            ec = etf_df["Close"]
            ev = etf_df["Volume"]
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

    # Step 2: Print sector leaderboard as part of results
    hot_sectors = sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)
    top_sectors = [s for s, _ in hot_sectors[:4]]  # top 4 hot sectors

    # Step 3: Map tickers to their sector via yfinance info
    results = []
    sector_leaderboard = []
    for sector_name, score in hot_sectors:
        d = sector_data.get(sector_name, {})
        sector_leaderboard.append(dict(
            Sector=sector_name,
            ETF=d.get("etf", ""),
            Mom5D=f"{d.get('m5d', 0):+.1f}%",
            Mom20D=f"{d.get('m20d', 0):+.1f}%",
            RVOL=f"{d.get('rvol', 0)}x",
            RSI=d.get("rsi", 0),
            HeatScore=score,
            Trend="🔥 HOT" if score > 15 else ("📈 Warm" if score > 5 else ("❄️ Cold" if score < -5 else "Neutral")),
        ))

    # Step 4: Find stocks in hot sectors with good setups
    for ticker, df in data_map.items():
        try:
            info   = info_map.get(ticker, {})
            sector = info.get("sector", "")
            if not sector:
                continue

            # Map yfinance sector name to our sector list
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
                "hot_sector":    1.0 if s_score > 15 else (0.7 if s_score > 5 else 0.3),
                "above_e20":     1.0 if price > e20 else 0.0,
                "above_e50":     1.0 if price > e50 else 0.0,
                "rsi_zone":      1.0 if 48 <= r <= 70 else 0.0,
                "stock_5d_mom":  1.0 if m5d > 3 else (0.5 if m5d > 0 else 0.0),
                "rvol_confirm":  1.0 if rv >= 1.5 else 0.0,
                "near_52h":      1.0 if price >= h52 * 0.90 else 0.0,
                "popular":       1.0 if pop_scores.get(ticker, 0) > 20 else 0.0,
            }

            conf = sig_score(sigs)
            if conf < 45:
                continue

            target  = round(price * (1 + atp / 100 * 2), 2)
            stop    = round(max(e50, price * 0.92), 2)
            tgt_pct = round((target - price) / price * 100, 1)

            results.append(dict(
                Ticker=ticker,
                Sector=mapped,
                SectorHeat=f"{s_score:+.0f}",
                Price=f"${price:.2f}",
                RSI=round(r, 1),
                Mom5D=f"{m5d:+.1f}%",
                Mom20D=f"{m20d:+.1f}%",
                RVOL=f"{rv}x",
                Target=f"${target}(+{tgt_pct}%)",
                Stop=f"${stop}",
                PopScore=round(pop_scores.get(ticker, 0)),
                Confidence=conf,
                _score=conf + s_score * 0.3,  # boost by sector heat
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return sector_leaderboard, results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 7: OPENING RANGE BREAKOUT (ORB)  ⏰
#  RUN AT 10:00–10:30 AM ET
#  The open is chaotic — by 10 AM direction is set
#  Best days: Tue, Wed, Thu (cleanest trends)
# ──────────────────────────────────────────────

def s7_orb(data_map, info_map, pop_scores):
    """
    Opening Range Breakout — the most reliable intraday pattern.

    By 10 AM the first 30-min candle high/low is established.
    Stocks breaking ABOVE that high with volume = strong bullish signal.
    Stocks holding above yesterday's close AND above open = momentum confirmed.

    Since we only have daily OHLCV (not intraday), we approximate ORB using:
      - Today's open vs prior close (gap direction)
      - Whether price is holding above open (bull) or below (bear)
      - Volume coming in heavy confirming the direction
      - RSI and EMA alignment confirming it's not a false move
    For real intraday ORB you'd switch to interval='5m' on the day itself.
    """
    results = []
    for ticker, df in data_map.items():
        try:
            if len(df) < 21:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price    = float(c.iloc[-1])
            if price < 1:
                continue

            today_o  = float(o.iloc[-1])
            today_h  = float(h.iloc[-1])
            today_l  = float(l.iloc[-1])
            prev_c   = float(c.iloc[-2])
            prev_h   = float(h.iloc[-2])

            # Gap direction
            gap_pct  = (today_o - prev_c) / prev_c * 100
            gap_up   = gap_pct > 0.5

            # Price holding above open = bullish after 30 min
            above_open = price > today_o

            # ORB breakout proxy: today already taking out yesterday's high
            orb_break = price >= prev_h * 0.99

            # Strong open: price in top 30% of today's range
            rng_c    = today_h - today_l
            pos_in_range = (price - today_l) / rng_c if rng_c > 0 else 0.5
            strong_open  = pos_in_range > 0.65

            rv       = rvol(v)
            r        = rsi(c)
            a        = atr(h, l, c)
            atp      = (a / price) * 100
            e20      = float(ema(c, 20).iloc[-1])
            e50      = float(ema(c, 50).iloc[-1])
            day_chg  = (price - prev_c) / prev_c * 100

            # Volume surging vs yesterday (confirms direction)
            vol_vs_yday = float(v.iloc[-1]) / float(v.iloc[-2]) if float(v.iloc[-2]) > 0 else 1.0

            # Intraday trend alignment: open > prior close AND current > open
            clean_trend = gap_up and above_open and strong_open

            info     = info_map.get(ticker, {})
            cap      = info.get("marketCap", 0)
            cap_lbl  = "Micro" if cap < 300e6 else ("Small" if cap < 2e9 else ("Mid" if cap < 10e9 else "Large"))

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

            # ORB target: project the opening range size above the breakout
            orb_size = today_h - today_l
            target   = round(today_h + orb_size, 2)         # classic ORB 1:1 extension
            target2  = round(today_h + orb_size * 1.5, 2)   # extended target
            stop     = round(today_o * 0.985, 2)            # stop just under open
            rr       = round((target - price) / (price - stop), 2) if price > stop else 0

            results.append(dict(
                Ticker=ticker,
                Price=f"${price:.2f}",
                Gap=f"{gap_pct:+.1f}%",
                DayChg=f"{day_chg:+.1f}%",
                PosInRange=f"{pos_in_range*100:.0f}%",
                RVOL=f"{rv}x",
                VolVsYday=f"{vol_vs_yday:.1f}x",
                RSI=round(r, 1),
                Cap=cap_lbl,
                OrbBreak="✅" if orb_break else "—",
                Target1=f"${target}",
                Target2=f"${target2}",
                Stop=f"${stop}",
                RR=f"1:{rr}",
                PopScore=round(pop_scores.get(ticker, 0)),
                Confidence=conf,
                _score=conf,
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

    # Column min-widths so nothing gets squeezed or truncated
    col_widths = {
        "Ticker":     7,  "Price":    9,  "RVOL":     7,  "DayChg":   8,
        "RSI":        6,  "ATR_pct":  8,  "Cap":      6,  "Breakout": 9,
        "PopScore":   9,  "Target":  14,  "Stop":    10,  "RR":       7,
        "Confidence":11,  "vsEMA20":  9,  "vsEMA50":  9,  "Mom1M":    8,
        "Mom3M":      8,  "UpDnVol":  9,  "HH_HL":    7,  "Timeframe":10,
        "Gap":        7,  "FlatBase": 9,  "H52Break": 10,
    }

    t = Table(
        box=box.SIMPLE_HEAVY,
        header_style=f"bold {color}",
        show_lines=True,
        expand=True,
    )
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
    ap.add_argument("--strategy",      type=int, choices=[1,2,3,4,5,6,7],
                    help="Run a single strategy (1–7)")
    ap.add_argument("--overnight",     action="store_true",
                    help="Run overnight prep strategies 4, 5, 6")
    ap.add_argument("--morning",       action="store_true",
                    help="Run morning strategies 1, 2, 3")
    ap.add_argument("--ten-am",        action="store_true",
                    help="Run 10 AM ORB strategy 7")
    ap.add_argument("--sources",       nargs="+",
                    default=["finviz", "yahoo", "reddit"],
                    choices=["finviz", "yahoo", "reddit", "nasdaq"])
    ap.add_argument("--max",           type=int, default=400)
    ap.add_argument("--export",        action="store_true")
    ap.add_argument("--show-universe", action="store_true")
    args = ap.parse_args()

    console.print(Panel.fit(
        "[bold white]NASDAQ POPULARITY SCREENER  v4.0[/bold white]\n"
        "[dim]Finviz + Yahoo + Reddit → popularity-ranked universe → 7 strategies[/dim]\n"
        "[dim]Strategies 1–3: Premarket/Open  |  4–6: Overnight Prep  |  7: 10 AM ORB[/dim]",
        border_style="bright_blue", padding=(1, 4)))
    timing_banner()

    # Determine which strategies to run
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

    # 1. Build universe
    tickers, pop_scores = build_universe(
        sources=args.sources,
        max_tickers=args.max,
        show=args.show_universe,
    )

    # 2. Price data
    data_map = fetch_price_data(tickers)

    # 3. Fundamentals
    info_map = fetch_fundamentals(list(data_map.keys()))
    console.print()

    exports = {}

    # ── Morning strategies ──
    if 1 in run:
        console.print("[bold]🔥 Strategy 1: HIGH RVOL / CATALYST SCANNER[/bold]")
        r1 = s1_catalyst(data_map, info_map, pop_scores)
        display("STRATEGY 1 — CATALYST / HIGH RVOL",
                "Price <$10 | RVOL >2x | Explosive intraday | Best: Premarket 6–9:30 AM",
                r1, "red")
        exports["s1_catalyst"] = r1

    if 2 in run:
        console.print("[bold]📈 Strategy 2: MOMENTUM SWING TRADER[/bold]")
        r2 = s2_swing(data_map, info_map, pop_scores)
        display("STRATEGY 2 — MOMENTUM SWING",
                "EMA stacked | RSI 50–68 | HH+HL | Target 15–35% over weeks",
                r2, "green")
        exports["s2_swing"] = r2

    if 3 in run:
        console.print("[bold]💥 Strategy 3: GAP & BREAKOUT SCANNER[/bold]")
        r3 = s3_breakout(data_map, info_map, pop_scores)
        display("STRATEGY 3 — GAP & BREAKOUT",
                "Gap up | Flat base break | Volume confirm | Best: Market Open",
                r3, "yellow")
        exports["s3_breakout"] = r3

    # ── Overnight strategies ──
    if 4 in run:
        console.print("[bold]🌙 Strategy 4: EARNINGS VOLATILITY SETUP[/bold]")
        console.print("[dim]  Best run: 8 PM–midnight the night before earnings[/dim]")
        r4 = s4_earnings_setup(data_map, info_map, pop_scores)
        display("STRATEGY 4 — EARNINGS VOLATILITY SETUP",
                "Earnings in 1–3 days | IV setup | Momentum into catalyst | Run: Night before",
                r4, "magenta")
        exports["s4_earnings"] = r4

    if 5 in run:
        console.print("[bold]🌙 Strategy 5: OVERSOLD REVERSAL HUNTER[/bold]")
        console.print("[dim]  Best run: 8 PM–midnight after 2+ red days | Best days: Mon/Tue nights[/dim]")
        r5 = s5_oversold_reversal(data_map, info_map, pop_scores)
        display("STRATEGY 5 — OVERSOLD REVERSAL HUNTER",
                "RSI <32 | Hammer candle | Volume drying | Bounce to EMA | Run: Night before",
                r5, "cyan")
        exports["s5_oversold"] = r5

    if 6 in run:
        console.print("[bold]🌙 Strategy 6: SECTOR ROTATION TRACKER[/bold]")
        console.print("[dim]  Best run: 8 PM–midnight | Any night, esp. after macro events[/dim]")
        result6 = s6_sector_rotation(data_map, info_map, pop_scores)
        if result6 and len(result6) == 2:
            sector_lb, r6 = result6
            # Print sector leaderboard first
            display("SECTOR HEAT MAP — Which sectors have money flowing in",
                    "Sorted by momentum score — top 4 sectors feed stock picks below",
                    sector_lb, "bright_magenta")
            display("STRATEGY 6 — TOP STOCKS IN HOT SECTORS",
                    "Stocks inside the strongest rotating sectors | EMA aligned | Run: Night before",
                    r6, "bright_magenta")
            exports["s6_sectors"]       = sector_lb
            exports["s6_sector_stocks"] = r6

    # ── 10 AM strategy ──
    if 7 in run:
        console.print("[bold]⏰ Strategy 7: OPENING RANGE BREAKOUT (ORB) — 10 AM[/bold]")
        console.print("[dim]  Best run: 10:00–10:30 AM ET | Best days: Tue, Wed, Thu[/dim]")
        r7 = s7_orb(data_map, info_map, pop_scores)
        display("STRATEGY 7 — OPENING RANGE BREAKOUT (10 AM)",
                "Gap held | Above open | Volume surging | First 30-min high broken | Run: 10 AM",
                r7, "bright_cyan")
        exports["s7_orb"] = r7

    # ── Export ──
    if args.export:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        for name, data in exports.items():
            if data:
                fname = f"{name}_{ts}.csv"
                pd.DataFrame(data).to_csv(fname, index=False)
                console.print(f"[green]💾 {fname}[/green]")

    # ── Strategy guide reminder ──
    console.print(Panel(
        "[bold]WHEN TO RUN WHAT[/bold]\n\n"
        "[cyan]8 PM – Midnight (night before)[/cyan]\n"
        "  --overnight  →  Strategies 4 (Earnings), 5 (Oversold), 6 (Sector Rotation)\n"
        "  Plan your watchlist, set alerts, know your levels before you sleep.\n\n"
        "[green]6:00 – 9:30 AM  (premarket)[/green]\n"
        "  --morning    →  Strategies 1 (Catalyst/RVOL), 2 (Swing), 3 (Gap/Breakout)\n"
        "  Catch gap-ups, news plays, premarket volume before the crowd.\n\n"
        "[yellow]10:00 – 10:30 AM  (after open dust settles)[/yellow]\n"
        "  --ten-am     →  Strategy 7 (ORB)\n"
        "  First 30 min chaos is done. Real direction is set. Break the range.\n\n"
        "[dim]Best days: Tue/Wed/Thu for cleanest setups. Mon for gap plays. Thu for earnings.[/dim]",
        border_style="blue", padding=(0, 2)
    ))

    console.print(Panel.fit(
        "[bold yellow]⚠  DISCLAIMER[/bold yellow]\n"
        "[dim]Educational use only. Not financial advice. Trade at your own risk.[/dim]",
        border_style="yellow"))


if __name__ == "__main__":
    main()