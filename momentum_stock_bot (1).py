"""
NSE Momentum Stock Screener + Telegram Bot
--------------------------------------------
Fetches NSE stock universe, computes a momentum composite score,
picks the top 20, and sends a formatted report to Telegram.

Run manually:
    python momentum_stock_bot.py

Environment variables required (set as GitHub Secrets or local env vars):
    TELEGRAM_BOT_TOKEN  - from @BotFather
    TELEGRAM_CHAT_ID    - your chat id (see setup instructions in README.md)

Optional env vars:
    UNIVERSE            - "nifty500" (default), "nifty200", or "nifty50"
    TOP_N               - number of stocks to report (default 20)
    MIN_PRICE           - minimum share price filter (default 20)
    MIN_AVG_VOLUME      - minimum 20-day avg volume filter (default 100000)
"""

import os
import sys
import time
import io
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
UNIVERSE = os.environ.get("UNIVERSE", "nifty500").lower()
TOP_N = int(os.environ.get("TOP_N", "20"))
MIN_PRICE = float(os.environ.get("MIN_PRICE", "20"))
MIN_AVG_VOLUME = float(os.environ.get("MIN_AVG_VOLUME", "100000"))

NSE_INDEX_URLS = {
    "nifty50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}

# Small fallback list used only if the NSE archive fetch fails (e.g. blocked in CI)
FALLBACK_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL",
    "ITC", "LT", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "HINDUNILVR", "MARUTI",
    "SUNPHARMA", "TATAMOTORS", "TITAN", "ULTRACEMCO", "ASIANPAINT", "WIPRO",
    "ADANIENT", "ADANIPORTS", "NTPC", "POWERGRID", "M&M", "TATASTEEL", "JSWSTEEL",
    "HCLTECH", "TECHM", "GRASIM", "CIPLA", "DRREDDY", "EICHERMOT", "BAJAJFINSV",
    "HEROMOTOCO", "COALINDIA", "ONGC", "BPCL", "DIVISLAB", "SBILIFE", "HDFCLIFE",
    "BRITANNIA", "NESTLEIND", "APOLLOHOSP", "TATACONSUM", "UPL", "INDUSINDBK",
    "BAJAJ-AUTO", "SHREECEM", "PIDILITIND", "DABUR",
]


def get_nse_universe() -> list[str]:
    """Fetch the NSE index constituent list. Falls back to a static list on failure."""
    url = NSE_INDEX_URLS.get(UNIVERSE, NSE_INDEX_URLS["nifty500"])
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/csv,*/*",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        symbols = df["Symbol"].dropna().astype(str).str.strip().tolist()
        if len(symbols) > 20:
            print(f"Loaded {len(symbols)} symbols from NSE ({UNIVERSE}).")
            return symbols
    except Exception as e:
        print(f"NSE fetch failed ({e}); using fallback list.")
    return FALLBACK_SYMBOLS


def compute_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty and not np.isnan(rsi.iloc[-1]) else 50.0


def fetch_and_score(symbols: list[str]) -> pd.DataFrame:
    tickers = [s + ".NS" for s in symbols]
    print(f"Downloading price history for {len(tickers)} tickers...")

    rows = []
    batch_size = 50
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            data = yf.download(
                batch, period="7mo", interval="1d",
                group_by="ticker", auto_adjust=True,
                progress=False, threads=True,
            )
        except Exception as e:
            print(f"Batch download failed ({e}), skipping batch.")
            continue

        for t in batch:
            sym = t.replace(".NS", "")
            try:
                df = data[t].dropna() if len(batch) > 1 else data.dropna()
                if len(df) < 70:
                    continue

                close = df["Close"]
                volume = df["Volume"]
                last_price = float(close.iloc[-1])
                if last_price < MIN_PRICE:
                    continue

                avg_vol_20 = float(volume.tail(20).mean())
                if avg_vol_20 < MIN_AVG_VOLUME:
                    continue

                ret_1d = (close.iloc[-1] / close.iloc[-2] - 1) * 100
                ret_1m = (close.iloc[-1] / close.iloc[-22] - 1) * 100 if len(close) > 22 else np.nan
                ret_3m = (close.iloc[-1] / close.iloc[-63] - 1) * 100 if len(close) > 63 else np.nan
                dma50 = float(close.tail(50).mean())
                vol_ratio = float(volume.tail(5).mean() / avg_vol_20) if avg_vol_20 > 0 else 1.0
                rsi14 = compute_rsi(close, 14)
                above_50dma = last_price > dma50

                if any(pd.isna(x) for x in [ret_1m, ret_3m]):
                    continue

                rows.append({
                    "Symbol": sym,
                    "LTP": round(last_price, 2),
                    "1D%": round(ret_1d, 2),
                    "1M%": round(ret_1m, 2),
                    "3M%": round(ret_3m, 2),
                    "RSI14": round(rsi14, 1),
                    "VolRatio": round(vol_ratio, 2),
                    "Above50DMA": above_50dma,
                })
            except Exception:
                continue
        time.sleep(1)  # be gentle with Yahoo Finance

    df_out = pd.DataFrame(rows)
    if df_out.empty:
        return df_out

    # Trend filter: only stocks in a confirmed uptrend
    df_out = df_out[df_out["Above50DMA"]].copy()

    # Rank-based composite momentum score (percentile ranks, higher = stronger)
    df_out["rank_1m"] = df_out["1M%"].rank(pct=True)
    df_out["rank_3m"] = df_out["3M%"].rank(pct=True)
    df_out["rank_vol"] = df_out["VolRatio"].rank(pct=True)
    df_out["rank_rsi"] = df_out["RSI14"].rank(pct=True)

    df_out["MomentumScore"] = (
        0.35 * df_out["rank_3m"] +
        0.30 * df_out["rank_1m"] +
        0.20 * df_out["rank_vol"] +
        0.15 * df_out["rank_rsi"]
    ) * 100

    df_out = df_out.sort_values("MomentumScore", ascending=False).reset_index(drop=True)
    return df_out


def format_telegram_message(df: pd.DataFrame) -> list[str]:
    top = df.head(TOP_N)
    header = f"📈 <b>Top {len(top)} Momentum Stocks (NSE)</b>\n"
    header += f"<i>{pd.Timestamp.now().strftime('%d %b %Y, %H:%M IST')}</i>\n\n"

    lines = [header]
    body = ""
    for i, row in top.iterrows():
        body += (
            f"<b>{i+1}. {row['Symbol']}</b>  ₹{row['LTP']}\n"
            f"   1D: {row['1D%']:+.2f}%  |  1M: {row['1M%']:+.2f}%  |  3M: {row['3M%']:+.2f}%\n"
            f"   RSI14: {row['RSI14']}  |  VolRatio: {row['VolRatio']}x  |  Score: {row['MomentumScore']:.1f}\n\n"
        )

    # Telegram messages cap at 4096 chars; split into chunks if needed
    messages = []
    current = header
    for line in body.split("\n\n"):
        if len(current) + len(line) > 3800:
            messages.append(current)
            current = ""
        current += line + "\n\n"
    if current.strip():
        messages.append(current)

    return messages


def send_telegram(messages: list[str]):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead:\n")
        for m in messages:
            print(m)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for m in messages:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": m,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if resp.status_code != 200:
            print(f"Telegram send failed: {resp.text}")
        time.sleep(1)


def main():
    symbols = get_nse_universe()
    df = fetch_and_score(symbols)

    if df.empty:
        send_telegram(["⚠️ Momentum screener ran but found no qualifying stocks today "
                        "(check filters or data source)."])
        sys.exit(0)

    messages = format_telegram_message(df)
    send_telegram(messages)
    print(f"Sent {len(messages)} message(s) with top {min(TOP_N, len(df))} stocks.")


if __name__ == "__main__":
    main()
