"""
Scrape tickers from:
  - Webull actives (rows 30-100 and 130-400) via headless Playwright
  - stockscan.io Robinhood popular top 100 via requests

The Webull scrape runs a headless browser in the background (no visible window).

Setup (one-time):
    pip install playwright
    playwright install chromium
disSymbol
"""

import re
import time
import requests
from bs4 import BeautifulSoup

TICKER_RE = re.compile(r'^[A-Z]{1,5}$')
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})


def is_ticker(s: str) -> bool:
    return bool(s) and bool(TICKER_RE.match(s.upper().strip()))


def write_organized_symbols(src: str = "webull.txt", dst: str = "webull_organized.txt") -> None:
    """
    Read webull.txt and list every "disSymbol":"VALUE" in the order it appears,
    writing only the value (e.g. DIA) one per line into webull_organized.txt.
    Repeating tickers are removed, keeping the first appearance.
    """
    try:
        with open(src, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"  [!] {src} not found — skipping organize step")
        return

    symbols = re.findall(r'"disSymbol"\s*:\s*"([^"]+)"', content)
    unique = list(dict.fromkeys(symbols))  # de-dupe, preserve first appearance

    with open(dst, "w", encoding="utf-8") as f:
        for sym in unique:
            f.write(f"{sym}\n")

    print(f"  [→] Wrote {len(unique)} unique disSymbol value(s) (in order) to {dst}")


# ─────────────────────────────────────────────
#  WEBULL — headless browser scrape (background, no window)
#  Collects ranks 30-100 and 130-400
# ─────────────────────────────────────────────

def scrape_webull_actives() -> set:
    """
    Scrape Webull US actives with a headless browser running in the background.
    Webull is a JS app, so we render the page, scroll to load rows past 100,
    then read the rendered table. Collects ranks 30-100 and 130-400.
    """
    print("\n[*] Scraping Webull actives (headless browser)...")
    tickers = {}  # rank -> symbol, so we can report the range

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [!] Playwright not installed. Run:")
        print("        pip install playwright")
        print("        playwright install chromium")
        return set()

    # Webull renders a *virtualized* list — only ~50 rows live in the DOM at a
    # time and scrolling recycles them. So instead of scraping the DOM, we
    # intercept the API responses (which contain "disSymbol") as we scroll;
    # each scroll triggers Webull to fetch the next batch.
    captured = []   # list of (order_seen, symbol) preserving arrival order
    raw_dumps = []  # raw JSON bodies for webull.txt

    def handle_response(response):
        try:
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = response.text()
            if "disSymbol" not in body and '"symbol"' not in body:
                return
            raw_dumps.append(f"\n--- {response.url} ---\n{body}")
            # Pull symbols in the order they appear in the payload
            for m in re.finditer(r'"(?:disSymbol|symbol|tickerSymbol)"\s*:\s*"([A-Z.]{1,6})"', body):
                sym = m.group(1).upper().split(".")[0]
                if is_ticker(sym):
                    captured.append(sym)
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            # headless=True → runs entirely in the background, no visible window
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            page.on("response", handle_response)

            # "domcontentloaded" fires early — waiting for full "load" on this
            # SPA often never completes (ad/analytics requests hang).
            page.goto(
                "https://www.webull.com/quote/us/actives/",
                wait_until="domcontentloaded",
                timeout=60000,
            )

            try:
                page.wait_for_selector("table tr", timeout=30000)
            except Exception:
                print("  [!] Table did not appear within 30s")
            time.sleep(3)

            # Scroll repeatedly to make Webull fetch each next batch of ~50.
            # We watch the captured-symbol count; when it stops growing, stop.
            print("  [→] Scrolling to trigger lazy-loaded batches...")
            last_len = 0
            stagnant = 0
            for _ in range(40):
                page.mouse.wheel(0, 3000)
                time.sleep(0.7)
                # de-dupe while preserving order to gauge progress
                unique_so_far = len(dict.fromkeys(captured))
                if unique_so_far == last_len:
                    stagnant += 1
                    if stagnant >= 5:
                        break
                else:
                    stagnant = 0
                last_len = unique_so_far
                if unique_so_far >= 400:
                    break

            browser.close()

        # Dump every captured API payload so you can inspect it
        with open("webull.txt", "w", encoding="utf-8") as f:
            f.write("".join(raw_dumps) if raw_dumps else "(no JSON responses captured)")
        print(f"  [→] Wrote {len(raw_dumps)} API payload(s) to webull.txt")

        # After webull.txt is written, read it back and list every disSymbol
        # value in the order it appears, one per line, into webull_organized.txt
        write_organized_symbols()

        # De-dupe preserving arrival order → that order IS the volume rank
        ordered = list(dict.fromkeys(captured))
        print(f"  [→] Captured {len(ordered)} unique symbols in rank order")

        for rank, sym in enumerate(ordered, start=1):
            if (30 <= rank <= 100) or (130 <= rank <= 400):
                tickers[rank] = sym

        if tickers:
            ranks = sorted(tickers)
            print(f"  [✓] Webull: {len(tickers)} tickers (ranks {ranks[0]}-{ranks[-1]})")
        else:
            print("  [!] Webull: 0 tickers — check webull.txt for the raw payloads")

    except Exception as e:
        print(f"  [!] Webull error: {e}")

    return set(tickers.values())


# ─────────────────────────────────────────────
#  STOCKSCAN.IO — Robinhood popular top 100
# ─────────────────────────────────────────────

def scrape_robinhood_popular() -> set:
    """
    Fetch stockscan.io Robinhood popular top 100.
    Extracts tickers from HTML links (href="/stocks/TSLA" pattern).
    """
    print("\n[*] Scraping Robinhood popular stocks (stockscan.io)...")
    tickers = set()

    try:
        resp = SESSION.get(
            "https://stockscan.io/100-popular-stocks-robinhood",
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "html.parser")

        # --- Method 1: extract tickers from href links like /stocks/AAPL or /stock/AAPL ---
        for a in soup.find_all("a", href=True):
            m = re.search(r'/stocks?/([A-Z]{1,5})(?:/|$|\?)', a["href"], re.I)
            if m:
                sym = m.group(1).upper()
                if is_ticker(sym):
                    tickers.add(sym)

        if tickers:
            print(f"  [✓] stockscan (href links): {len(tickers)} tickers")
            return tickers

        # --- Method 2: look for embedded JSON / __NEXT_DATA__ ---
        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            raw = script.string
            matches = re.findall(r'"(?:symbol|ticker|Symbol)"\s*:\s*"([A-Z]{1,5})"', raw)
            tickers = {m for m in matches if is_ticker(m)}
            if tickers:
                print(f"  [✓] stockscan (__NEXT_DATA__): {len(tickers)} tickers")
                return tickers

        # --- Method 3: any table cell / span with short uppercase text ---
        for tag in soup.find_all(["td", "span", "div"], class_=re.compile(r'symbol|ticker', re.I)):
            t = tag.get_text(strip=True).upper()
            if is_ticker(t):
                tickers.add(t)

        if tickers:
            print(f"  [✓] stockscan (class match): {len(tickers)} tickers")
            return tickers

        # --- Debug: dump first 500 chars of HTML so we can see the structure ---
        print("  [!] No tickers found — dumping HTML snippet for debugging:")
        print(resp.text[:1500])

    except Exception as e:
        print(f"  [!] stockscan error: {e}")

    print(f"  [✓] stockscan: {len(tickers)} tickers")
    return tickers


def main():
    print("=" * 60)
    print("  TICKER UNIVERSE SCRAPER")
    print("=" * 60)
    print("  Sources:")
    print("    Webull actives (rows 30-100, 130-400)")
    print("    stockscan.io Robinhood popular top 100")
    print("=" * 60)

    webull_tickers = scrape_webull_actives()
    robinhood_tickers = scrape_robinhood_popular()

    all_tickers = sorted(webull_tickers | robinhood_tickers)

    print("\n" + "=" * 60)
    print(f"  Webull:    {len(webull_tickers)} tickers")
    print(f"  Robinhood: {len(robinhood_tickers)} tickers")
    print(f"  Combined:  {len(all_tickers)} unique tickers")
    print("=" * 60)

    output_file = "scraped_universe.txt"
    with open(output_file, "w") as f:
        for ticker in all_tickers:
            f.write(f"{ticker}\n")

    print(f"\n[✓] Saved to {output_file}")
    print("\nTickers:")
    for ticker in all_tickers:
        print(f"  {ticker}")


if __name__ == "__main__":
    main()
