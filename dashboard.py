"""
NASDAQ Screener — Interactive Dashboard
=======================================
Streamlit front-end for nasdaq_v3.py.

Run with:
    streamlit run dashboard.py

Two tabs:
  • Run Analysis — build the popularity universe and run the 7 strategies,
    showing each strategy's ranked picks in sortable tables.
  • Rate a Ticker — score a single ticker against all 7 strategies (with
    entry gates relaxed) and report which strategy it fits best.
"""

import sys

# nasdaq_v3 uses `rich` for colorful console output. On Windows the default
# cp1252 stdout can't encode the emoji/box characters it prints, which would
# crash the screener mid-run. Force UTF-8 before importing it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pandas as pd
import streamlit as st
import yfinance as yf

import nasdaq_v3 as ns

# Interactive grid with hover-chart tooltips. Optional — if not installed the
# app falls back to the plain styled dataframe (no hover charts).
try:
    from st_aggrid import AgGrid, GridOptionsBuilder, JsCode
    HAS_AGGRID = True
    AGGRID_ERR = ""
except Exception as _e:
    HAS_AGGRID = False
    AGGRID_ERR = f"{type(_e).__name__}: {_e}"

st.set_page_config(page_title="NASDAQ Screener", page_icon="📈", layout="wide")

# ──────────────────────────────────────────────
#  STRATEGY REGISTRY
#  key -> (label, subtitle, color, runner(data_map, info_map, pop, bench, pm, relax))
# ──────────────────────────────────────────────

def _run_s1(dm, im, pop, bench, pm, relax):
    return ns.s1_catalyst(dm, im, pop, bench_c=bench, premarket=pm, relax=relax)

def _run_s2(dm, im, pop, bench, pm, relax):
    return ns.s2_swing(dm, im, pop, bench_c=bench, premarket=pm, relax=relax)

def _run_s3(dm, im, pop, bench, pm, relax):
    return ns.s3_breakout(dm, im, pop, bench_c=bench, premarket=pm, relax=relax)

def _run_s4(dm, im, pop, bench, pm, relax):
    return ns.s4_quality_compounder(dm, im, pop, bench_c=bench, relax=relax)

def _run_s5(dm, im, pop, bench, pm, relax):
    return ns.s5_oversold_reversal(dm, im, pop, relax=relax)

def _run_s6(dm, im, pop, bench, pm, relax):
    # s6 returns (sector_leaderboard, stock_rows); the rater only needs the stocks.
    out = ns.s6_sector_rotation(dm, im, pop, relax=relax)
    if isinstance(out, tuple) and len(out) == 2:
        return out[1]
    return []

def _run_s7(dm, im, pop, bench, pm, relax):
    return ns.s7_orb(dm, im, pop, bench_c=bench, relax=relax)

def _run_s8(dm, im, pop, bench, pm, relax):
    return ns.s8_power_swing(dm, im, pop, bench_c=bench, relax=relax)

STRATEGIES = {
    1: ("Strategy 1 — Catalyst / High RVOL",
        "Price <$10 | RVOL >2x | explosive intraday | premarket gap+volume",
        _run_s1),
    2: ("Strategy 2 — Momentum Swing",
        "EMA stacked | RSI 50–68 | higher highs + higher lows",
        _run_s2),
    3: ("Strategy 3 — Gap & Breakout",
        "Premarket + daily gap | flat-base break | volume confirm",
        _run_s3),
    4: ("Strategy 4 — Quality Growth (4–6 Month Hold)",
        "Stage-2 uptrend + momentum | strong growth, decent (not strict) fundamentals | analyst + insider/institutional backing | tradeable volatility welcome",
        _run_s4),
    5: ("Strategy 5 — Oversold Reversal",
        "Was oversold + ≥2 reversal signs (RSI↑/divergence/MACD↑/EMA reclaim…)",
        _run_s5),
    6: ("Strategy 6 — Sector Rotation",
        "Stocks inside the strongest rotating sectors | EMA aligned",
        _run_s6),
    7: ("Strategy 7 — Opening Range Breakout (ORB)",
        "Gap held | above open | volume surging | first 30-min high broken",
        _run_s7),
    8: ("Strategy 8 — Power Swing (1–2 Week Hold)",
        "Hybrid: pullback bounce to rising 9/20 EMA OR tight-base breakout on volume | small-mid friendly | tight 5–14% targets",
        _run_s8),
}


# ──────────────────────────────────────────────
#  CACHED HELPERS
# ──────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def get_benchmark():
    """SPY close series for relative-strength scoring. Cached for an hour."""
    try:
        df = yf.download("SPY", period="1y", interval="1d",
                         auto_adjust=True, progress=False)
        if df is not None and len(df) > 60:
            return df["Close"]
    except Exception:
        pass
    return None


def style_table(rows):
    """Turn a list of strategy row-dicts into a styled DataFrame for display."""
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "Score" in df.columns:                    # Strategy 1 — 1–10 hop-in score
        def _color10(v):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return ""
            if v >= 8:
                return "background-color: #1b5e20; color: white"
            if v >= 5:
                return "background-color: #f9a825; color: black"
            return "color: #9e9e9e"
        subset = [c for c in ("Score", "Strength") if c in df.columns]
        return df.style.map(_color10, subset=subset)
    if "Confidence" in df.columns:
        def _color(v):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return ""
            if v >= 75:
                return "background-color: #1b5e20; color: white"
            if v >= 55:
                return "background-color: #f9a825; color: black"
            return "color: #9e9e9e"
        return df.style.map(_color, subset=["Confidence"])
    return df


# ──────────────────────────────────────────────
#  INTERACTIVE GRID  (hover a ticker → live chart)
# ──────────────────────────────────────────────

# Daily candle chart image for the hovered ticker. Finviz serves these without
# auth and refreshes the current day's candle intraday, so it's effectively live.
CHART_URL = "https://charts2.finviz.com/chart.ashx?t={t}&ty=c&ta=1&p=d&s=l"

if HAS_AGGRID:
    # Custom AgGrid tooltip: builds a small card with the live chart image for
    # whatever ticker the row holds. Shown after a short hover delay.
    _CHART_TOOLTIP = JsCode("""
    class ChartTooltip {
        init(params) {
            const t = (params.value || '').toString().toUpperCase();
            this.eGui = document.createElement('div');
            this.eGui.style.cssText =
                'background:#0e1117;border:1px solid #444;border-radius:8px;'
                + 'padding:6px;box-shadow:0 4px 14px rgba(0,0,0,.5);';
            const url = 'https://charts2.finviz.com/chart.ashx?t=' + t
                      + '&ty=c&ta=1&p=d&s=l';
            this.eGui.innerHTML =
                '<div style="color:#fff;font-weight:bold;margin-bottom:4px;">'
                + t + ' — daily</div>'
                + '<img src="' + url + '" width="440" height="220" '
                + 'style="display:block;border-radius:4px;" '
                + 'onerror="this.replaceWith(Object.assign(document.createElement(\\'div\\'),'
                + '{textContent:\\'chart unavailable\\',style:\\'color:#aaa;padding:20px\\'}));"/>';
        }
        getGui() { return this.eGui; }
    }
    """)

    _SCORE_STYLE = JsCode("""
    function(p){ const v = Number(p.value);
        if (v >= 8) return {backgroundColor:'#1b5e20', color:'white'};
        if (v >= 5) return {backgroundColor:'#f9a825', color:'black'};
        return {color:'#9e9e9e'}; }
    """)

    _CONF_STYLE = JsCode("""
    function(p){ const v = Number(p.value);
        if (v >= 75) return {backgroundColor:'#1b5e20', color:'white'};
        if (v >= 55) return {backgroundColor:'#f9a825', color:'black'};
        return {color:'#9e9e9e'}; }
    """)

    # Transparent wrapper so our own card styling shows instead of AgGrid's box.
    _TOOLTIP_CSS = {
        ".ag-tooltip": {"background-color": "transparent !important",
                        "border": "none !important",
                        "padding": "0 !important"},
    }


def render_grid(rows, key):
    """Render strategy rows as an interactive grid; hovering a Ticker for a
    moment pops a live daily chart. Falls back to a styled dataframe if
    streamlit-aggrid isn't installed."""
    if not rows:
        st.info("No qualifying setups for this strategy right now.")
        return
    if not HAS_AGGRID:
        st.dataframe(style_table(rows), use_container_width=True, hide_index=True)
        return

    df = pd.DataFrame(rows)
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, sortable=True, filter=True)
    if "Ticker" in df.columns:
        gb.configure_column(
            "Ticker", pinned="left", tooltipField="Ticker",
            tooltipComponent=_CHART_TOOLTIP,
            cellStyle={"fontWeight": "bold", "cursor": "crosshair"},
        )
    for col in ("Score", "Strength"):
        if col in df.columns:
            gb.configure_column(col, cellStyle=_SCORE_STYLE)
    if "Confidence" in df.columns:
        gb.configure_column("Confidence", cellStyle=_CONF_STYLE)

    opts = gb.build()
    opts["tooltipShowDelay"] = 1200   # ~1.2s hover before the chart appears
    opts["tooltipHideDelay"] = 10000  # keep it up long enough to read
    opts["tooltipInteraction"] = True

    AgGrid(
        df, gridOptions=opts, allow_unsafe_jscode=True,
        custom_css=_TOOLTIP_CSS, theme="streamlit",
        fit_columns_on_grid_load=False, enable_enterprise_modules=False,
        height=min(620, 80 + 34 * len(df)), key=key,
    )


# ──────────────────────────────────────────────
#  PIPELINE
# ──────────────────────────────────────────────

def run_full_analysis(sources, max_tickers, selected, skip_polygon):
    """Build the universe, fetch data, and run the selected strategies."""
    progress = st.status("Running analysis…", expanded=True)

    with progress:
        st.write(f"Building universe from: {', '.join(sources)}")
        tickers, pop_scores = ns.build_universe(
            sources=sources, max_tickers=max_tickers, show=False, large=False)
        st.write(f"Universe: {len(tickers)} tickers")

        st.write("Fetching price data…")
        data_map = ns.fetch_price_data(tickers, skip_polygon=skip_polygon)
        st.write(f"Price data: {len(data_map)} tickers")

        st.write("Fetching fundamentals…")
        info_map = ns.fetch_fundamentals(list(data_map.keys()))
        ns.enrich_short_interest(info_map)

        premarket = {} if skip_polygon else ns.fetch_polygon_premarket(list(data_map.keys()))
        bench_c = get_benchmark()

        results = {}
        for key in selected:
            label, _sub, runner = STRATEGIES[key]
            st.write(f"Scoring {label}…")
            results[key] = runner(data_map, info_map, pop_scores, bench_c, premarket, False)
        progress.update(label="Analysis complete", state="complete", expanded=False)

    return results


def rate_ticker(ticker):
    """Score one ticker against all 7 strategies with entry gates relaxed."""
    ticker = ticker.strip().upper()
    data_map = ns.fetch_price_data([ticker])
    if ticker not in data_map:
        return None, None, "No price data found for this ticker."

    info_map = ns.fetch_fundamentals([ticker])
    ns.enrich_short_interest(info_map)
    pop_scores = {ticker: 0}
    bench_c = get_benchmark()
    try:
        premarket = ns.fetch_polygon_premarket([ticker])
    except Exception:
        premarket = {}

    ratings = []
    for key, (label, _sub, runner) in STRATEGIES.items():
        try:
            rows = runner(data_map, info_map, pop_scores, bench_c, premarket, True)
        except Exception:
            rows = []
        match = next((r for r in rows if r.get("Ticker") == ticker), None)
        ratings.append({
            "key": key,
            "label": label,
            "confidence": match.get("Confidence") if match else None,
            "row": match,
        })

    ratings.sort(key=lambda x: (x["confidence"] is not None, x["confidence"] or 0),
                 reverse=True)
    return ratings, info_map.get(ticker, {}), None


# ──────────────────────────────────────────────
#  UI
# ──────────────────────────────────────────────

st.title("📈 NASDAQ Popularity Screener")
st.caption("Interactive dashboard for the 7-strategy screener. "
           "Educational use only — not financial advice.")

tab_run, tab_rate = st.tabs(["🔍 Run Analysis", "🎯 Rate a Ticker"])

# ---- Tab 1: Run Analysis ----
with tab_run:
    with st.sidebar:
        st.header("Analysis settings")
        sources = st.multiselect(
            "Universe sources",
            ["finviz", "yahoo", "reddit", "nasdaq", "insider",
             "quality", "momentum", "finnhub", "movers"],
            default=["finviz", "yahoo", "quality", "momentum", "movers"],
        )
        max_tickers = st.slider("Max tickers to screen", 50, 1200, 600, step=50)
        strat_labels = {k: v[0] for k, v in STRATEGIES.items()}
        selected = st.multiselect(
            "Strategies to run",
            options=list(STRATEGIES.keys()),
            default=list(STRATEGIES.keys()),
            format_func=lambda k: strat_labels[k],
        )
        skip_polygon = st.checkbox("Skip Polygon (yfinance only)", value=False)

    st.subheader("Run the screener")
    st.write("Builds the popularity-ranked universe, fetches live data, and runs "
             "the selected strategies. This can take a few minutes depending on "
             "universe size.")

    if st.button("▶ Run Analysis", type="primary", disabled=not (sources and selected)):
        results = run_full_analysis(sources, max_tickers, selected, skip_polygon)
        st.session_state["analysis_results"] = results

    results = st.session_state.get("analysis_results")
    if results:
        if HAS_AGGRID:
            st.caption("💡 Hover over a **Ticker** for a moment to pop its live daily chart. "
                       "Columns are sortable and filterable.")
        else:
            st.caption("Hover-charts disabled — `streamlit-aggrid` isn't importable in the "
                       "Python running this app. Launch with the project venv: "
                       "`.venv\\Scripts\\python -m streamlit run dashboard.py`")
            st.caption(f"Import error: `{AGGRID_ERR}` · running on `{sys.executable}`")
        for key in sorted(results):
            label, subtitle, _runner = STRATEGIES[key]
            rows = results[key]
            st.markdown(f"### {label}")
            st.caption(subtitle)
            render_grid(rows, key=f"grid_s{key}")

# ---- Tab 2: Rate a Ticker ----
with tab_rate:
    st.subheader("Rate a single ticker")
    st.write("Scores the ticker against all 7 strategies (entry gates relaxed so "
             "every strategy returns a score) and reports the best fit.")

    col_in, col_btn = st.columns([3, 1])
    with col_in:
        ticker_input = st.text_input("Ticker", placeholder="e.g. NVDA",
                                     label_visibility="collapsed")
    with col_btn:
        rate_clicked = st.button("🎯 Rate", type="primary",
                                 disabled=not ticker_input.strip())

    if rate_clicked:
        with st.spinner(f"Rating {ticker_input.strip().upper()}…"):
            ratings, info, err = rate_ticker(ticker_input)
        if err:
            st.error(err)
        else:
            scored = [r for r in ratings if r["confidence"] is not None]
            company = info.get("longName") or info.get("shortName") or ticker_input.upper()
            sector = info.get("sector", "—")
            st.markdown(f"## {ticker_input.strip().upper()} — {company}")
            st.caption(f"Sector: {sector}")

            if scored:
                best = scored[0]
                c1, c2 = st.columns(2)
                c1.metric("Best-fit strategy", best["label"].split("—")[-1].strip())
                c2.metric("Confidence", f"{best['confidence']}")

                st.markdown("### Fit across all strategies")
                summary = pd.DataFrame([
                    {"Strategy": r["label"],
                     "Confidence": r["confidence"] if r["confidence"] is not None else "—"}
                    for r in ratings
                ])
                st.dataframe(style_table(summary.to_dict("records"))
                             if "Confidence" in summary else summary,
                             use_container_width=True, hide_index=True)

                st.bar_chart(
                    pd.DataFrame(
                        {"Confidence": [r["confidence"] for r in scored]},
                        index=[r["label"].split("—")[0].strip() for r in scored],
                    )
                )

                st.markdown("### Detail per strategy")
                for r in ratings:
                    if not r["row"]:
                        continue
                    with st.expander(f"{r['label']}  ·  Confidence {r['confidence']}"):
                        detail = {k: v for k, v in r["row"].items() if k != "Ticker"}
                        st.dataframe(pd.DataFrame([detail]).T.rename(columns={0: "Value"}),
                                     use_container_width=True)
            else:
                st.warning("Could not score this ticker on any strategy "
                           "(insufficient price history).")
