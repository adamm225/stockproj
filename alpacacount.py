from alpaca.trading.client import TradingClient

# API keys
API_KEY = "YOUR_API_KEY"
SECRET_KEY = "YOUR_SECRET_KEY"

# Replace with your real keys from Alpaca dashboard
API_KEY = "PKOIIQ3ZO5OKXU4F7IUXO24LAT"
SECRET_KEY = "AiSjrps3rtpgDNeKiTWM4PcPWN8dh1X2GVLtPy49va7X"

# Tickers
tickers = [

    # =========================
    # Mega / Large Cap Momentum
    # =========================
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "NFLX", "INTC", "ORCL", "ADBE", "CRM", "AVGO",
    "QCOM", "TXN", "AMAT", "MU", "IBM", "CSCO",

    # =========================
    # Growth / Mid Cap Momentum
    # =========================
    "BROS", "SHOP", "SNOW", "ROKU", "COIN", "HOOD", "UPST",
    "PLTR", "NET", "DDOG", "OKTA", "ZM", "SQ", "AFRM",
    "LCID", "RIVN", "NIO", "XPEV", "LI", "W", "U",

    # =========================
    # Biotech / High Volatility
    # =========================
    "OVID", "NNOX", "SLS", "MULN", "NKLA", "INO", "VXRT",
    "IBRX", "MRNA", "BNTX", "SRNE", "CRSP", "EDIT", "NTLA",
    "VERV", "BEAM", "ARWR", "SGMO", "BLUE", "AGL",

    # =========================
    # Low Float / Momentum Small Caps
    # =========================
    "TOPS", "SINT", "ATER", "MARA", "RIOT", "CLSK", "WULF",
    "BTBT", "HUT", "CIFR", "IREN", "HIVE", "CORZ",

    # =========================
    # Energy / Cyclicals (volatile runners)
    # =========================
    "XOM", "CVX", "OXY", "DVN", "EOG", "SLB", "HAL",
    "COP", "FANG", "PXD",

    # =========================
    # Financial / Momentum Banks
    # =========================
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW",

    # =========================
    # Retail / Consumer momentum
    # =========================
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "MCD",
    "SBUX", "DKS", "LULU",

    # =========================
    # Tech Momentum Add-ons
    # =========================
    "PANW", "CRWD", "ZS", "SNPS", "CDNS", "ANET",
    "DELL", "HPQ", "SMCI", "FSLR",

    # =========================
    # EV / Speculative Momentum
    # =========================
    "TSLA", "LCID", "RIVN", "NKLA", "FSR", "GOEV",

    # =========================
    # Speculative / Small Cap Movers
    # =========================
    "BBBYQ", "SPCE", "FFIE", "EXPR", "KOSS", "GME", "AMC",

    #added here
    "UBER", "LYFT", "DASH", "ABNB", "PINS", "ETSY",
    "TWLO", "DOCU", "MDB", "GTLB", "AI", "IONQ",
    "PATH", "C3AI", "RBLX", "TTD", "BILL", "WIX",
    "ESTC", "FIVN", "S", "NET",

    #C3AI is the only one that causes the whole thing to fail
    #how to handle that to make it so that it won't cause the whole fial
    # AI / High Momentum Themes
    "NVTS", "SYM", "TEM", "SOUN", "AIRO", "BBAI",
    "AUR", "JOBY", "ACHR", "RKLB", "ASTS",

    # Biotech / Pharma Movers
    "PFE", "MRK", "ABBV", "LLY", "REGN", "GILD",
    "VRTX", "BIIB", "ILMN", "EXEL", "IONS", "SRPT",
    "AMGN", "ALNY", "UTHR",

    # Small/Mid Cap Volatility
    "FFIE", "WKHS", "SOUN", "CLOV", "SOFI", "OPEN",
    "DNA", "HOOD", "PLUG", "BLNK", "ENPH", "SEDG",
    "RUN", "SPWR",

    # Semiconductors / AI Hardware
    "TSM", "ASML", "NVMI", "TER", "KLAC", "LRCX",
    "AMKR", "MPWR", "ENTG", "ONTO", "COHR",

    # Energy / Uranium / Commodities Momentum
    "UEC", "CCJ", "NXE", "URA", "USO", "UNG",
    "VRT", "ET", "KMI", "WMB",

    # Financial Fintech / Speculative
    "PYPL", "SQ", "AFRM", "AFG", "SOFI", "HOOD",
    "UPST", "LC", "ALLY", "FIS", "FISV",

    # Retail / Consumer / Cyclical
    "TJX", "ROST", "BURL", "KR", "KHC", "GIS",
    "PEP", "KO", "DE", "CAT", "NUE", "STLD",

    # High Beta ETFs (useful for context signals)
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SOXL", "SOXS",

    # Momentum Small Caps / Traders favorites
    "MARA", "RIOT", "CLSK", "WULF", "BTBT",
    "IONQ", "HUT", "CIFR", "IREN", "HIVE",

    # Meme / Retail Speculative (optional but useful for sentiment)
    "GME", "AMC", "BB", "KOSS", "EXPR", "SAVA"

]


'''
Prompt:
I notice here when I get an invalid symbol, such as C3AI, the whole thing fails. How can I handle that so that it won't cause the whole thing to fail and ones that have been successfully retireived
# will still allow it to continue in alpacacount.py but also translate to nasdaq_v3.py?


'''

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from datetime import datetime, timedelta

import pandas as pd
import numpy as np

# ============================================
# API KEYS
# ============================================

# ============================================
# CREATE CLIENT
# ============================================

data_client = StockHistoricalDataClient(
    API_KEY,
    SECRET_KEY
)



# ============================================
# DATE RANGE
# ============================================

end_date = datetime.now()
start_date = end_date - timedelta(days=120)

# ============================================
# FETCH DATA (with error handling for invalid symbols)
# ============================================

def fetch_bars_with_retry(client, tickers_list, timeframe, start, end):
    """Fetch bars, splitting batches if invalid symbols are encountered."""
    if not tickers_list:
        return None

    try:
        request = StockBarsRequest(
            symbol_or_symbols=tickers_list,
            timeframe=timeframe,
            start=start,
            end=end
        )
        return client.get_stock_bars(request)
    except Exception as e:
        error_msg = str(e)
        if "invalid symbol" in error_msg.lower() and len(tickers_list) > 1:
            print(f"\n⚠ Invalid symbol in batch, splitting {len(tickers_list)} tickers...")
            mid = len(tickers_list) // 2
            result1 = fetch_bars_with_retry(client, tickers_list[:mid], timeframe, start, end)
            result2 = fetch_bars_with_retry(client, tickers_list[mid:], timeframe, start, end)

            # Merge results
            if result1 and result2:
                for ticker in result2.data:
                    if ticker not in result1.data:
                        result1.data[ticker] = result2.data[ticker]
                return result1
            return result1 or result2
        else:
            print(f"Error fetching bars: {e}")
            return None

# Request in smaller batches to avoid invalid symbol failures
batch_size = 50
all_bars = {}
batches = [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]

for i, batch in enumerate(batches):
    print(f"\nFetching batch {i+1}/{len(batches)} ({len(batch)} tickers)...")
    bars_batch = fetch_bars_with_retry(data_client, batch, TimeFrame.Day, start_date, end_date)
    if bars_batch:
        for ticker in batch:
            if ticker in bars_batch.data:
                all_bars[ticker] = bars_batch.data[ticker]

class BarsResult:
    def __init__(self, data_dict):
        self.data = data_dict

bars = BarsResult(all_bars)
print(f"\nSuccessfully fetched {len(bars.data)} out of {len(tickers)} tickers")

# ============================================
# INDICATOR FUNCTIONS
# ============================================

def calculate_rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss

    rsi = 100 - (100 / (1 + rs))

    return rsi


def calculate_atr(df, period=14):

    high_low = df["high"] - df["low"]

    high_close = np.abs(
        df["high"] - df["close"].shift()
    )

    low_close = np.abs(
        df["low"] - df["close"].shift()
    )

    ranges = pd.concat(
        [high_low, high_close, low_close],
        axis=1
    )

    true_range = ranges.max(axis=1)

    atr = true_range.rolling(period).mean()

    return atr


# ============================================
# PROCESS EACH TICKER
# ============================================

for ticker in tickers:

    print("\n================================================")
    print("Ticker:", ticker)
    print("================================================")

    ticker_data = bars.data.get(ticker)

    if not ticker_data:
        print("No data returned.")
        continue

    # ----------------------------------------
    # Convert to DataFrame
    # ----------------------------------------

    df = pd.DataFrame([{

        "timestamp": bar.timestamp,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume

    } for bar in ticker_data])

    df.set_index("timestamp", inplace=True)

    # ----------------------------------------
    # INDICATORS
    # ----------------------------------------

    # RSI
    df["RSI"] = calculate_rsi(df["close"])

    # ATR
    df["ATR"] = calculate_atr(df)

    # ATR %
    df["ATR_PCT"] = (
        df["ATR"] / df["close"]
    ) * 100

    # EMA20 / EMA50
    df["EMA20"] = (
        df["close"]
        .ewm(span=20, adjust=False)
        .mean()
    )

    df["EMA50"] = (
        df["close"]
        .ewm(span=50, adjust=False)
        .mean()
    )

    # Daily Change %
    df["DAY_CHANGE_PCT"] = (
        (df["close"] - df["close"].shift(1))
        / df["close"].shift(1)
    ) * 100

    # 3-Bar Momentum
    df["MOMENTUM_3D"] = (
        (df["close"] - df["close"].shift(3))
        / df["close"].shift(3)
    ) * 100

    # 20-Day High
    df["HIGH_20D"] = (
        df["high"]
        .rolling(20)
        .max()
    )

    # Average Volume
    df["AVG_VOL_5D"] = (
        df["volume"]
        .rolling(5)
        .mean()
    )

    df["AVG_VOL_20D"] = (
        df["volume"]
        .rolling(20)
        .mean()
    )

    # RVOL
    df["RVOL"] = (
        df["volume"]
        / df["AVG_VOL_20D"]
    )

    # Volume Spike
    df["VOL_SPIKE"] = (
        df["volume"]
        / df["AVG_VOL_5D"]
    )

    # Candle Body
    df["BODY"] = (
        abs(df["close"] - df["open"])
    )

    # Candle Range
    df["RANGE"] = (
        df["high"] - df["low"]
    )

    # Body/Range Ratio
    df["BODY_RANGE_RATIO"] = (
        df["BODY"] / df["RANGE"]
    )

    # Dollar Volume
    df["DOLLAR_VOLUME"] = (
        df["close"] * df["volume"]
    )

    # Breakout %
    df["BREAKOUT_PCT"] = (
        df["close"]
        / df["HIGH_20D"]
    ) * 100

    # ----------------------------------------
    # GET LATEST ROW
    # ----------------------------------------

    latest = df.iloc[-1]

    # ----------------------------------------
    # SIGNAL SCORING
    # ----------------------------------------

    score = 0

    # RVOL
    if latest["RVOL"] >= 2.5:
        score += 1.0
    elif latest["RVOL"] >= 1.5:
        score += 0.5

    # RSI Zone
    if 42 <= latest["RSI"] <= 72:
        score += 1.0

    # Big Move
    if latest["DAY_CHANGE_PCT"] >= 5:
        score += 1.0
    elif latest["DAY_CHANGE_PCT"] >= 3:
        score += 0.5

    # Breakout
    if latest["BREAKOUT_PCT"] >= 97:
        score += 1.0

    # Volume Spike
    if latest["VOL_SPIKE"] >= 3:
        score += 1.0
    elif latest["VOL_SPIKE"] >= 2:
        score += 0.5

    # Momentum
    if latest["MOMENTUM_3D"] > 0:
        score += 1.0

    # ATR %
    if latest["ATR_PCT"] >= 5:
        score += 1.0
    elif latest["ATR_PCT"] >= 3:
        score += 0.5

    # Bull Candle
    if (
        latest["BODY_RANGE_RATIO"] > 0.55
        and latest["close"] > latest["open"]
    ):
        score += 1.0

    # EMA Stack
    if latest["EMA20"] > latest["EMA50"]:
        score += 1.0

    # Dollar Volume
    if latest["DOLLAR_VOLUME"] > 10_000_000:
        score += 1.0
    elif latest["DOLLAR_VOLUME"] > 2_000_000:
        score += 0.5

    # ----------------------------------------
    # CONFIDENCE SCORE
    # ----------------------------------------

    confidence = (
        score / 10
    ) * 100

    # ----------------------------------------
    # TARGETS / STOPS
    # ----------------------------------------

    price = latest["close"]

    target = (
        price
        * (1 + (latest["ATR_PCT"] / 100) * 2.2)
    )

    stop = (
        price
        * (1 - (latest["ATR_PCT"] / 100) * 0.9)
    )

    rr = (
        (target - price)
        / (price - stop)
    )

    # ========================================
    # OUTPUT
    # ========================================

    print(f"Price:              ${price:.2f}")
    print(f"RSI:                {latest['RSI']:.2f}")
    print(f"ATR %:              {latest['ATR_PCT']:.2f}%")
    print(f"RVOL:               {latest['RVOL']:.2f}x")
    print(f"Volume Spike:       {latest['VOL_SPIKE']:.2f}x")
    print(f"3D Momentum:        {latest['MOMENTUM_3D']:.2f}%")
    print(f"Day Change:         {latest['DAY_CHANGE_PCT']:.2f}%")
    print(f"EMA20:              ${latest['EMA20']:.2f}")
    print(f"EMA50:              ${latest['EMA50']:.2f}")
    print(f"Breakout %:         {latest['BREAKOUT_PCT']:.2f}%")
    print(f"Dollar Volume:      ${latest['DOLLAR_VOLUME']:,.0f}")
    print(f"Confidence Score:   {confidence:.1f}/100")
    print(f"Target:             ${target:.2f}")
    print(f"Stop Loss:          ${stop:.2f}")
    print(f"Risk/Reward:        {rr:.2f}")

    # ========================================
    # STRATEGY SIGNAL
    # ========================================

    if confidence >= 70:
        print("SIGNAL: STRONG MOMENTUM")
    elif confidence >= 50:
        print("SIGNAL: MODERATE MOMENTUM")
    else:
        print("SIGNAL: WEAK / NO SETUP")