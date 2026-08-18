"""Presentation: the rich result table renderer and the market-clock banner."""

from datetime import datetime

import pytz
from rich import box
from rich.panel import Panel
from rich.table import Table

from .config import console


# ──────────────────────────────────────────────
#  INDUSTRY HEAT
# ──────────────────────────────────────────────

def display_industry_heat(gainers, losers):
    """Side-by-side top gaining vs losing industries (avg today's move + name count)."""
    if not gainers and not losers:
        console.print("[dim]Industry heat: not enough industry data to rank.[/dim]\n")
        return

    console.print(Panel.fit(
        "[bold]INDUSTRY HEAT — today's leaders & laggards[/bold]\n"
        "[dim]Avg move per industry across the screened universe (min 3 names) — "
        "where money is rotating before you trade catalysts[/dim]",
        border_style="bright_white", padding=(0, 2)))

    t = Table(box=box.SIMPLE_HEAVY, expand=True, show_lines=False)
    t.add_column("🟢 Top Gaining Industries", no_wrap=True, min_width=26)
    t.add_column("Avg",  justify="right", min_width=8)
    t.add_column("#",    justify="right", min_width=4)
    t.add_column("🔴 Top Losing Industries", no_wrap=True, min_width=26)
    t.add_column("Avg",  justify="right", min_width=8)
    t.add_column("#",    justify="right", min_width=4)

    for i in range(max(len(gainers), len(losers))):
        g = gainers[i] if i < len(gainers) else None
        l = losers[i]  if i < len(losers)  else None
        t.add_row(
            g["Industry"] if g else "—",
            f"[green]{g['Avg']:+.2f}%[/green]" if g else "",
            str(g["Count"]) if g else "",
            l["Industry"] if l else "—",
            f"[red]{l['Avg']:+.2f}%[/red]" if l else "",
            str(l["Count"]) if l else "",
        )
    console.print(t)
    console.print()


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
        "Gap": 7, "PM_Gap": 8, "FlatBase": 9, "H52Break": 10,
        "Phase": 13, "SetupQ": 8,
        "RSImin": 8, "RevSigns": 28, "Rev": 5, "CatQ": 6,
        "Industry": 20, "Country": 12,
        # Strategy 1 — "hop in now" scoring columns
        "Score": 6, "Strength": 9, "Verdict": 22, "Type": 18, "Why": 34, "News": 11,
        "LivePrice": 20, "LiveVol": 13,
        # Strategy 4 — entry-timing guard columns
        "Entry": 14, "vsE50": 7,
        # Strategy 4 — Quality Compounder columns
        "Sector": 15, "RevGr": 7, "EarnGr": 7, "NetMgn": 7, "ROE": 6,
        "PEG": 6, "Rating": 14, "Upside": 8, "Analysts": 9, "Insider": 8,
        "Inst": 7, "ShortFlt": 9, "Beta": 6, "Trend": 8, "Mom6M": 8,
        "Pos52": 7, "QualityQ": 9, "EarnIn": 7,
        # Strategy 8 — Power Swing columns
        "Setup": 10, "vsEMA9": 9, "Mom5": 8, "Mom10": 8, "RS": 6,
    }
    t = Table(box=box.SIMPLE_HEAVY, header_style=f"bold {color}",
              show_lines=True, expand=True)
    for col in rows[0]:
        mw = col_widths.get(col, 8)
        t.add_column(col, no_wrap=True, min_width=mw)
    for row in rows:
        if "Score" in row:                       # Strategy 1 — 1–10 hop-in score
            s10   = row.get("Score", 0)
            style = "bold green" if s10 >= 8 else ("yellow" if s10 >= 5 else "dim white")
        else:
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
        best   = "Strategies 4 (Quality Compounder), 5 (Oversold), 6 (Sector Rotation)  →  --overnight"
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

