"""
price_alerter.py — live price + strategy-signal watchdog, pushed via ntfy.
============================================================================

Watches every ticker in ``tickers.txt`` on a fixed interval (default 10 min)
and publishes a push notification (via ntfy.sh) when either of two things
happens:

  1) STRATEGY SIGNAL — the same analysis the screener runs (nasdaq_v3.py),
     scored per-ticker with entry gates relaxed. If ANY strategy (except
     Strategy 4, the 4–6 month hold) reaches confidence >= 0.85, you get one
     alert listing every qualifying strategy. After that fires, the ticker is
     muted for analysis for the rest of the day.

  2) PRICE CROSS — each ticker line may carry threshold criteria, e.g.
        MU (<450,<700,>1100)
        URG (>1.15,>1.45)
     A "<X" criterion fires only when price was at/above X earlier today and
     then crossed below (reported as "sank from today's high $Y to $Z, below
     $X"). ">X" is the mirror (crossed up from today's low). Each criterion
     fires at most once per day; a *different* criterion on the same ticker
     re-alerts.

Live price accounts for pre-market and after-hours via Polygon's snapshot
(``fetch_polygon_premarket`` → ``pm_price`` = last trade incl. extended hours).

State (what has already fired today) is persisted to ``alerter_state.json`` and
reset automatically at the start of each new trading day (US/Eastern).

Notification transport
-----------------------
Publishes to a private ntfy.sh topic — no login, no API key. Subscribe on
your phone (ntfy app, iOS/Android) or browser to https://ntfy.sh/<topic> to
receive the pushes. The topic name IS the secret — anyone who knows it can
publish/read, so keep it as unguessable as the default below (override via
.env if you want your own):

    NTFY_TOPIC="adam-stock-alerts-4f8c9b2d71e3a6f5"

Usage
-----
    python price_alerter.py                # loop forever, every 10 min
    python price_alerter.py --once         # run a single cycle and exit
    python price_alerter.py --interval 300 # custom seconds between cycles
    python price_alerter.py --test-notify  # send a test push and exit
    python price_alerter.py --ignore-hours # don't skip outside trading hours
"""

import sys

# rich (pulled in via nasdaq_v3) prints emoji/box chars; force UTF-8 on Windows
# so a cp1252 console can't crash the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import argparse
import json
import os
import re
import time
from datetime import datetime

import pytz
import requests
import yfinance as yf
from dotenv import load_dotenv

import nasdaq_v3 as ns

load_dotenv()

# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────

ET               = pytz.timezone("America/New_York")
CONF_THRESHOLD   = 0.85                       # normalized 0–1 confidence to alert on
STATE_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "alerter_state.json")
TICKERS_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tickers.txt")
DEFAULT_INTERVAL = 600                        # seconds (10 minutes)

# Extended trading window (ET): premarket 4:00 → after-hours 20:00, weekdays.
SESSION_START_H  = 4.0
SESSION_END_H    = 20.0

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "adam-stock-alerts-4f8c9b2d71e3a6f5")
NTFY_URL   = f"https://ntfy.sh/{NTFY_TOPIC}"


# Analysis strategies to score, EXCLUDING Strategy 4 (4–6 month hold). Each entry:
# (tag, human label, runner, value-key, scale)  — normalized conf = row[key] / scale.
def _run_s1(dm, im, pop, bench, pm): return ns.s1_catalyst(dm, im, pop, bench_c=bench, premarket=pm, relax=True)
def _run_s2(dm, im, pop, bench, pm): return ns.s2_swing(dm, im, pop, bench_c=bench, premarket=pm, relax=True)
def _run_s3(dm, im, pop, bench, pm): return ns.s3_breakout(dm, im, pop, bench_c=bench, premarket=pm, relax=True)
def _run_s5(dm, im, pop, bench, pm): return ns.s5_oversold_reversal(dm, im, pop, relax=True)
def _run_s7(dm, im, pop, bench, pm): return ns.s7_orb(dm, im, pop, bench_c=bench, relax=True)
def _run_s8(dm, im, pop, bench, pm): return ns.s8_power_swing(dm, im, pop, bench_c=bench, relax=True)

def _run_s6(dm, im, pop, bench, pm):
    out = ns.s6_sector_rotation(dm, im, pop, relax=True)
    return out[1] if isinstance(out, tuple) and len(out) == 2 else []

ANALYSIS_STRATS = [
    ("S1", "Catalyst / High RVOL",       _run_s1, "Score",      10),
    ("S2", "Momentum Swing",             _run_s2, "Confidence", 100),
    ("S3", "Gap & Breakout",             _run_s3, "Confidence", 100),
    ("S5", "Oversold Reversal",          _run_s5, "Confidence", 100),
    ("S6", "Sector Rotation",            _run_s6, "Confidence", 100),
    ("S7", "Opening Range Breakout",     _run_s7, "Confidence", 100),
    ("S8", "Power Swing",                _run_s8, "Confidence", 100),
]

# ──────────────────────────────────────────────
#  TICKERS + CRITERIA PARSING
# ──────────────────────────────────────────────

def parse_tickers(path):
    """Parse tickers.txt into [{'ticker': str, 'criteria': [(op, level, raw), ...]}].

    Line format:  TICKER (<450,<700,>1100)   — criteria optional.
    op is '<' or '>', level is a float, raw is the original token ('<700').
    """
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z.\-]+)\s*(?:\((.*)\))?", line)
            if not m:
                continue
            ticker = m.group(1).upper()
            criteria = []
            if m.group(2):
                for tok in m.group(2).split(","):
                    tok = tok.strip()
                    cm = re.match(r"^([<>])\s*([0-9]*\.?[0-9]+)$", tok)
                    if cm:
                        criteria.append((cm.group(1), float(cm.group(2)), f"{cm.group(1)}{cm.group(2)}"))
            out.append({"ticker": ticker, "criteria": criteria})
    return out


# ──────────────────────────────────────────────
#  STATE  (persisted, reset each new ET day)
# ──────────────────────────────────────────────

def today_str():
    return datetime.now(ET).strftime("%Y-%m-%d")


def load_state():
    """Load today's state, or a fresh blank state if the file is missing/stale."""
    blank = {"date": today_str(), "analysis_fired": [], "criteria_fired": {},
             "day_high": {}, "day_low": {}}
    if not os.path.exists(STATE_FILE):
        return blank
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return blank
    if st.get("date") != today_str():           # new day → wipe all daily memory
        return blank
    for k, v in blank.items():
        st.setdefault(k, v)
    return st


def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
    except Exception as e:
        print(f"⚠ could not save state: {e}")


# ──────────────────────────────────────────────
#  NTFY  (push notification)
# ──────────────────────────────────────────────

def send_ntfy(subject, body):
    """Publish an alert as a push notification to the ntfy.sh topic. Returns
    True on success; prints the alert to the console and returns False on
    failure (network down, ntfy.sh unreachable, etc.)."""
    try:
        r = requests.post(
            NTFY_URL,
            data=body.encode("utf-8"),
            headers={
                "Title":    subject,
                "Priority": "high",
                "Tags":     "chart_with_upwards_trend",
            },
            timeout=15,
        )
        if r.status_code == 200:
            print(f"🔔 published: {subject}")
            return True
        print(f"⚠ ntfy returned {r.status_code} — alert below:\nSubject: {subject}\n{body}")
        return False
    except Exception as e:
        print(f"⚠ ntfy publish failed ({e}) — alert below:\nSubject: {subject}\n{body}")
        return False


# ──────────────────────────────────────────────
#  ANALYSIS  (per-cycle, over the whole watchlist)
# ──────────────────────────────────────────────

def get_benchmark():
    """SPY close series for relative-strength scoring; None on failure."""
    try:
        df = yf.download("SPY", period="1y", interval="1d",
                         auto_adjust=True, progress=False)
        if df is not None and len(df) > 60:
            return df["Close"]
    except Exception:
        pass
    return None


def run_analysis(tickers, data_map, info_map, pop, bench, premarket):
    """Score all strategies once over the full watchlist and return
    {ticker: [(tag, label, normalized_conf, display), ...]} for qualifiers >= threshold."""
    hits = {t: [] for t in tickers}
    for tag, label, runner, key, scale in ANALYSIS_STRATS:
        try:
            rows = runner(data_map, info_map, pop, bench, premarket) or []
        except Exception:
            rows = []
        by_ticker = {r.get("Ticker"): r for r in rows if r.get("Ticker")}
        for t in tickers:
            r = by_ticker.get(t)
            if not r or key not in r:
                continue
            try:
                norm = float(r[key]) / scale
            except (TypeError, ValueError):
                continue
            if norm >= CONF_THRESHOLD:
                disp = f"{r[key]}/10" if scale == 10 else f"{int(r[key])}%"
                hits[t].append((tag, label, norm, disp))
    return hits


# ──────────────────────────────────────────────
#  PRICE-CROSS EVALUATION
# ──────────────────────────────────────────────

def eval_price_crosses(entry, price, st):
    """Update day high/low and return a list of alert strings for any criteria
    that crossed this cycle (and haven't already fired today)."""
    tkr      = entry["ticker"]
    alerts   = []
    fired    = st["criteria_fired"].setdefault(tkr, [])

    # Track intraday extremes (used both for the cross test and the report).
    hi = st["day_high"].get(tkr)
    lo = st["day_low"].get(tkr)
    st["day_high"][tkr] = price if hi is None else max(hi, price)
    st["day_low"][tkr]  = price if lo is None else min(lo, price)
    day_high = st["day_high"][tkr]
    day_low  = st["day_low"][tkr]

    for op, level, raw in entry["criteria"]:
        if raw in fired:
            continue
        if op == "<" and price < level and day_high >= level:
            alerts.append(f"{tkr} sank from today's high ${day_high:,.2f} "
                          f"to ${price:,.2f} — now below {raw[1:]}.")
            fired.append(raw)
        elif op == ">" and price > level and day_low <= level:
            alerts.append(f"{tkr} climbed from today's low ${day_low:,.2f} "
                          f"to ${price:,.2f} — now above {raw[1:]}.")
            fired.append(raw)
    return alerts


# ──────────────────────────────────────────────
#  ONE CYCLE
# ──────────────────────────────────────────────

def run_cycle():
    watch = parse_tickers(TICKERS_FILE)
    if not watch:
        print("No tickers in tickers.txt — nothing to do.")
        return
    tickers = [w["ticker"] for w in watch]
    st = load_state()
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z")
    print(f"\n── cycle {now} — watching {', '.join(tickers)} ──")

    # 1) Live prices (one snapshot call; includes pre/after-hours last trade).
    try:
        premarket = ns.fetch_polygon_premarket(tickers)
    except Exception as e:
        print(f"⚠ price snapshot failed: {e}")
        premarket = {}

    # 2) Bulk data + fundamentals for the analysis pass (fetched once for all).
    data_map = ns.fetch_price_data(tickers)
    info_map = ns.fetch_fundamentals(list(data_map.keys()))
    try:
        ns.enrich_short_interest(info_map)
    except Exception:
        pass
    pop   = {t: 0 for t in data_map}
    bench = get_benchmark()

    # 3) Only analyze tickers not already muted for the day.
    to_analyze = [t for t in data_map if t not in st["analysis_fired"]]
    hits = run_analysis(to_analyze, data_map, info_map, pop, bench, premarket) if to_analyze else {}

    alert_sections = []

    # --- Strategy-signal alerts ---
    for t in to_analyze:
        qual = hits.get(t) or []
        if not qual:
            continue
        qual.sort(key=lambda x: x[2], reverse=True)
        lines = [f"  • {tag} {label}: {disp} (>= {int(CONF_THRESHOLD*100)}%)"
                 for tag, label, _n, disp in qual]
        alert_sections.append(f"[SIGNAL] {t} hit {len(qual)} "
                              f"strateg{'y' if len(qual)==1 else 'ies'} at/above threshold:\n"
                              + "\n".join(lines))
        st["analysis_fired"].append(t)      # mute analysis for this ticker today

    # --- Price-cross alerts ---
    for w in watch:
        t = w["ticker"]
        snap = premarket.get(t)
        if not snap or not snap.get("pm_price"):
            continue
        price = float(snap["pm_price"])
        for msg in eval_price_crosses(w, price, st):
            alert_sections.append(f"[PRICE] {msg}")

    # 4) One batched push per cycle if anything new fired.
    if alert_sections:
        body = (f"Stock alerts — {now}\n\n" + "\n\n".join(alert_sections)
                + "\n\n— price_alerter.py")
        subject = f"Stock alert: {len(alert_sections)} new "\
                  f"trigger{'s' if len(alert_sections) != 1 else ''}"
        send_ntfy(subject, body)
    else:
        print("  no new triggers this cycle.")

    st["date"] = today_str()
    save_state(st)


# ──────────────────────────────────────────────
#  SCHEDULING
# ──────────────────────────────────────────────

def in_session(ignore_hours=False):
    if ignore_hours:
        return True
    now = datetime.now(ET)
    if now.weekday() >= 5:                    # Sat/Sun
        return False
    h = now.hour + now.minute / 60
    return SESSION_START_H <= h < SESSION_END_H


def main():
    ap = argparse.ArgumentParser(description="Live price + strategy-signal ntfy alerter.")
    ap.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help="Seconds between cycles (default 600).")
    ap.add_argument("--ignore-hours", action="store_true",
                    help="Run cycles even outside the 4:00–20:00 ET weekday window.")
    ap.add_argument("--test-notify", action="store_true",
                    help="Send a test push to the ntfy topic to confirm it's wired up, then exit.")
    args = ap.parse_args()

    if args.test_notify:
        ok = send_ntfy("Adams the goat",
                       f"If you're reading this, ntfy is wired up correctly.\nTopic: {NTFY_TOPIC}")
        print("Test push sent." if ok else "Push not sent (see message above).")
        return

    if args.once:
        if in_session(args.ignore_hours):
            run_cycle()
        else:
            print("Outside trading hours (4:00–20:00 ET, weekdays). "
                  "Use --ignore-hours to force a run.")
        return

    print(f"price_alerter running every {args.interval}s. Ctrl+C to stop.")
    while True:
        try:
            if in_session(args.ignore_hours):
                run_cycle()
            else:
                print(f"[{datetime.now(ET):%H:%M %Z}] outside trading hours — sleeping.")
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"⚠ cycle error: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
