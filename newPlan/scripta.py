import os

import pandas as pd

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_URL  = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
OUT_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stockTickers.txt")

# NASDAQ-listed symbols
nasdaq = pd.read_csv(NASDAQ_URL, sep="|")
nasdaq = nasdaq[nasdaq["Test Issue"] == "N"]          # drop test issues + the footer row
nasdaq_symbols = nasdaq["Symbol"]

# NYSE-listed symbols live in the "otherlisted" directory, keyed by Exchange code.
other = pd.read_csv(OTHER_URL, sep="|")
other = other[(other["Test Issue"] == "N") & (other["Exchange"] == "N")]   # N = NYSE
nyse_symbols = other["ACT Symbol"]

symbols = pd.concat([nasdaq_symbols, nyse_symbols]).dropna().unique()
symbols = sorted(symbols)

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("\n".join(symbols) + "\n")

print(f"Saved {len(symbols)} tickers ({len(nasdaq_symbols)} NASDAQ + {len(nyse_symbols)} NYSE) to {OUT_PATH}")
