"""The eight strategy screeners and their scoring helpers (country filter,
popularity percentile, intraday/daily continuation, the Strategy-1 hop-in /
move-type scoring, Strategy-4 entry timing). Pulls all math from indicators."""

import time
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

from .config import console
from .indicators import *


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
#  INDUSTRY HEAT  (market context before Strategy 1)
# ──────────────────────────────────────────────

def industry_heat(data_map, info_map, premarket=None, min_names=3, top=4):
    """Aggregate today's % move by INDUSTRY across the screened universe so you can
    see, at a glance, which industries are leading and lagging before you trade the
    catalyst names in Strategy 1.

    Uses the live premarket/extended-hours gap when a snapshot exists, otherwise the
    latest completed daily change. Only industries with >= `min_names` names are
    kept (one stock isn't a trend). Returns (gainers, losers): each a list of
    {Industry, Avg, Count} dicts, gainers sorted high→low and losers low→high,
    capped at `top` each."""
    pm      = premarket or {}
    buckets = defaultdict(list)
    for ticker, df in data_map.items():
        try:
            ind = str((info_map.get(ticker, {}) or {}).get("industry", "") or "").strip()
            if not ind:
                continue
            snap = pm.get(ticker)
            if snap and snap.get("pm_gap") is not None:
                chg = float(snap["pm_gap"])                      # live gap (incl. pre/post)
            else:
                c = df["Close"]
                if len(c) < 2:
                    continue
                chg = (float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
            buckets[ind].append(chg)
        except Exception:
            continue

    rows = [{"Industry": ind, "Avg": round(sum(chgs) / len(chgs), 2), "Count": len(chgs)}
            for ind, chgs in buckets.items() if len(chgs) >= min_names]

    gainers = sorted([r for r in rows if r["Avg"] > 0], key=lambda x: x["Avg"], reverse=True)[:top]
    losers  = sorted([r for r in rows if r["Avg"] < 0], key=lambda x: x["Avg"])[:top]
    return gainers, losers


# ──────────────────────────────────────────────
#  POPULARITY NORMALISATION + INTRADAY CONVICTION
#  (helpers for Strategy 1's reworked PopScore / Confidence)
# ──────────────────────────────────────────────

def _pop_percentile_map(pop_scores: dict) -> dict:
    """Turn raw popularity scores (arbitrary magnitude) into an interpretable
    0–100 percentile rank within the universe. PopScore 95 ⇒ more popular than
    95% of the universe — far more meaningful than the raw weighted count."""
    if not pop_scores:
        return {}
    arr = np.array(list(pop_scores.values()), dtype=float)
    n   = len(arr)
    return {t: round(float((arr <= s).sum()) / n * 100) for t, s in pop_scores.items()}


def intraday_continuation(df_min):
    """Strategy 1's core read: 'will this keep climbing *today*?' conviction,
    from 1-minute bars + session VWAP.

    Scores how cleanly price is trending up through the session with room left
    to run (not parabolic). Higher = more reliable continuation. Returns
    (score 0-100 | None, phase_label, meta). None ⇒ no intraday data available.
    """
    if df_min is None or len(df_min) < 15:
        return None, "No-Intraday", {}
    try:
        o, h, l = df_min["Open"], df_min["High"], df_min["Low"]
        c, v    = df_min["Close"], df_min["Volume"]
        price   = float(c.iloc[-1])
        if price <= 0:
            return None, "No-Intraday", {}

        tp   = (h + l + c) / 3.0
        cumv = v.cumsum()
        vwap = (tp * v).cumsum() / cumv.replace(0, np.nan)
        vw   = float(vwap.iloc[-1])
        if not np.isfinite(vw) or vw <= 0:
            return None, "No-Intraday", {}

        dist       = (price - vw) / vw * 100
        sess_h     = float(h.max()); sess_l = float(l.min())
        rng        = sess_h - sess_l
        pos        = (price - sess_l) / rng if rng > 0 else 0.5
        above_frac = float((c > vwap).mean())

        seg       = c.iloc[-15:].values.astype(float)
        slope     = float(np.polyfit(np.arange(len(seg)), seg, 1)[0]) if len(seg) >= 5 else 0.0
        slope_pct = slope / price * 100 if price > 0 else 0.0

        # Volume momentum — are buyers still showing up late in the session?
        third     = max(len(v) // 3, 1)
        vol_early = float(v.iloc[:third].mean())
        vol_late  = float(v.iloc[-third:].mean())
        vol_mom   = vol_late / vol_early if vol_early > 0 else 1.0

        # Intraday higher-lows: recent third's low above the middle third's low.
        nbar     = len(l)
        lo_mid   = float(l.iloc[nbar//3:2*nbar//3].min()) if nbar >= 6 else sess_l
        lo_late  = float(l.iloc[-(nbar//3):].min())       if nbar >= 6 else sess_l
        higher_lows = lo_late > lo_mid

        tail_above = float((c.iloc[-3:] > vwap.iloc[-3:]).mean())
        parabolic  = dist >= 8 and pos > 0.9
        pulling_back = dist > 0 and pos < 0.6 and higher_lows and slope_pct <= 0.02

        sigs = {
            "above_vwap":    1.0 if dist > 0 else (0.3 if dist > -0.5 else 0.0),
            "holds_vwap":    1.0 if above_frac >= 0.7 else (0.6 if above_frac >= 0.5 else 0.0),
            "rising":        1.0 if slope_pct > 0.03 else (0.5 if slope_pct >= 0 else 0.0),
            "range_pos":     1.0 if 0.60 <= pos <= 0.92 else (0.6 if pos > 0.92 else (0.4 if pos >= 0.45 else 0.0)),
            "vol_sustain":   1.0 if vol_mom >= 1.1 else (0.6 if vol_mom >= 0.8 else 0.2),
            "higher_lows":   1.0 if higher_lows else 0.0,
            "tail_strength": 1.0 if tail_above >= 0.66 else (0.5 if tail_above >= 0.33 else 0.0),
            "not_parabolic": 0.0 if parabolic else 1.0,
        }
        score = sig_score(sigs)

        if parabolic:
            phase = "Parabolic ⚠"
            score = min(score, 45)                      # cap conviction — exhaustion risk
        elif dist < -0.3 and slope_pct < 0:
            phase = "Fade ↓"
        elif dist <= 0 and slope_pct >= 0:
            phase = "Reclaim ↑"
        elif pulling_back:
            phase = "Pullback→VWAP"
        elif dist > 0 and pos >= 0.60 and above_frac >= 0.55 and slope_pct >= 0:
            phase = "Gap&Go ↑" if above_frac >= 0.80 else "Trend ↑"
        else:
            phase = "Range"

        meta = {"dist_vwap": round(dist, 2), "pos": round(pos, 2),
                "above_frac": round(above_frac, 2), "vol_mom": round(vol_mom, 2)}
        return score, phase, meta
    except Exception:
        return None, "No-Intraday", {}


def daily_continuation(df):
    """Fallback for when no intraday bars exist (overnight / pre-open): a daily-bar
    proxy for 'is this positioned to keep pushing up?'. Returns (score, phase)."""
    if df is None or len(df) < 20:
        return 0, "—"
    try:
        c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
        price = float(c.iloc[-1])
        e9s   = ema(c, 9);  e9 = float(e9s.iloc[-1]); e9_prev = float(e9s.iloc[-3])
        e20   = float(ema(c, 20).iloc[-1])
        rising9    = e9 > e9_prev
        upper_half = closes_upper_half(h, l, c, days=2)
        upvol      = up_vol_expansion(c, v, 10)
        day_chg    = (price - float(c.iloc[-2])) / float(c.iloc[-2]) * 100
        mom3       = (price - float(c.iloc[-4])) / float(c.iloc[-4]) * 100 if len(c) >= 4 else 0
        r          = rsi(c)
        near_brk   = price >= float(h.iloc[-21:-1].max()) * 0.98

        sigs = {
            "above_e9":       1.0 if price > e9 else 0.0,
            "above_e20":      1.0 if price > e20 else 0.0,
            "rising_e9":      1.0 if rising9 else 0.0,
            "closed_strong":  1.0 if upper_half else 0.0,
            "up_vol_expand":  1.0 if upvol else 0.0,
            "positive_day":   1.0 if day_chg > 0 else 0.0,
            "mom3_positive":  1.0 if mom3 > 0 else 0.0,
            "not_overbought": 1.0 if r < 78 else 0.0,
            "near_breakout":  1.0 if near_brk else 0.0,
        }
        score = sig_score(sigs)
        if price > e9 and rising9 and upper_half:
            phase = "Continuation ↑"
        elif price < e9 and price > e20:
            phase = "Pullback"
        elif price < e20:
            phase = "Weak"
        else:
            phase = "Base"
        return score, phase
    except Exception:
        return 0, "—"


# ──────────────────────────────────────────────
#  STRATEGY 1 — "HOP IN NOW" 1–10 SCORE + MOVE-TYPE CLASSIFIER
#  Distills the whole catalyst read into ONE number you can act on, plus a tag
#  for what kind of move it is (multi-day / all-day / premarket / postmarket / mix).
# ──────────────────────────────────────────────

def _et_hour(now_et=None):
    """Current US/Eastern time as a float hour (e.g. 9.5 = 9:30 AM)."""
    et  = pytz.timezone("America/New_York")
    now = now_et or datetime.now(et)
    return now.hour + now.minute / 60


def _session_tag(hour):
    """Which trading session the live quote belongs to, from the ET hour.
      PRE  4:00–9:30   |  LIVE 9:30–16:00 (regular)  |  AH 16:00–20:00 (post)
      CLOSED otherwise (overnight — last quote is the prior session's close)."""
    if 4.0 <= hour < 9.5:   return "PRE"
    if 9.5 <= hour < 16.0:  return "LIVE"
    if 16.0 <= hour < 20.0: return "AH"
    return "CLOSED"


def _fmt_vol(x):
    """Compact share-volume formatter: 2_000_000 → '2.0M', 850_000 → '850K'."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "—"
    if x <= 0:      return "—"
    if x >= 1e9:    return f"{x/1e9:.1f}B"
    if x >= 1e6:    return f"{x/1e6:.1f}M"
    if x >= 1e3:    return f"{x/1e3:.0f}K"
    return f"{x:.0f}"


def classify_move(hour, phase, imeta, pm, pm_relvol, near_brk, day_chg, mom3, rs, rvol_val):
    """Label the *character* of the move so you know what you're hopping into:
    a Multi-day runner (carries past today), an All-day trend (ride the session),
    a Premarket pop, a Postmarket (after-hours) pop, or a Mix. Reads the live ET
    session window together with the daily structure, the intraday VWAP read, and
    the premarket snapshot. Returns a short label string."""
    pm_gap     = float(pm["pm_gap"]) if pm else 0.0
    above_frac = imeta.get("above_frac", 0.0)
    vol_mom    = imeta.get("vol_mom", 1.0)
    pos        = imeta.get("pos", 0.5)
    has_intra  = bool(imeta)

    premarket_window  = 4.0 <= hour < 9.5
    afterhours_window = 16.0 <= hour < 20.0

    arche = {}

    # Multi-day runner — daily breakout + momentum + relative strength, still
    # holding (not exhausted): the kind of move that tends to carry into tomorrow.
    md = 0.0
    if near_brk:        md += 0.35
    if mom3 >= 5:       md += 0.25
    elif mom3 >= 2:     md += 0.12
    if (rs or 0) > 2:   md += 0.20
    elif (rs or 0) > 0: md += 0.10
    if rvol_val >= 2:   md += 0.10
    if has_intra and above_frac >= 0.6 and "Parabolic" not in phase:
        md += 0.10
    if "Parabolic" in phase or "Fade" in phase:
        md *= 0.5
    arche["Multi-day"] = min(md, 1.0)

    # All-day trend — intraday holds above VWAP across the session with buyers
    # still showing up late: rides the whole regular session.
    ad = 0.0
    if has_intra:
        if above_frac >= 0.70:   ad += 0.40
        elif above_frac >= 0.55: ad += 0.25
        if "Gap&Go" in phase or "Trend" in phase: ad += 0.25
        if vol_mom >= 1.0:       ad += 0.15
        if 0.50 <= pos <= 0.95:  ad += 0.10
        if "Fade" in phase:      ad *= 0.40
    arche["All-day"] = min(ad, 1.0)

    # Premarket pop — gap + premarket volume, weighted up while still pre-open.
    pmkt = 0.0
    if pm_gap >= 4:         pmkt += 0.40
    elif pm_gap >= 1.5:     pmkt += 0.20
    if pm_relvol >= 0.30:   pmkt += 0.30
    elif pm_relvol >= 0.10: pmkt += 0.15
    if premarket_window:    pmkt += 0.30
    arche["Premarket"] = min(pmkt, 1.0)

    # Postmarket pop — a fresh move during the after-hours window (earnings/news).
    ah = 0.0
    if afterhours_window:
        ah += 0.40
        if abs(pm_gap) >= 4:     ah += 0.35
        elif abs(pm_gap) >= 1.5: ah += 0.20
        if pm_relvol >= 0.10:    ah += 0.15
    arche["Postmarket"] = min(ah, 1.0)

    ranked = sorted(arche.items(), key=lambda kv: kv[1], reverse=True)
    (top, topv), (second, secv) = ranked[0], ranked[1]
    if topv < 0.25:
        return "Unclear"
    if secv >= 0.45 and secv >= topv - 0.15:
        return f"Mix:{top}+{second}"
    return top


def hop_in_scores(conf, base_conf, setupq, catq, phase):
    """Return (entry_score, strength_score), both on a 1–10 scale.

    entry_score    — 'should I hop in *right now*?'  Driven by continuation
                     conviction + setup/catalyst quality, then reality-checked on
                     entry TIMING: chasing a parabolic top or catching a fade is
                     capped LOW even when the move is huge. 10 = clean, reliable
                     entry you can lean on.
    strength_score — raw horsepower of the move, ignoring entry timing. A vertical
                     monster reads high here even while entry_score says don't
                     chase. The gap between them is the signal: high Strength +
                     low Score = 'great mover, wait for a pullback'.
    """
    composite = conf * 0.55 + setupq * 0.20 + catq * 0.25
    entry = composite / 10.0
    if "Parabolic" in phase:
        entry = min(entry, 4.0)        # chasing a vertical move — bad 'right now' entry
    elif "Fade" in phase:
        entry = min(entry, 3.0)        # rolling over, wrong direction
    elif phase == "Range":
        entry = min(entry, 6.0)        # no edge this second
    entry = int(max(1, min(10, round(entry))))

    strength = (base_conf * 0.50 + catq * 0.30 + setupq * 0.20) / 10.0
    if "Parabolic" in phase:
        strength = max(strength, 8.0)  # parabolic = maximum raw momentum
    strength = int(max(1, min(10, round(strength))))
    return entry, strength


def hop_verdict(score, phase):
    """One-glance call derived from the entry score + live phase."""
    if "Parabolic" in phase: return "⚠ Extended—don't chase"
    if "Fade" in phase:      return "⛔ Fading—avoid"
    if score >= 8:           return "🔥 Strong—hop in"
    if score >= 6:           return "✅ Decent—size in"
    if score >= 4:           return "⚠ Wait for confirm"
    return "⛔ Skip"


# ──────────────────────────────────────────────
#  STRATEGY 1: HIGH RVOL / CATALYST
# ──────────────────────────────────────────────

def s1_catalyst(data_map, info_map, pop_scores, bench_c=None, premarket=None, intraday=None, news=None, relax=False):
    results = []
    pop_pct = _pop_percentile_map(pop_scores)   # 0–100 percentile, computed once
    hour    = _et_hour()                         # live ET session window for move typing
    news_on = news is not None                   # was a news fetch actually attempted?
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 1) and not relax:
                continue
            if len(df) < 20:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price = float(c.iloc[-1])
            if not (0.50 <= price <= 10.0) and not relax:
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
            popp    = pop_pct.get(ticker, 0)            # popularity percentile (0-100)
            pm      = (premarket or {}).get(ticker)
            nw      = (news or {}).get(ticker)          # fresh-catalyst record (or None)

            # ── Catalyst BASE: is there a real, liquid move here at all? ──
            base_sigs = {
                "rvol":        1.0 if rv >= 2.5 else (0.5 if rv >= 1.5 else 0.0),
                "rsi_zone":    1.0 if 42 <= r <= 74 else 0.0,
                "big_move":    1.0 if abs(day_chg) >= 5 else (0.5 if abs(day_chg) >= 3 else 0.0),
                "breakout":    1.0 if near_brk else 0.0,
                "vol_spike":   1.0 if vspike >= 3 else (0.5 if vspike >= 2 else 0.0),
                "momentum":    1.0 if mom3 > 0 else 0.0,
                "bull_candle": 1.0 if (body_r > 0.55 and c.iloc[-1] > o.iloc[-1]) else 0.0,
                "popular":     1.0 if popp >= 80 else (0.5 if popp >= 50 else 0.0),
            }
            # Premarket confirmation folds into the base when a live snapshot exists.
            if pm:
                v20pm     = float(v.iloc[-21:-1].mean())
                pm_relvol = pm["pm_vol"] / v20pm if v20pm > 0 else 0
                base_sigs["pm_gap"]    = 1.0 if pm["pm_gap"] >= 4 else (0.5 if pm["pm_gap"] >= 1.5 else 0.0)
                base_sigs["pm_volume"] = 1.0 if pm_relvol >= 0.30 else (0.5 if pm_relvol >= 0.10 else 0.0)
            # Fresh-catalyst confirmation: a real, recent headline is what makes a
            # move trustworthy. When the news fetch ran, an unexplained spike (no
            # headline) scores 0 here — pulling its Confidence down on purpose.
            if news_on:
                if nw and nw["count"] >= 2 and nw["age_h"] <= 24:
                    base_sigs["news_catalyst"] = 1.0
                elif nw and nw["age_h"] <= 48:
                    base_sigs["news_catalyst"] = 0.7
                else:
                    base_sigs["news_catalyst"] = 0.0
            base_conf = sig_score(base_sigs)

            # ── CONVICTION: will it keep climbing? This now DRIVES Confidence, so the
            #    number means "how reliable is further upside today", not just "is it
            #    moving". Intraday 1-min read when available; daily proxy otherwise. ──
            cont_score, iphase, imeta = intraday_continuation((intraday or {}).get(ticker))
            if cont_score is not None:
                conf  = round(0.65 * cont_score + 0.35 * base_conf)
                phase = iphase                                   # live VWAP-based regime
            else:
                dcont, dphase = daily_continuation(df)
                conf  = round(0.60 * dcont + 0.40 * base_conf)
                phase = dphase                                   # daily-bar regime tag
                imeta = {}                                       # no intraday VWAP read

            if conf < 40 and not relax:
                continue

            setupq, _bphase, _meta = setup_quality_score(df, bench_c, popp)
            info = info_map.get(ticker, {})
            catq = catalyst_quality_score(info, c, h, l, o, v, bench_c, rv, price)
            rs   = rs_vs_benchmark(c, bench_c, periods=(5, 10)) if bench_c is not None else None

            # ── LIVE price: the daily close is yesterday's; the Polygon snapshot
            #    carries the real pre/post-market last trade. Show that as the entry
            #    price (and base the trade levels on it) so Price isn't stale, while
            #    the SETUP reads (RVOL/breakout/ATR) still come off completed bars. ──
            live_px   = float(pm["pm_price"]) if (pm and pm.get("pm_price")) else price
            shown_chg = float(pm["pm_gap"]) if pm else day_chg   # today's change so far

            target = round(live_px * (1 + atp / 100 * 2.2), 2)
            stop   = round(live_px * (1 - atp / 100 * 0.9), 2)
            rr     = round((target - live_px) / (live_px - stop), 2) if live_px > stop else 0

            # ── The two headline numbers ──
            entry_score, strength_score = hop_in_scores(conf, base_conf, setupq, catq, phase)
            verdict = hop_verdict(entry_score, phase)

            # ── What KIND of move is this? ──
            pm_relvol = 0.0
            if pm:
                v20pm     = float(v.iloc[-21:-1].mean())
                pm_relvol = pm["pm_vol"] / v20pm if v20pm > 0 else 0.0
            move_type = classify_move(hour, phase, imeta, pm, pm_relvol,
                                      near_brk, day_chg, mom3, rs, rv)

            # ── WHY, in plain English (no jargon — for newer traders) ──
            why = []
            if near_brk:                          why.append("at new highs")
            if rv >= 2.5:                         why.append(f"{rv:.0f}× normal volume")
            elif rv >= 1.5:                       why.append(f"{rv:.1f}× volume")
            if pm and pm["pm_gap"] >= 1.5:        why.append(f"gapped +{pm['pm_gap']:.0f}%")
            if nw and nw.get("count"):            why.append("fresh news")
            if (rs or 0) > 2:                     why.append("beating the market")
            if imeta.get("above_frac", 0) >= 0.7: why.append("holding its gains")
            if "Pullback" in phase or "Reclaim" in phase: why.append("buyable dip")
            why_str = ", ".join(why[:4]) if why else "just elevated volume"

            # News cell — fresh catalyst at a glance.
            if nw:
                news_cell = f"🗞{nw['count']}·{nw['age_h']:.0f}h"
            elif news_on:
                news_cell = "none ⚠"
            else:
                news_cell = "—"

            # Pre/Post cell — the EXACT live extended-hours quote, stamped with the
            # session it belongs to (PRE / LIVE / AH). Separate from Price so you see
            # the real-time print on its own. '—' when no live snapshot is available.
            if pm and pm.get("pm_price"):
                prepost_cell = f"{_session_tag(hour)} ${live_px:.2f} ({shown_chg:+.0f}%)"
            else:
                prepost_cell = "—"

            # Live volume cell — shares traded SO FAR this session (premarket /
            # current day / after-hours), plus what fraction of a normal full day
            # that already is (e.g. '2.0M 40%d' = 40% of avg daily volume pre-open).
            if pm and pm.get("pm_vol"):
                livevol_cell = f"{_fmt_vol(pm['pm_vol'])} {pm_relvol*100:.0f}%d"
            else:
                livevol_cell = "—"

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}",
                LivePrice=prepost_cell, LiveVol=livevol_cell,
                Score=entry_score, Strength=strength_score,
                Verdict=verdict, Type=move_type, News=news_cell, Phase=phase,
                RVOL=f"{rv}x", DayChg=f"{shown_chg:+.1f}%",
                Target=f"${target}", Stop=f"${stop}", RR=f"1:{rr}",
                Why=why_str,
                # Sort by the entry score you actually act on; composite breaks ties.
                _score=(conf * 0.55 + setupq * 0.20 + catq * 0.25),
            ))
        except Exception:
            continue

    results.sort(key=lambda x: (x["Score"], x["_score"]), reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 2: MOMENTUM SWING
# ──────────────────────────────────────────────

def s2_swing(data_map, info_map, pop_scores, bench_c=None, premarket=None, relax=False):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 2) and not relax:
                continue
            if len(df) < 55:
                continue
            c, v, h, l = df["Close"], df["Volume"], df["High"], df["Low"]
            price = float(c.iloc[-1])
            if price < 5 and not relax:
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
            pm   = (premarket or {}).get(ticker)

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

            # Live premarket strength — confirms the swing isn't gapping down today.
            if pm:
                sigs["pm_strength"] = 1.0 if pm["pm_gap"] >= 1 else (0.0 if pm["pm_gap"] <= -2 else 0.5)

            conf = sig_score(sigs)
            if conf < 45 and not relax:
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
                PM_Gap=f"{pm['pm_gap']:+.1f}%" if pm else "—",
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

def s3_breakout(data_map, info_map, pop_scores, bench_c=None, premarket=None, relax=False):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 3) and not relax:
                continue
            if len(df) < 30:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price   = float(c.iloc[-1])
            if price < 1 and not relax:
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
            pm      = (premarket or {}).get(ticker)

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

            # Live premarket gap + volume — the breakout's freshest confirmation,
            # which the prior daily bar can't see. Scored only when a snapshot exists.
            if pm:
                v20pm     = float(v.iloc[-21:-1].mean())
                pm_relvol = pm["pm_vol"] / v20pm if v20pm > 0 else 0
                sigs["pm_gap"]    = 1.0 if pm["pm_gap"] >= 3 else (0.5 if pm["pm_gap"] >= 1 else 0.0)
                sigs["pm_volume"] = 1.0 if pm_relvol >= 0.25 else (0.5 if pm_relvol >= 0.08 else 0.0)

            conf   = sig_score(sigs)
            if conf < 42 and not relax:
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
                Gap=f"{gap_pct:+.1f}%",
                PM_Gap=f"{pm['pm_gap']:+.1f}%" if pm else "—",
                RVOL=f"{rv}x",
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
#  STRATEGY 4: QUALITY COMPOUNDER  (4–6 month hold)
#
#  Replaces the old earnings-volatility setup. Hunts for *good companies* in
#  confirmed uptrends that you can hold for a season, not a day: consistent
#  growth (the "catalyst" is the business itself), healthy margins/ROE/cash
#  flow, sane valuation, analyst support, insider/institutional backing — with
#  realistic (not explosive) volatility and genuine liquidity. The opposite
#  end of the risk spectrum from Strategy 1.
# ──────────────────────────────────────────────

def _fmt_pct(x, signed=False, scale=100.0):
    """Format a fraction (0.12 → '12%') for display, or '—' when missing."""
    if x is None:
        return "—"
    try:
        v = float(x) * scale
    except (TypeError, ValueError):
        return "—"
    return f"{v:+.0f}%" if signed else f"{v:.0f}%"


def _analyst_view(info, price):
    """Pull yfinance analyst consensus into (label, mean, n_analysts, upside%, target).
    recommendationMean is 1.0 (Strong Buy) → 5.0 (Strong Sell)."""
    mean = info.get("recommendationMean")
    n    = int(info.get("numberOfAnalystOpinions") or 0)
    key  = (info.get("recommendationKey") or "").replace("_", " ").title() or "—"
    tgt  = info.get("targetMeanPrice") or info.get("targetMedianPrice")
    upside = (float(tgt) - price) / price * 100 if (tgt and price > 0) else None
    label = f"{key}({mean:.1f})" if isinstance(mean, (int, float)) else key
    return label, mean, n, upside, (float(tgt) if tgt else None)


def s4_entry_timing(price, e50, e200, rsi_val, pos52):
    """Judge whether a quality name is at a SANE entry or already extended near a
    peak. Great businesses are often spotted only *after* they've run far above
    their 50-day — and buying there means buying the top right before a pullback.

    Returns (label, multiplier, ext50%):
      🟢 Good entry  — pulled back to / sitting on the rising 50d, still in uptrend
      ⚪ Fair        — modestly above the 50d, normal
      🟡 Extended    — stretched; better to wait for a dip
      🔴 Peak risk   — very stretched + overbought + pinned to 52w highs

    The multiplier down-ranks extended names so the list favors good companies you
    can still buy with room to run, not ones you'd be chasing at the very top.
    """
    ext50 = (price / e50 - 1) * 100 if e50 else 0.0

    very_ext = ext50 > 22 or (pos52 >= 97 and rsi_val >= 75)
    extended = ext50 > 12 or (pos52 >= 92 and rsi_val >= 72)
    pullback = (-9 <= ext50 <= 4) and (price > e200) and (rsi_val < 68)

    if very_ext:
        return "🔴 Peak risk", 0.76, ext50
    if extended:
        return "🟡 Extended", 0.90, ext50
    if pullback:
        return "🟢 Good entry", 1.10, ext50
    return "⚪ Fair", 1.0, ext50


def s4_quality_compounder(data_map, info_map, pop_scores, bench_c=None, relax=False):
    """4–6 month positional holds: quality businesses in Stage-2 uptrends with
    consistent growth, analyst support, and realistic volatility."""
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 4) and not relax:
                continue
            if len(df) < 130:          # need ~6 months of history for the trend/momentum reads
                continue
            c, v, h, l = df["Close"], df["Volume"], df["High"], df["Low"]
            price = float(c.iloc[-1])

            # ── Gate: real company, real liquidity — but room for smaller, livelier
            #    growth names. We want *some* risk here, not just sleepy mega-caps. ──
            info       = info_map.get(ticker, {})
            cap        = info.get("marketCap", 0) or 0
            avg_vol    = float(v.iloc[-21:-1].mean())
            dollar_vol = price * avg_vol
            if not relax:
                if price < 5:                 continue   # contrasts with S1's <$10 band
                if cap < 300e6:               continue   # small-cap+ allowed → more upside/volatility
                if dollar_vol < 3e6:          continue   # ≥ $3M/day traded = still exitable
                # Decent-business floor (NOT a profitability mandate): only drop names
                # whose top line is actively shrinking. Unprofitable fast growers stay.
                _rg = info.get("revenueGrowth")
                if _rg is not None and _rg < -0.10:
                    continue

            # ── Trend / momentum (the "is it working?" half) ──
            e50    = float(ema(c, 50).iloc[-1])
            e200   = float(ema(c, 200).iloc[-1]) if len(c) >= 200 else e50
            r      = rsi(c)
            stage  = weinstein_stage(c)
            m3m    = (price / float(c.iloc[-63])  - 1) * 100 if len(c) >= 63  else 0.0
            m6m    = (price / float(c.iloc[-126]) - 1) * 100 if len(c) >= 126 else 0.0
            rs     = rs_vs_benchmark(c, bench_c, periods=(63, 126))   # medium-term relative strength
            ext50  = (price / e50 - 1) * 100                          # how stretched above the 50d
            h52    = float(h.rolling(252).max().iloc[-1]) if len(h) >= 252 else float(h.max())
            l52    = float(l.rolling(252).min().iloc[-1]) if len(l) >= 252 else float(l.min())
            pos52  = (price - l52) / (h52 - l52) * 100 if h52 > l52 else 50.0
            obvs   = obv_slope(c, v, 40)                              # 40d accumulation slope

            # ── Fundamentals (the "is it a good business?" half) ──
            rev_g   = info.get("revenueGrowth")
            earn_g  = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
            margin  = info.get("profitMargins")
            roe     = info.get("returnOnEquity")
            d2e     = info.get("debtToEquity")          # yfinance reports this as a percent (80 ⇒ 0.8×)
            fcf     = info.get("freeCashflow")
            peg     = info.get("pegRatio")
            fpe     = info.get("forwardPE") or info.get("trailingPE")
            beta    = info.get("beta")
            inst    = info.get("heldPercentInstitutions")
            insider = info.get("heldPercentInsiders")
            short_f = info.get("shortPercentOfFloat")

            label, rmean, n_an, upside, a_target = _analyst_view(info, price)

            # Fundamental quality sub-score (0–100), shown as its own column.
            # "Decent, not strict": profitability is a *plus*, never a hard zero, so a
            # reinvesting hyper-grower (negative FCF/ROE) still earns a respectable floor.
            fund = {
                "rev_growth":  1.0 if (rev_g  and rev_g  > 0.25) else (0.6 if (rev_g  and rev_g  > 0.10) else (0.3 if (rev_g and rev_g > 0)  else 0.0)),
                "earn_growth": 1.0 if (earn_g and earn_g > 0.20) else (0.6 if (earn_g and earn_g > 0.0)  else (0.3 if earn_g is None      else 0.1)),
                "margins":     1.0 if (margin and margin > 0.12) else (0.6 if (margin and margin > 0.0)  else 0.3),
                "roe":         1.0 if (roe    and roe    > 0.15) else (0.6 if (roe    and roe    > 0.05) else 0.3),
                "balance":     1.0 if (d2e is not None and d2e < 100) else (0.6 if (d2e is not None and d2e < 200) else (0.4 if d2e is None else 0.2)),
                "cash_flow":   1.0 if (fcf and fcf > 0) else 0.3,
                "valuation":   1.0 if (peg and 0 < peg <= 1.5) else (0.7 if (peg and peg <= 2.5) else (0.5 if (fpe and 0 < fpe <= 50) else (0.4 if (peg is None and fpe is None) else 0.2))),
            }
            quality_q = sig_score(fund)

            # Consistent catalyst = the growth is broad-based (both top- and bottom-line).
            consistent = bool(rev_g and rev_g > 0.05 and earn_g and earn_g > 0)

            sigs = {
                # Trend / structure
                "stage2":        1.0 if stage == "Stage2" else (0.4 if stage == "Stage1" else 0.0),
                "above_e200":    1.0 if price > e200 else 0.0,
                "ema_stack":     1.0 if e50 > e200 else 0.0,
                "good_entry":    1.0 if 0 <= ext50 <= 15 else (0.5 if -8 <= ext50 < 0 else (0.2 if ext50 > 30 else 0.4)),
                "rsi_room":      1.0 if 45 <= r <= 72 else (0.6 if (40 <= r < 45 or 72 < r <= 80) else 0.2),
                # Momentum / risk-on — more signals here = more weight on "it's moving"
                "mom_3m":        1.0 if 8 <= m3m <= 70 else (0.5 if 0 < m3m < 8 else 0.0),
                "mom_6m":        1.0 if 15 <= m6m <= 120 else (0.6 if 0 < m6m < 15 else 0.0),
                "rs_leader":     (1.0 if rs > 8 else (0.7 if rs > 0 else 0.0)) if rs is not None else 0.4,
                "accumulation":  1.0 if obvs > 0.2 else (0.5 if obvs > 0 else 0.0),
                "near_highs":    1.0 if pos52 >= 60 else (0.5 if pos52 >= 40 else 0.0),
                # Growth / high-potential — the upside tilt (rewards explosive growers)
                "rev_growth":    fund["rev_growth"],
                "earn_growth":   fund["earn_growth"],
                "hypergrowth":   1.0 if ((rev_g and rev_g > 0.35) or (earn_g and earn_g > 0.40)) else (0.5 if (rev_g and rev_g > 0.20) else 0.0),
                "consistent":    1.0 if consistent else 0.0,
                # Decent-business floor (forgiving — see `fund`)
                "margins":       fund["margins"],
                "roe":           fund["roe"],
                "balance":       fund["balance"],
                "valuation":     fund["valuation"],
                # Conviction / sponsorship
                "analyst_buy":   (1.0 if rmean <= 2.0 else (0.6 if rmean <= 2.5 else (0.3 if rmean <= 3.0 else 0.0))) if isinstance(rmean, (int, float)) else 0.3,
                "analyst_upside":(1.0 if upside > 20 else (0.7 if upside > 8 else (0.4 if upside > 0 else 0.0))) if upside is not None else 0.3,
                "coverage":      1.0 if n_an >= 5 else (0.5 if n_an >= 2 else 0.0),
                "institutional": 1.0 if (inst and 0.40 <= inst <= 0.95) else (0.5 if (inst and inst > 0) else 0.0),
                "insider_skin":  1.0 if (insider and insider > 0.05) else (0.5 if (insider and insider > 0.01) else 0.0),
                # Volatility is WELCOME here — reward tradeable movement, don't punish it
                "tradeable_vol": 1.0 if (beta and 1.0 <= beta <= 2.2) else (0.7 if (beta and 0.7 <= beta < 1.0) else (0.5 if (beta and beta > 2.2) else (0.4 if beta else 0.4))),
            }

            conf = sig_score(sigs)
            if conf < 50 and not relax:
                continue

            # ── Best-effort next-earnings date (only for names that already qualify,
            #     so we make very few network calls). A near catalyst is a small plus. ──
            earn_in = None
            if not relax:
                try:
                    cal = yf.Ticker(ticker).calendar
                    ed  = None
                    if cal is not None and not getattr(cal, "empty", True):
                        if "Earnings Date" in cal.columns:
                            ed = pd.to_datetime(cal["Earnings Date"].iloc[0])
                        elif hasattr(cal, "T") and "Earnings Date" in cal.T.columns:
                            ed = pd.to_datetime(cal.T["Earnings Date"].iloc[0])
                    if ed is not None:
                        earn_in = (ed.date() - datetime.now().date()).days
                except Exception:
                    pass

            # Target blends analyst consensus with a momentum-implied move. Cap is
            # wider than a sleepy compounder's — these can run, and we want the upside.
            mom_target = price * (1 + max(0.12, min(0.60, m6m / 100 * 0.6 + 0.15)))
            target     = round((a_target + mom_target) / 2, 2) if a_target else round(mom_target, 2)
            tgt_pct    = (target / price - 1) * 100
            stop       = round(min(e50, price * 0.85), 2)   # ride the 50d / give it room to breathe

            cap_lbl = ("Mega" if cap >= 200e9 else "Large" if cap >= 10e9
                       else "Mid" if cap >= 2e9 else "Small")
            sector  = (info.get("sector") or "—")[:14]

            # ── Entry-timing guard: are we buying with room, or chasing the peak? ──
            entry_lbl, entry_mult, ext50 = s4_entry_timing(price, e50, e200, r, pos52)

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}", Cap=cap_lbl, Sector=sector,
                Entry=entry_lbl, vsE50=f"{ext50:+.0f}%",
                RevGr=_fmt_pct(rev_g, signed=True), EarnGr=_fmt_pct(earn_g, signed=True),
                NetMgn=_fmt_pct(margin), ROE=_fmt_pct(roe), PEG=(f"{peg:.1f}" if peg else "—"),
                Rating=label, Upside=(f"{upside:+.0f}%" if upside is not None else "—"),
                Analysts=n_an, Insider=_fmt_pct(insider), Inst=_fmt_pct(inst),
                ShortFlt=_fmt_pct(short_f), Beta=(f"{beta:.2f}" if beta else "—"),
                Trend=("Stage2" if stage == "Stage2" else ("Up" if price > e200 else "Weak")),
                Mom3M=f"{m3m:+.0f}%", Mom6M=f"{m6m:+.0f}%",
                RS=(f"{rs:+.0f}" if rs is not None else "—"),
                Pos52=f"{pos52:.0f}%", QualityQ=quality_q,
                EarnIn=(f"{earn_in}d" if earn_in is not None else "—"),
                Target=f"${target}(+{tgt_pct:.0f}%)", Stop=f"${stop}", Timeframe="4–6 mo",
                Confidence=conf,
                # Down-rank extended names so the top of the list isn't peak-chasing.
                _score=(conf * 0.8 + quality_q * 0.2) * entry_mult,
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]


# ──────────────────────────────────────────────
#  STRATEGY 5: OVERSOLD REVERSAL HUNTER
# ──────────────────────────────────────────────

def s5_oversold_reversal(data_map, info_map, pop_scores, relax=False):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 5) and not relax:
                continue
            if len(df) < 40:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price = float(c.iloc[-1])
            if price < 2 and not relax:
                continue

            # — was it oversold? (gate: currently OR within last 10 days)
            r_ser = rsi_series(c)
            r = float(r_ser.iloc[-1])
            rsi_min_10d = float(r_ser.iloc[-10:].min()) if len(r_ser) >= 10 else r
            if not (r < 40 or rsi_min_10d < 32) and not relax:
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
            if rev_count < 2 and not relax:
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
            if conf < 42 and not relax:
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

def s6_sector_rotation(data_map, info_map, pop_scores, relax=False):
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
            if _is_blocked(ticker, info_map, 6) and not relax:
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
            if mapped not in top_sectors and not relax:
                continue
            if len(df) < 30:
                continue
            c, v, h, l = df["Close"], df["Volume"], df["High"], df["Low"]
            price = float(c.iloc[-1])
            if price < 2 and not relax:
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
            if conf < 45 and not relax:
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

def s7_orb(data_map, info_map, pop_scores, bench_c=None, relax=False):
    results = []
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 7) and not relax:
                continue
            if len(df) < 21:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price   = float(c.iloc[-1])
            if price < 1 and not relax:
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
            if conf < 45 and not relax:
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
#  STRATEGY 8: POWER SWING  (1–2 week hold)
#
#  Fills the gap between S1 (intraday) and S2 (4–8 wks). HYBRID — fires on
#  whichever short-term setup is present:
#    • Pullback bounce  — uptrend dips to a rising 9/20 EMA, then turns up
#    • Breakout follow  — tight 5–10d range breaks out on a volume surge
#  Tight 1–2 week targets (≈5–14%), small-mid-cap friendly (price ≥ $3).
# ──────────────────────────────────────────────

def s8_power_swing(data_map, info_map, pop_scores, bench_c=None, relax=False):
    results = []
    pop_pct = _pop_percentile_map(pop_scores)
    for ticker, df in data_map.items():
        try:
            if _is_blocked(ticker, info_map, 8) and not relax:
                continue
            if len(df) < 35:
                continue
            c, v, h, l, o = df["Close"], df["Volume"], df["High"], df["Low"], df["Open"]
            price = float(c.iloc[-1])
            if price < 3 and not relax:        # small-mid friendly, skips sub-$3 noise
                continue

            e9s  = ema(c, 9);  e9  = float(e9s.iloc[-1]);  e9p  = float(e9s.iloc[-3])
            e20s = ema(c, 20); e20 = float(e20s.iloc[-1]); e20p = float(e20s.iloc[-3])
            e50  = float(ema(c, 50).iloc[-1])
            r    = rsi(c)
            a    = atr(h, l, c); atp = (a / price) * 100
            rv   = rvol(v)
            mom5  = (price - float(c.iloc[-6]))  / float(c.iloc[-6])  * 100 if len(c) >= 6  else 0
            mom10 = (price - float(c.iloc[-11])) / float(c.iloc[-11]) * 100 if len(c) >= 11 else 0

            uptrend     = e9 > e20 and price > e50 and e20 >= e20p
            rising_emas = e9 >= e9p and e20 >= e20p
            day_up      = price > float(c.iloc[-2]) and price > float(o.iloc[-1])
            upper_half  = closes_upper_half(h, l, c, days=1)
            dist_e9     = (price - e9)  / e9  * 100
            dist_e20    = (price - e20) / e20 * 100

            # — Pullback bounce: hugging a rising 20-EMA from just below/above, turning up
            pulled_back = -1.5 * atp <= dist_e20 <= 2.0
            bounce      = uptrend and rising_emas and pulled_back and day_up and 40 <= r <= 62

            # — Breakout follow-through: tight base breaks prior-day & 10-day high on volume
            hi10  = float(h.iloc[-11:-1].max())
            rng10 = (hi10 - float(l.iloc[-11:-1].min())) / price if price > 0 else 1
            tight = rng10 < 0.14
            breakout = price >= hi10 * 0.995 and price > float(h.iloc[-2]) and rv >= 1.3 and upper_half

            if not (bounce or breakout) and not relax:
                continue
            setup = ("Pull+Brk" if (bounce and breakout)
                     else "Pullback" if bounce
                     else "Breakout" if breakout else "—")

            rs  = rs_vs_benchmark(c, bench_c, periods=(10, 21))
            pop = pop_scores.get(ticker, 0)

            sigs = {
                "uptrend":       1.0 if uptrend else 0.0,
                "rising_emas":   1.0 if rising_emas else 0.0,
                "above_e50":     1.0 if price > e50 else 0.0,
                "setup_present": 1.0 if (bounce or breakout) else 0.0,
                "bounce":        1.0 if bounce else 0.0,
                "breakout":      1.0 if breakout else 0.0,
                "day_strength":  1.0 if (day_up and upper_half) else (0.5 if day_up else 0.0),
                "rsi_zone":      1.0 if 45 <= r <= 65 else (0.5 if 40 <= r <= 70 else 0.0),
                "mom10_pos":     1.0 if mom10 > 0 else 0.0,
                "rvol_ok":       1.0 if rv >= 1.3 else (0.5 if rv >= 1.0 else 0.0),
                "rs_leader":     (1.0 if rs > 3 else (0.6 if rs > 0 else 0.0)) if rs is not None else 0.4,
                "tight_or_pb":   1.0 if (tight or pulled_back) else 0.0,
                "popular":       1.0 if pop_pct.get(ticker, 0) >= 60 else (0.5 if pop_pct.get(ticker, 0) >= 30 else 0.0),
            }
            conf = sig_score(sigs)
            if conf < 48 and not relax:
                continue

            setupq, phase, _meta = setup_quality_score(df, bench_c, pop)

            # Tight 1–2 week target: ~1.6× ATR move, clamped to a realistic 5–14%.
            tgt_pct = max(5, min(14, atp * 1.6))
            target  = round(price * (1 + tgt_pct / 100), 2)
            stop    = round(min(e20, price * (1 - atp / 100 * 1.1)), 2)
            rr      = round((target - price) / (price - stop), 2) if price > stop else 0

            info     = info_map.get(ticker, {})
            cap      = info.get("marketCap", 0)
            cap_lbl  = "Micro" if cap < 300e6 else ("Small" if cap < 2e9 else ("Mid" if cap < 10e9 else "Large"))
            industry = info.get("industry", "—")

            results.append(dict(
                Ticker=ticker, Price=f"${price:.2f}", Setup=setup,
                RSI=round(r, 1), vsEMA9=f"{dist_e9:+.1f}%", vsEMA20=f"{dist_e20:+.1f}%",
                Mom5=f"{mom5:+.1f}%", Mom10=f"{mom10:+.1f}%", RVOL=f"{rv}x",
                RS=(f"{rs:+.0f}" if rs is not None else "—"), Cap=cap_lbl,
                Phase=phase, SetupQ=setupq,
                Target=f"${target}(+{tgt_pct:.0f}%)", Stop=f"${stop}", RR=f"1:{rr}",
                Timeframe="1–2 wks", PopScore=pop_pct.get(ticker, 0),
                Confidence=conf, Industry=industry,
                _score=(conf * 0.6 + setupq * 0.4),
            ))
        except Exception:
            continue

    results.sort(key=lambda x: x["_score"], reverse=True)
    [r.pop("_score") for r in results]
    return results[:20]

