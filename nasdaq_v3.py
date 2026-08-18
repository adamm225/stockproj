"""
NASDAQ popularity screener — compatibility shim.

The code is now organized as the `screener` package for readability. The
end-to-end run sequence lives in `screener/pipeline.py` (main). This module
re-exports the package's public names so existing entry points keep working:

    python nasdaq_v3.py --morning      # same CLI as before
    import nasdaq_v3 as ns             # dashboard.py uses this

Package layout — the sequence of a run:
    screener/config.py      env, API keys, shared rich console
    screener/sources.py     build the popularity universe (Finviz/Yahoo/Reddit/...)
    screener/marketdata.py  Polygon price/premarket/intraday/news + yfinance fallback
    screener/indicators.py  pure technical indicators + quality math
    screener/strategies.py  strategies 1-8 and their scoring helpers
    screener/display.py     rich tables + market-clock banner
    screener/pipeline.py    main(): wires the whole sequence together
"""
from screener.config import *        # noqa: F401,F403
from screener.indicators import *    # noqa: F401,F403
from screener.marketdata import *    # noqa: F401,F403
from screener.sources import *       # noqa: F401,F403
from screener.strategies import *    # noqa: F401,F403
from screener.display import *       # noqa: F401,F403
from screener.pipeline import main   # noqa: F401

if __name__ == "__main__":
    main()
