"""Pure technical indicators + quality-scoring math. No I/O, no console —
just numbers in, numbers out, so these stay fast and trivially testable."""

import numpy as np
import pandas as pd


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


def breakout_phase(c, h, l, v=None, lookback=20, window=10):
    """Tag where the stock is in the breakout process.
    Base        — still under a tight pivot, primed
    Breakout    — broke pivot in last 1-2 days ON VOLUME (best entry)
    Breakout?   — broke pivot but volume did NOT confirm (suspect — fails often)
    Continuation— 3-5 days post-breakout, holding
    Extended    — far above pivot, chasing risk
    Failed      — broke pivot then fell back below

    Volume confirmation: a genuine breakout expands volume on the breakout bar
    vs its trailing 50-day average. An unconfirmed (low-volume) breakout is
    statistically far more likely to fail, so it is downgraded to 'Breakout?'.
    Pass `v` (the Volume series) to enable this; omit it for the legacy
    price-only behavior."""
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

    # Volume confirmation on the breakout bar (RVOL >= 1.5 vs trailing 50d avg).
    vol_confirmed = True
    if v is not None and len(v) == len(c):
        brk_idx = len(c) - window + first_break   # absolute index of breakout bar
        if 0 <= brk_idx < len(v):
            trail = v.iloc[max(0, brk_idx - 50):brk_idx]
            avg   = float(trail.mean()) if len(trail) else 0.0
            brk_v = float(v.iloc[brk_idx])
            vol_confirmed = avg > 0 and (brk_v / avg) >= 1.5

    if days_since <= 1:
        return "Breakout" if vol_confirmed else "Breakout?"
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
        phase     = breakout_phase(c, h, l, v, 20, 10)
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
            "Phase":    10 if phase == "Breakout" else (8 if phase == "Continuation" else (6 if phase == "Base" else (4 if phase == "Breakout?" else (2 if phase == "Extended" else 0)))),
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


def intraday_phase(df_min):
    """Intraday phase model for low-priced catalyst/momentum names (Strategy 1).

    A SEPARATE model from the daily breakout_phase: catalyst runners live on an
    intraday timescale (gap → VWAP → run → exhaustion), not a 20-day base. Built
    on 1-minute bars + session VWAP. Returns one of:
      Gap&Go       — opened strong, held above VWAP, sitting high in the day's range
      VWAP-Reclaim — spent the session below VWAP but reclaimed it and is rising
      Parabolic    — stretched far above VWAP and pinned to highs (exhaustion risk)
      Fade         — below VWAP and rolling over (avoid)
      Range        — chopping around VWAP, no edge
      Unknown      — insufficient data
    """
    if df_min is None or len(df_min) < 15:
        return "Unknown"
    try:
        o, h, l = df_min["Open"], df_min["High"], df_min["Low"]
        c, v    = df_min["Close"], df_min["Volume"]
        price   = float(c.iloc[-1])
        if price <= 0:
            return "Unknown"

        tp     = (h + l + c) / 3.0
        cum_v  = v.cumsum()
        vwap   = (tp * v).cumsum() / cum_v.replace(0, np.nan)
        vw     = float(vwap.iloc[-1])
        if not np.isfinite(vw) or vw <= 0:
            return "Unknown"

        dist   = (price - vw) / vw * 100              # % above/below VWAP
        sess_h = float(h.max())
        sess_l = float(l.min())
        rng    = sess_h - sess_l
        pos    = (price - sess_l) / rng if rng > 0 else 0.5   # position in day range
        above_vwap_frac = float((c > vwap).mean())            # share of day above VWAP

        seg    = c.iloc[-15:].values.astype(float)            # recent ~15-min slope
        slope  = float(np.polyfit(np.arange(len(seg)), seg, 1)[0]) if len(seg) >= 5 else 0.0
        slope_pct = slope / price * 100 if price > 0 else 0.0

        if dist >= 8 and pos > 0.85:
            return "Parabolic"
        if dist < -1 and slope_pct < 0:
            return "Fade"
        if above_vwap_frac < 0.5 and dist > 0 and slope_pct >= 0:
            return "VWAP-Reclaim"
        if dist >= 0 and pos >= 0.55 and above_vwap_frac >= 0.5:
            return "Gap&Go"
        return "Range"
    except Exception:
        return "Unknown"

