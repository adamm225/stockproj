"""
rank_recommendations.py

Reads all strategy export CSVs in runs/ (filename pattern: s<N>_<name>_YYYYMMDD_HHMM.csv),
treats each (ticker, run-timestamp) as a separate recommendation, fetches OHLC from
recommendation time to now, and ranks by best max-percent-gain.

Metrics per recommendation:
  RecPrice   — Price column from the export
  High       — highest High since recommendation
  Low        — lowest  Low  since recommendation
  Current    — latest Close
  MaxGain%   — (High    - RecPrice) / RecPrice * 100   (the "if you sold at the top")
  Drawdown%  — (Low     - RecPrice) / RecPrice * 100   (worst point hit)
  Now%       — (Current - RecPrice) / RecPrice * 100

Usage:
  python rank_recommendations.py
  python rank_recommendations.py --strategy s3_breakout
  python rank_recommendations.py --since 2026-05-12
  python rank_recommendations.py --top 50
"""

from __future__ import annotations
import argparse
import glob
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(PROJ_DIR, "runs")

FNAME_RE = re.compile(r"^(s\d+_[^_]+)_(\d{8})_(\d{4})\.csv$")


def parse_run_file(path: str):
    """Return (strategy, run_dt) or None if filename doesn't match."""
    m = FNAME_RE.match(os.path.basename(path))
    if not m:
        return None
    strategy, ymd, hm = m.group(1), m.group(2), m.group(3)
    try:
        run_dt = datetime.strptime(f"{ymd} {hm}", "%Y%m%d %H%M")
    except ValueError:
        return None
    return strategy, run_dt


def load_recommendations(strategy_filter: str | None, since: datetime | None) -> pd.DataFrame:
    rows = []
    for path in sorted(glob.glob(os.path.join(RUNS_DIR, "s*_*.csv"))):
        parsed = parse_run_file(path)
        if not parsed:
            continue
        strategy, run_dt = parsed
        if strategy_filter and strategy != strategy_filter:
            continue
        if since and run_dt < since:
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if df.empty or "Ticker" not in df.columns or "Price" not in df.columns:
            continue
        for _, r in df.iterrows():
            try:
                price = float(str(r["Price"]).replace("$", "").replace(",", ""))
            except (ValueError, TypeError):
                continue
            ticker = str(r["Ticker"]).strip().upper()
            if not ticker or not ticker.isalpha():
                continue
            rows.append({
                "Ticker":   ticker,
                "Strategy": strategy,
                "RunTime":  run_dt,
                "RecPrice": price,
                "Conf":     r.get("Confidence", ""),
                "SetupQ":   r.get("SetupQ", ""),
            })
    return pd.DataFrame(rows)


def fetch_bars_alpaca(tickers: list, start: datetime) -> dict:
    if not ALPACA_AVAILABLE:
        return {}
    api_key    = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        return {}
    try:
        client = StockHistoricalDataClient(api_key, secret_key)
        request = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame.Day,
            start=start.date(),
            end=datetime.now().date(),
        )
        bars = client.get_stock_bars(request)
        out = {}
        for t in tickers:
            if t in bars.df.index.get_level_values(0):
                sub = bars.df.loc[t].copy()
                sub.columns = [c.title() for c in sub.columns]
                if {"Open", "High", "Low", "Close"}.issubset(sub.columns):
                    out[t] = sub
        return out
    except Exception as e:
        console.print(f"[yellow]⚠ Alpaca fetch failed: {e}[/yellow]")
        return {}


def fetch_bars_yfinance(tickers: list, start: datetime) -> dict:
    out = {}
    try:
        df = yf.download(
            tickers=tickers,
            start=start.date() - timedelta(days=1),
            end=datetime.now().date() + timedelta(days=1),
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
        if df is None or df.empty:
            return {}
        if isinstance(df.columns, pd.MultiIndex):
            for t in tickers:
                if t in df.columns.get_level_values(0):
                    sub = df[t].dropna(how="all")
                    if len(sub) > 0:
                        out[t] = sub
        else:
            if len(tickers) == 1 and len(df) > 0:
                out[tickers[0]] = df
    except Exception as e:
        console.print(f"[yellow]⚠ yfinance fetch failed: {e}[/yellow]")
    return out


def evaluate(recs: pd.DataFrame) -> pd.DataFrame:
    if recs.empty:
        return recs

    earliest_per_ticker = recs.groupby("Ticker")["RunTime"].min().to_dict()
    tickers = list(earliest_per_ticker.keys())
    global_start = min(earliest_per_ticker.values())

    console.print(f"[cyan]Fetching OHLC for {len(tickers)} tickers since {global_start.date()}...[/cyan]")

    bars = fetch_bars_alpaca(tickers, global_start)
    if bars:
        console.print(f"[green]✔ Alpaca returned {len(bars)} tickers[/green]")
        missing = [t for t in tickers if t not in bars]
    else:
        missing = tickers

    if missing:
        console.print(f"[cyan]yfinance fallback for {len(missing)} tickers...[/cyan]")
        yb = fetch_bars_yfinance(missing, global_start)
        bars.update(yb)
        console.print(f"[green]✔ yfinance returned {len(yb)} tickers[/green]")

    results = []
    for _, rec in recs.iterrows():
        t = rec["Ticker"]
        df = bars.get(t)
        if df is None or df.empty:
            results.append({**rec.to_dict(),
                            "High": None, "Low": None, "Current": None,
                            "MaxGain%": None, "Drawdown%": None, "Now%": None,
                            "Status": "no data"})
            continue
        # Slice from run-time onwards (inclusive of same day if bar exists)
        df_idx = df.copy()
        try:
            df_idx.index = pd.to_datetime(df_idx.index).tz_localize(None)
        except (AttributeError, TypeError):
            df_idx.index = pd.to_datetime(df_idx.index)
            try:
                df_idx.index = df_idx.index.tz_localize(None)
            except (AttributeError, TypeError):
                pass
        cutoff = pd.Timestamp(rec["RunTime"]).normalize()
        sub = df_idx[df_idx.index >= cutoff]
        if sub.empty:
            sub = df_idx.tail(1)
        try:
            high    = float(sub["High"].max())
            low     = float(sub["Low"].min())
            current = float(sub["Close"].iloc[-1])
        except (KeyError, ValueError, IndexError):
            results.append({**rec.to_dict(),
                            "High": None, "Low": None, "Current": None,
                            "MaxGain%": None, "Drawdown%": None, "Now%": None,
                            "Status": "bad data"})
            continue
        rp = rec["RecPrice"]
        results.append({**rec.to_dict(),
                        "High":      round(high, 2),
                        "Low":       round(low, 2),
                        "Current":   round(current, 2),
                        "MaxGain%":  round((high    - rp) / rp * 100, 2),
                        "Drawdown%": round((low     - rp) / rp * 100, 2),
                        "Now%":      round((current - rp) / rp * 100, 2),
                        "Status":    "ok"})
    return pd.DataFrame(results)


def render(df: pd.DataFrame, top: int):
    if df.empty:
        console.print("[yellow]No recommendations found in runs/[/yellow]")
        return
    ok      = df[df["Status"] == "ok"].copy()
    bad     = df[df["Status"] != "ok"]
    ok      = ok.sort_values("MaxGain%", ascending=False).head(top)

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan", show_lines=False, expand=True)
    cols = ["Ticker", "Strategy", "RunTime", "RecPrice", "Current", "Now%",
            "High", "MaxGain%", "Low", "Drawdown%", "Conf", "SetupQ"]
    for c in cols:
        table.add_column(c, no_wrap=True)
    for _, r in ok.iterrows():
        gain = r["MaxGain%"]
        style = ("bold green" if gain >= 10 else
                 "green"      if gain >= 5  else
                 "yellow"     if gain >= 0  else
                 "red")
        row = [
            str(r["Ticker"]),
            str(r["Strategy"]),
            r["RunTime"].strftime("%m-%d %H:%M") if isinstance(r["RunTime"], (pd.Timestamp, datetime)) else str(r["RunTime"]),
            f"${r['RecPrice']:.2f}",
            f"${r['Current']:.2f}",
            f"{r['Now%']:+.2f}%",
            f"${r['High']:.2f}",
            f"{r['MaxGain%']:+.2f}%",
            f"${r['Low']:.2f}",
            f"{r['Drawdown%']:+.2f}%",
            str(r["Conf"]),
            str(r["SetupQ"]),
        ]
        table.add_row(*row, style=style)
    console.print(table)

    if not bad.empty:
        console.print(f"\n[dim]Skipped {len(bad)} recommendation(s) with no price data "
                      f"({', '.join(sorted(set(bad['Ticker'])))[:200]})[/dim]")

    if not ok.empty:
        winners = (ok["MaxGain%"] >= 10).sum()
        positives = (ok["MaxGain%"] > 0).sum()
        console.print(f"\n[bold]{len(ok)} ranked[/bold] | "
                      f"[green]{winners}[/green] ≥+10% peak | "
                      f"[cyan]{positives}[/cyan] hit any positive peak")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None,
                    help="Filter to one strategy prefix, e.g. 's3_breakout'")
    ap.add_argument("--since", default=None,
                    help="Only include runs from this date forward (YYYY-MM-DD)")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--export", action="store_true",
                    help="Save full ranked table to ranked_<TS>.csv")
    args = ap.parse_args()

    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            console.print(f"[red]Bad --since value (expected YYYY-MM-DD): {args.since}[/red]")
            sys.exit(1)

    recs = load_recommendations(args.strategy, since)
    if recs.empty:
        console.print(f"[yellow]No recommendation CSVs found in {RUNS_DIR}[/yellow]")
        sys.exit(0)

    console.print(f"[green]Loaded {len(recs)} recommendations across "
                  f"{recs['Ticker'].nunique()} unique tickers "
                  f"from {recs['Strategy'].nunique()} strategies[/green]\n")

    result = evaluate(recs)
    render(result, args.top)

    if args.export:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        out = os.path.join(PROJ_DIR, f"ranked_{ts}.csv")
        result.sort_values("MaxGain%", ascending=False, na_position="last").to_csv(out, index=False)
        console.print(f"\n[green]💾 Saved full ranked table → {out}[/green]")


if __name__ == "__main__":
    main()
