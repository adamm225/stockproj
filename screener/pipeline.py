"""The run sequence, top to bottom: parse args -> build universe -> fetch price/
fundamental/premarket/intraday/news data -> load SPY benchmark -> run the
selected strategies -> display -> optional CSV export."""

import argparse
from datetime import datetime

import pandas as pd
import yfinance as yf
from rich.panel import Panel

from .config import console
from .sources import build_universe
from .marketdata import (fetch_price_data, fetch_fundamentals, enrich_short_interest,
                         fetch_polygon_premarket, fetch_polygon_intraday, fetch_polygon_news)
from .strategies import (industry_heat, s1_catalyst, s2_swing, s3_breakout, s4_quality_compounder,
                         s5_oversold_reversal, s6_sector_rotation, s7_orb, s8_power_swing)
from .display import display, display_industry_heat, timing_banner


# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy",      type=int, choices=[1,2,3,4,5,6,7,8])
    ap.add_argument("--overnight",     action="store_true")
    ap.add_argument("--morning",       action="store_true")
    ap.add_argument("--ten-am",        action="store_true")
    ap.add_argument("--swing",         action="store_true", help="Run the swing strategies (2, 4, 8)")
    ap.add_argument("--sources",       nargs="+",
                    default=["finviz", "yahoo", "reddit", "insider", "quality", "momentum", "finnhub", "movers", "robinhood"],
                    choices=["finviz", "yahoo", "reddit", "nasdaq", "insider", "quality", "momentum", "finnhub", "movers", "webull", "robinhood"])
    ap.add_argument("--max",           type=int, default=600,
                    help="Popularity-ranked universe size (default 600).")
    ap.add_argument("--sleepers",      type=int, default=150,
                    help="Extra slots for high-quality 'sleeper' names that didn't make the "
                         "popularity cut (default 150). Set 0 to disable. Widens small/mid-cap variety.")
    ap.add_argument("--large_universe", action="store_true",
                    help="Fetch a wider universe by pulling deeper from the quality-filtered "
                         "Finviz screens (more pages, higher cap). Defaults the cap to 1200.")
    ap.add_argument("--export",        action="store_true")
    ap.add_argument("--show-universe", action="store_true")
    ap.add_argument("--override_polygon", action="store_true", help="Skip Polygon and use yfinance only for price data")
    args = ap.parse_args()

    # In large mode, raise the cap unless the user explicitly set --max
    max_tickers = args.max
    if args.large_universe and args.max == 600:
        max_tickers = 1200

    start_time = datetime.now()

    console.print(Panel.fit(
        "[bold white]NASDAQ POPULARITY SCREENER  v4.0[/bold white]\n"
        "[dim]Finviz + Yahoo + StockTwits + Reddit + OpenInsider + Quality (+ sleepers) → ranked universe → 8 strategies[/dim]\n"
        "[dim]1–3: Premarket/Open  |  4–6: Overnight Prep  |  7: 10 AM ORB  |  8: 1–2wk Power Swing[/dim]",
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
    elif args.swing:
        run = {2, 4, 8}
    else:
        run = {1, 2, 3, 4, 5, 6, 7, 8}

    tickers, pop_scores = build_universe(
        sources=args.sources,
        max_tickers=max_tickers,
        show=args.show_universe,
        large=args.large_universe,
        sleeper_quota=args.sleepers,
    )

    data_map = fetch_price_data(tickers, skip_polygon=args.override_polygon)
    info_map = fetch_fundamentals(list(data_map.keys()))
    enrich_short_interest(info_map)

    # Premarket / live extended-hours snapshot — feeds the intraday strategies (1–3).
    # Daily bars miss this; the snapshot carries the gap + premarket volume.
    premarket_map = {} if args.override_polygon else fetch_polygon_premarket(list(data_map.keys()))

    # Intraday 1-min bars for Strategy 1's intraday_phase model — only fetched for
    # the S1 price band (≤$12, buffered above the $10 cap) to keep it lean.
    intraday_map = {}
    news_map     = None
    if 1 in run and not args.override_polygon:
        s1_candidates = [t for t, df in data_map.items()
                         if len(df) and float(df["Close"].iloc[-1]) <= 12.0]
        intraday_map = fetch_polygon_intraday(s1_candidates)
        # Fresh-catalyst check for the same S1 candidate set — confirms a real news
        # driver behind each move so the score is trustworthy, not just volume.
        news_map = fetch_polygon_news(s1_candidates)

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
        # Market context first: which industries are leading/lagging today, so the
        # catalyst picks below can be read against the rotation backdrop.
        heat_gainers, heat_losers = industry_heat(data_map, info_map, premarket=premarket_map)
        display_industry_heat(heat_gainers, heat_losers)

        r1 = s1_catalyst(data_map, info_map, pop_scores, bench_c=bench_c,
                         premarket=premarket_map, intraday=intraday_map, news=news_map)
        display("STRATEGY 1 — CATALYST / HIGH RVOL",
                "Price = prior close (setup anchor) | LivePrice = EXACT live pre/post quote, session-stamped (PRE/LIVE/AH) with today's % | "
                "LiveVol = shares traded so far this session + % of a normal full day | DayChg = today's change so far | "
                "Score 1–10 = should you HOP IN RIGHT NOW (10=best; chasing parabolic/fade capped low) | "
                "Strength 1–10 = raw move horsepower (high Strength + low Score = wait for pullback) | "
                "Type = Multi-day / All-day / Premarket / Postmarket / Mix | "
                "News = fresh catalyst (count·age); 'none ⚠' = unexplained spike",
                r1, "red")
        exports["s1_catalyst"] = r1

    if 2 in run:
        r2 = s2_swing(data_map, info_map, pop_scores, bench_c=bench_c, premarket=premarket_map)
        display("STRATEGY 2 — MOMENTUM SWING",
                "EMA stacked | RSI 50–68 | HH+HL | SetupQ favors Stage2 + RS leaders + accumulation",
                r2, "green")
        exports["s2_swing"] = r2

    if 3 in run:
        r3 = s3_breakout(data_map, info_map, pop_scores, bench_c=bench_c, premarket=premarket_map)
        display("STRATEGY 3 — GAP & BREAKOUT",
                "PM gap + daily gap | Flat base break | Volume confirm | Phase tag flags Base/Breakout/Continuation/Extended",
                r3, "yellow")
        exports["s3_breakout"] = r3

    if 4 in run:
        r4 = s4_quality_compounder(data_map, info_map, pop_scores, bench_c=bench_c)
        display("STRATEGY 4 — QUALITY GROWTH (4–6 MONTH HOLD)",
                "Stage-2 uptrend + real momentum | strong growth (decent—not strict—fundamentals) | analyst + insider/inst backing | "
                "Entry = 🟢 good / ⚪ fair / 🟡 extended / 🔴 peak-risk (vsE50 = % above 50-day; extended names are down-ranked so you're not buying the top)",
                r4, "magenta")
        exports["s4_quality"] = r4

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

    if 8 in run:
        r8 = s8_power_swing(data_map, info_map, pop_scores, bench_c=bench_c)
        display("STRATEGY 8 — POWER SWING (1–2 WEEK HOLD)",
                "Hybrid: pullback bounce to rising 9/20 EMA OR tight-base breakout on volume | small-mid friendly | tight 5–14% targets",
                r8, "bright_green")
        exports["s8_power_swing"] = r8

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
        "  --overnight  →  Strategies 4 (Quality Compounder), 5 (Oversold), 6 (Sector Rotation)\n\n"
        "[green]6:00 – 9:30 AM  (premarket)[/green]\n"
        "  --morning    →  Strategies 1 (Catalyst/RVOL), 2 (Swing), 3 (Gap/Breakout)\n\n"
        "[yellow]10:00 – 10:30 AM  (after open dust settles)[/yellow]\n"
        "  --ten-am     →  Strategy 7 (ORB)\n\n"
        "[bright_green]Any time — swing watchlist[/bright_green]\n"
        "  --swing      →  Strategies 2 (4–8wk), 4 (4–6mo Quality), 8 (1–2wk Power Swing)\n\n"
        "[dim]Best days: Tue/Wed/Thu for cleanest setups. Mon for gap plays. Thu for earnings.[/dim]",
        border_style="blue", padding=(0, 2)
    ))

    console.print(Panel.fit(
        "[bold yellow]⚠  DISCLAIMER[/bold yellow]\n"
        "[dim]Educational use only. Not financial advice. Trade at your own risk.[/dim]",
        border_style="yellow"))

    end_time = datetime.now()
    end_time_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
    elapsed = end_time - start_time
    minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
    console.print(f"\n[dim]Completed: {end_time_str}  |  Time taken: {minutes}m {seconds}s[/dim]")
