"""The CLI front-end: parse args -> engine.run_scan() -> rich tables -> optional
CSV export.

The actual run sequence (universe -> price/fundamental/premarket/intraday/news
data -> benchmark -> strategies) lives in screener/engine.py, which the web UI
in webapp/ calls too. Keeping it there means the CLI and the dashboard can never
drift apart.
"""

import argparse
from datetime import datetime

import pandas as pd
from rich.panel import Panel

from .config import console
from .engine import ALL_STRATEGIES, DEFAULT_SOURCES, STRATEGIES, run_scan
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
                    default=DEFAULT_SOURCES,
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
        run = set(ALL_STRATEGIES)

    def report(phase, pct, detail=""):
        if detail:
            console.print(f"[dim]· {detail}[/dim]")

    exports = run_scan(
        sources=args.sources,
        max_tickers=max_tickers,
        strategies=sorted(run),
        skip_polygon=args.override_polygon,
        sleepers=args.sleepers,
        large=args.large_universe,
        show_universe=args.show_universe,
        progress=report,
    )
    console.print()

    # Industry heat is market context for the Strategy 1 picks — print it first.
    heat = exports.get("heat")
    if heat:
        display_industry_heat(heat["gainers"], heat["losers"])

    for key in sorted(run):
        meta = STRATEGIES[key]
        if key == 6:
            sector_lb = exports.get("s6_sectors")
            if sector_lb:
                display("SECTOR HEAT MAP — Which sectors have money flowing in",
                        "Sorted by momentum score — top 4 sectors feed stock picks below",
                        sector_lb, meta["color"])
            display(meta["cli_title"], meta["cli_subtitle"],
                    exports.get("s6_sector_stocks"), meta["color"])
        else:
            display(meta["cli_title"], meta["cli_subtitle"],
                    exports.get(meta["export"]), meta["color"])

    if args.export:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        for name, data in exports.items():
            if name == "heat" or not data:      # heat is a dict of two lists, not a table
                continue
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
