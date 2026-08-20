"""Headless scan engine — the data + strategy phases with no I/O opinions.

`pipeline.main()` (CLI) and `webapp/` (web UI) both call `run_scan()`, so there
is exactly one copy of the run sequence. The only difference between the two is
what they do with the returned rows: the CLI prints rich tables, the web app
writes them to SQLite.

Progress is reported through a `progress(phase, pct, detail)` callback rather
than printed, so the caller decides whether it becomes console output, a
progress bar, or a job-status field.
"""

import time

import yfinance as yf

from .sources import build_universe
from .marketdata import (fetch_price_data, fetch_fundamentals, enrich_short_interest,
                         fetch_polygon_premarket, fetch_polygon_intraday, fetch_polygon_news)
from .strategies import (DEFAULT_PRICE_BANDS, industry_heat, s1_catalyst, s2_swing,
                         s3_breakout, s4_quality_compounder, s5_oversold_reversal,
                         s6_sector_rotation, s7_orb, s8_power_swing)


# ──────────────────────────────────────────────
#  STRATEGY REGISTRY
#  The single source of truth for names, colors and how each strategy is
#  invoked. Previously duplicated between pipeline.main() and dashboard.py.
# ──────────────────────────────────────────────

def _band(c, key):
    """Price band for this strategy: S1 has its own, 2-8 share the other one.

    A None band means "use the strategy's built-in default" — for S1 that is
    the $0.50-$10 catalyst window, for the rest their own per-strategy floors.
    """
    return c.get("s1_band") if key == 1 else c.get("other_band")


def _run_s1(c, relax):
    return s1_catalyst(c["data_map"], c["info_map"], c["pop_scores"],
                       bench_c=c["bench_c"], premarket=c["premarket"],
                       intraday=c["intraday"], news=c["news"], relax=relax,
                       price_band=_band(c, 1))

def _run_s2(c, relax):
    return s2_swing(c["data_map"], c["info_map"], c["pop_scores"],
                    bench_c=c["bench_c"], premarket=c["premarket"], relax=relax,
                    price_band=_band(c, 2))

def _run_s3(c, relax):
    return s3_breakout(c["data_map"], c["info_map"], c["pop_scores"],
                       bench_c=c["bench_c"], premarket=c["premarket"], relax=relax,
                       price_band=_band(c, 3))

def _run_s4(c, relax):
    return s4_quality_compounder(c["data_map"], c["info_map"], c["pop_scores"],
                                 bench_c=c["bench_c"], relax=relax,
                                 price_band=_band(c, 4))

def _run_s5(c, relax):
    return s5_oversold_reversal(c["data_map"], c["info_map"], c["pop_scores"], relax=relax,
                                price_band=_band(c, 5))

def _run_s6(c, relax):
    # s6 returns (sector_leaderboard, stock_rows). run_scan() splits the tuple
    # into two export keys; callers that only want the stocks get them here.
    out = s6_sector_rotation(c["data_map"], c["info_map"], c["pop_scores"], relax=relax,
                             price_band=_band(c, 6))
    if isinstance(out, tuple) and len(out) == 2:
        return out[1]
    return []

def _run_s7(c, relax):
    return s7_orb(c["data_map"], c["info_map"], c["pop_scores"],
                  bench_c=c["bench_c"], relax=relax, price_band=_band(c, 7))

def _run_s8(c, relax):
    return s8_power_swing(c["data_map"], c["info_map"], c["pop_scores"],
                          bench_c=c["bench_c"], relax=relax, price_band=_band(c, 8))


STRATEGIES = {
    1: {
        "export": "s1_catalyst",
        "runner": _run_s1,
        "color": "red",
        "label": "Catalyst / High RVOL",
        "short": "Catalyst",
        "subtitle": "Price <$10 | RVOL >2x | explosive intraday | premarket gap+volume",
        "cli_title": "STRATEGY 1 — CATALYST / HIGH RVOL",
        "cli_subtitle": (
            "Price = prior close (setup anchor) | LivePrice = EXACT live pre/post quote, session-stamped (PRE/LIVE/AH) with today's % | "
            "LiveVol = shares traded so far this session + % of a normal full day | DayChg = today's change so far | "
            "Score 1–10 = should you HOP IN RIGHT NOW (10=best; chasing parabolic/fade capped low) | "
            "Strength 1–10 = raw move horsepower (high Strength + low Score = wait for pullback) | "
            "Type = Multi-day / All-day / Premarket / Postmarket / Mix | "
            "News = fresh catalyst (count·age); 'none ⚠' = unexplained spike"),
        "badge": "Score",
        # Fields the mobile card shows before you tap to expand.
        "card": ["Price", "LivePrice", "Strength", "Verdict", "News"],
    },
    2: {
        "export": "s2_swing",
        "runner": _run_s2,
        "color": "green",
        "label": "Momentum Swing",
        "short": "Swing",
        "subtitle": "EMA stacked | RSI 50–68 | higher highs + higher lows",
        "cli_title": "STRATEGY 2 — MOMENTUM SWING",
        "cli_subtitle": "EMA stacked | RSI 50–68 | HH+HL | SetupQ favors Stage2 + RS leaders + accumulation",
        "badge": "Confidence",
        "card": ["Price", "Target", "Stop", "SetupQ", "Phase"],
    },
    3: {
        "export": "s3_breakout",
        "runner": _run_s3,
        "color": "yellow",
        "label": "Gap & Breakout",
        "short": "Breakout",
        "subtitle": "Premarket + daily gap | flat-base break | volume confirm",
        "cli_title": "STRATEGY 3 — GAP & BREAKOUT",
        "cli_subtitle": "PM gap + daily gap | Flat base break | Volume confirm | Phase tag flags Base/Breakout/Continuation/Extended",
        "badge": "Confidence",
        "card": ["Price", "Target", "Stop", "PM_Gap", "Phase"],
    },
    4: {
        "export": "s4_quality",
        "runner": _run_s4,
        "color": "magenta",
        "label": "Quality Growth (4–6 Month Hold)",
        "short": "Quality",
        "subtitle": ("Stage-2 uptrend + momentum | strong growth, decent (not strict) fundamentals | "
                     "analyst + insider/institutional backing | tradeable volatility welcome"),
        "cli_title": "STRATEGY 4 — QUALITY GROWTH (4–6 MONTH HOLD)",
        "cli_subtitle": (
            "Stage-2 uptrend + real momentum | strong growth (decent—not strict—fundamentals) | analyst + insider/inst backing | "
            "Entry = 🟢 good / ⚪ fair / 🟡 extended / 🔴 peak-risk (vsE50 = % above 50-day; extended names are down-ranked so you're not buying the top)"),
        "badge": "Confidence",
        "card": ["Price", "Entry", "Sector", "RevGr", "EarnGr", "Upside"],
    },
    5: {
        "export": "s5_oversold",
        "runner": _run_s5,
        "color": "cyan",
        "label": "Oversold Reversal",
        "short": "Oversold",
        "subtitle": "Was oversold + ≥2 reversal signs (RSI↑/divergence/MACD↑/EMA reclaim…)",
        "cli_title": "STRATEGY 5 — OVERSOLD REVERSAL HUNTER",
        "cli_subtitle": "Was oversold + ≥2 reversal signs (RSI↑/Div/MACD↑/EMA reclaim/HL/UpV/UpHalf) | Run: Night before",
        "badge": "Rev",
        "card": ["Price", "RSI", "RSImin", "Target", "Stop", "RR"],
    },
    6: {
        "export": "s6_sector_stocks",
        "runner": _run_s6,
        "color": "bright_magenta",
        "label": "Sector Rotation",
        "short": "Sectors",
        "subtitle": "Stocks inside the strongest rotating sectors | EMA aligned",
        "cli_title": "STRATEGY 6 — TOP STOCKS IN HOT SECTORS",
        "cli_subtitle": "Stocks inside the strongest rotating sectors | EMA aligned | Run: Night before",
        "badge": "Confidence",
        "card": ["Price", "Sector", "Mom5D", "Mom20D", "Target", "Stop"],
    },
    7: {
        "export": "s7_orb",
        "runner": _run_s7,
        "color": "bright_cyan",
        "label": "Opening Range Breakout (ORB)",
        "short": "ORB",
        "subtitle": "Gap held | above open | volume surging | first 30-min high broken",
        "cli_title": "STRATEGY 7 — OPENING RANGE BREAKOUT (10 AM)",
        "cli_subtitle": "Gap held | Above open | Volume surging | First 30-min high broken | Run: 10 AM",
        "badge": "Confidence",
        "card": ["Price", "Gap", "RVOL", "Target1", "Stop"],
    },
    8: {
        "export": "s8_power_swing",
        "runner": _run_s8,
        "color": "bright_green",
        "label": "Power Swing (1–2 Week Hold)",
        "short": "Power Swing",
        "subtitle": ("Hybrid: pullback bounce to rising 9/20 EMA OR tight-base breakout on volume | "
                     "small-mid friendly | tight 5–14% targets"),
        "cli_title": "STRATEGY 8 — POWER SWING (1–2 WEEK HOLD)",
        "cli_subtitle": "Hybrid: pullback bounce to rising 9/20 EMA OR tight-base breakout on volume | small-mid friendly | tight 5–14% targets",
        "badge": "Confidence",
        "card": ["Price", "Setup", "Target", "Stop", "SetupQ"],
    },
}

ALL_STRATEGIES = sorted(STRATEGIES)

# Export keys that hold strategy picks, in display order. `s6_sectors` is the
# sector leaderboard rather than a pick list, so it is kept separate.
EXPORT_ORDER = [STRATEGIES[k]["export"] for k in ALL_STRATEGIES]

DEFAULT_SOURCES = ["finviz", "yahoo", "reddit", "insider", "quality",
                   "momentum", "finnhub", "movers", "robinhood"]


def strategy_meta() -> list:
    """Registry as a JSON-serializable list — what the web UI renders its tabs from."""
    return [
        {
            "key": k,
            "export": STRATEGIES[k]["export"],
            "label": STRATEGIES[k]["label"],
            "short": STRATEGIES[k]["short"],
            "subtitle": STRATEGIES[k]["subtitle"],
            "badge": STRATEGIES[k]["badge"],
            "card": STRATEGIES[k]["card"],
        }
        for k in ALL_STRATEGIES
    ]


# ──────────────────────────────────────────────
#  BENCHMARK
# ──────────────────────────────────────────────

_bench_cache = {"hour": None, "series": None}


def get_benchmark(force: bool = False):
    """SPY close series for relative-strength scoring, cached for the hour.

    Replaces dashboard.py's @st.cache_data so the engine has no Streamlit
    dependency. Returns None on failure — every strategy treats a missing
    benchmark as neutral RS.
    """
    key = time.strftime("%Y%m%d%H")
    if not force and _bench_cache["hour"] == key:
        return _bench_cache["series"]
    series = None
    try:
        df = yf.download("SPY", period="1y", interval="1d",
                         auto_adjust=True, progress=False)
        if df is not None and len(df) > 60:
            series = df["Close"]
    except Exception:
        series = None
    _bench_cache["hour"] = key
    _bench_cache["series"] = series
    return series


# ──────────────────────────────────────────────
#  SCAN
# ──────────────────────────────────────────────

def _noop(phase, pct, detail=""):
    pass


def run_scan(sources=None, max_tickers: int = 600, strategies=None,
             skip_polygon: bool = False, sleepers: int = 150,
             large: bool = False, show_universe: bool = False,
             s1_price=None, other_price=None, progress=None) -> dict:
    """Build the universe, fetch data, and run the selected strategies.

    Returns the `exports` dict keyed exactly as the CSV exports always were:
    {"s1_catalyst": [...], "s2_swing": [...], ..., "s6_sectors": [...]},
    plus "heat" (industry gainers/losers) when Strategy 1 runs.

    `s1_price` and `other_price` are optional (min, max) price bands: the first
    applies to Strategy 1 only, the second to Strategies 2–8. Either end may be
    None for "no bound", and a band of None leaves that strategy on its own
    default gate (S1: $0.50–$10; the rest: their per-strategy floors).

    `progress(phase, pct, detail)` is called between stages; pct is 0.0–1.0.

    """
    progress = progress or _noop
    sources = list(sources or DEFAULT_SOURCES)
    run = sorted(set(strategies or ALL_STRATEGIES))
    s1_band = tuple(s1_price) if s1_price else None
    other_band = tuple(other_price) if other_price else None

    progress("universe", 0.02, f"Building universe from: {', '.join(sources)}")
    tickers, pop_scores = build_universe(
        sources=sources, max_tickers=max_tickers, show=show_universe,
        large=large, sleeper_quota=sleepers)
    progress("universe", 0.15, f"Universe: {len(tickers)} tickers")

    progress("prices", 0.18, "Fetching price data…")
    data_map = fetch_price_data(tickers, skip_polygon=skip_polygon)
    progress("prices", 0.45, f"Price data: {len(data_map)} tickers")

    progress("fundamentals", 0.48, "Fetching fundamentals…")
    info_map = fetch_fundamentals(list(data_map.keys()))
    enrich_short_interest(info_map)
    progress("fundamentals", 0.60, f"Fundamentals: {len(info_map)} tickers")

    # Premarket / live extended-hours snapshot — feeds the intraday strategies
    # (1–3). Daily bars miss this; the snapshot carries the gap + premarket volume.
    premarket_map = {}
    if not skip_polygon:
        progress("premarket", 0.62, "Fetching premarket snapshot…")
        premarket_map = fetch_polygon_premarket(list(data_map.keys()))

    # Intraday 1-min bars for Strategy 1's intraday_phase model — only fetched
    # for the active S1 price band to keep it lean.
    intraday_map = {}
    news_map = None
    if 1 in run and not skip_polygon:
        # Buffer 20% above the active S1 cap so a name that ticked up since the
        # daily close still gets its intraday bars.
        s1_cap = (s1_band or DEFAULT_PRICE_BANDS[1])[1]
        s1_cap = float(s1_cap) * 1.2 if s1_cap else float("inf")
        s1_candidates = [t for t, df in data_map.items()
                         if len(df) and float(df["Close"].iloc[-1]) <= s1_cap]
        progress("intraday", 0.68, f"Intraday + news for {len(s1_candidates)} S1 candidates…")
        intraday_map = fetch_polygon_intraday(s1_candidates)
        # Fresh-catalyst check for the same S1 candidate set — confirms a real
        # news driver behind each move so the score is trustworthy, not just volume.
        news_map = fetch_polygon_news(s1_candidates)

    progress("benchmark", 0.74, "Loading SPY benchmark…")
    bench_c = get_benchmark()
    if bench_c is None:
        progress("benchmark", 0.75, "SPY benchmark unavailable — RS score will be neutral")

    ctx = {
        "data_map": data_map,
        "info_map": info_map,
        "pop_scores": pop_scores,
        "bench_c": bench_c,
        "premarket": premarket_map,
        "intraday": intraday_map,
        "news": news_map,
        "s1_band": s1_band,
        "other_band": other_band,
    }

    exports = {}

    if 1 in run:
        # Market context first: which industries are leading/lagging today, so
        # the catalyst picks can be read against the rotation backdrop.
        gainers, losers = industry_heat(data_map, info_map, premarket=premarket_map)
        exports["heat"] = {"gainers": gainers, "losers": losers}

    span = 0.24 / max(len(run), 1)
    for i, key in enumerate(run):
        meta = STRATEGIES[key]
        progress("strategies", 0.76 + i * span, f"Scoring {meta['label']}…")
        if key == 6:
            out = s6_sector_rotation(data_map, info_map, pop_scores, price_band=other_band)
            if out and len(out) == 2:
                exports["s6_sectors"], exports["s6_sector_stocks"] = out
        else:
            exports[meta["export"]] = meta["runner"](ctx, False)

    progress("done", 1.0, "Scan complete")
    return exports


# ──────────────────────────────────────────────
#  RATE A SINGLE TICKER
# ──────────────────────────────────────────────

def rate_ticker(ticker: str):
    """Score one ticker against all 8 strategies with entry gates relaxed.

    Returns (ratings, info, error). `ratings` is sorted best-fit first; entries
    whose strategy could not score the ticker carry confidence None.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None, None, "No ticker given."

    data_map = fetch_price_data([ticker])
    if ticker not in data_map:
        return None, None, f"No price data found for {ticker}."

    info_map = fetch_fundamentals([ticker])
    enrich_short_interest(info_map)

    try:
        premarket = fetch_polygon_premarket([ticker])
    except Exception:
        premarket = {}
    try:
        intraday = fetch_polygon_intraday([ticker])
    except Exception:
        intraday = {}

    ctx = {
        "data_map": data_map,
        "info_map": info_map,
        "pop_scores": {ticker: 0},
        "bench_c": get_benchmark(),
        "premarket": premarket,
        "intraday": intraday,
        "news": None,
    }

    ratings = []
    for key in ALL_STRATEGIES:
        meta = STRATEGIES[key]
        try:
            rows = meta["runner"](ctx, True)
        except Exception:
            rows = []
        match = next((r for r in rows if r.get("Ticker") == ticker), None)
        ratings.append({
            "key": key,
            "label": meta["label"],
            "subtitle": meta["subtitle"],
            "confidence": match.get("Confidence") if match else None,
            "row": match,
        })

    ratings.sort(key=lambda x: (x["confidence"] is not None, x["confidence"] or 0),
                 reverse=True)
    return ratings, info_map.get(ticker, {}), None
