"""
Momentum Strategy Backtest
----------------------------
Simulates the SAME scoring logic used by momentum_stock_bot.py, rebalancing
weekly and holding each week's top-N picks for a fixed number of trading
days (default 5 = ~1 week), then reports how that would have performed
historically vs. the Nifty 50 benchmark.

IMPORTANT: The scoring logic below is intentionally a duplicate of
fetch_and_score() in momentum_stock_bot.py, not an import — this keeps the
live bot simple and dependency-free. If you change the scoring weights or
filters in momentum_stock_bot.py, mirror the change here too, or the
backtest will no longer reflect what the bot actually sends you.

Usage:
    python backtest.py

Env vars (all optional):
    BACKTEST_MONTHS  - months of history to test (default 6)
    HOLD_DAYS         - holding period in trading days (default 5 = ~1 week)
    TOP_N             - picks per rebalance (default 20)
    UNIVERSE          - "nifty200" (default, faster) or "nifty500" (slower, more thorough)
    MIN_PRICE         - minimum share price filter (default 20)
    MIN_AVG_VOLUME    - minimum 20-day avg volume filter (default 100000)

Output:
    Prints a summary to the console and saves full period-by-period
    results to backtest_results.csv
"""

import os
import time
import numpy as np
import pandas as pd
import yfinance as yf

from momentum_stock_bot import get_nse_universe, compute_rsi, send_telegram  # reuse helpers

BACKTEST_MONTHS = int(os.environ.get("BACKTEST_MONTHS", "6"))
HOLD_DAYS = int(os.environ.get("HOLD_DAYS", "5"))
TOP_N = int(os.environ.get("TOP_N", "20"))
MIN_PRICE = float(os.environ.get("MIN_PRICE", "20"))
MIN_AVG_VOLUME = float(os.environ.get("MIN_AVG_VOLUME", "100000"))
UNIVERSE_OVERRIDE = os.environ.get("UNIVERSE", "nifty200").lower()


def download_history(symbols: list[str], months: int) -> dict[str, pd.DataFrame]:
    tickers = [s + ".NS" for s in symbols]
    # extra buffer months so the earliest rebalance date still has a full
    # 3-month lookback available
    period_str = f"{months + 4}mo"
    print(f"Downloading {period_str} of history for {len(tickers)} tickers...")

    panel = {}
    batch_size = 50
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            data = yf.download(
                batch, period=period_str, interval="1d",
                group_by="ticker", auto_adjust=True,
                progress=False, threads=True,
            )
        except Exception as e:
            print(f"Batch download failed ({e}), skipping.")
            continue

        for t in batch:
            sym = t.replace(".NS", "")
            try:
                df = data[t].dropna() if len(batch) > 1 else data.dropna()
                if len(df) > 70:
                    panel[sym] = df[["Close", "Volume"]]
            except Exception:
                continue
        time.sleep(1)

    print(f"Got usable history for {len(panel)} symbols.")
    return panel


def score_as_of(panel: dict[str, pd.DataFrame], as_of_date: pd.Timestamp) -> pd.DataFrame:
    """Mirrors fetch_and_score()'s logic in momentum_stock_bot.py, but scores
    using only data up to (and including) as_of_date — no lookahead."""
    rows = []
    for sym, df in panel.items():
        d = df[df.index <= as_of_date]
        if len(d) < 70:
            continue

        close = d["Close"]
        volume = d["Volume"]
        last_price = float(close.iloc[-1])
        if last_price < MIN_PRICE:
            continue

        avg_vol_20 = float(volume.tail(20).mean())
        if avg_vol_20 < MIN_AVG_VOLUME:
            continue

        if len(close) <= 63:
            continue
        ret_1m = (close.iloc[-1] / close.iloc[-22] - 1) * 100
        ret_3m = (close.iloc[-1] / close.iloc[-63] - 1) * 100
        dma50 = float(close.tail(50).mean())
        vol_ratio = float(volume.tail(5).mean() / avg_vol_20) if avg_vol_20 > 0 else 1.0
        rsi14 = compute_rsi(close, 14)
        above_50dma = last_price > dma50

        if pd.isna(ret_1m) or pd.isna(ret_3m):
            continue

        rows.append({
            "Symbol": sym, "1M%": ret_1m, "3M%": ret_3m,
            "RSI14": rsi14, "VolRatio": vol_ratio, "Above50DMA": above_50dma,
        })

    df_out = pd.DataFrame(rows)
    if df_out.empty:
        return df_out

    df_out = df_out[df_out["Above50DMA"]].copy()
    if df_out.empty:
        return df_out

    df_out["rank_1m"] = df_out["1M%"].rank(pct=True)
    df_out["rank_3m"] = df_out["3M%"].rank(pct=True)
    df_out["rank_vol"] = df_out["VolRatio"].rank(pct=True)
    df_out["rank_rsi"] = df_out["RSI14"].rank(pct=True)
    df_out["MomentumScore"] = (
        0.35 * df_out["rank_3m"] + 0.30 * df_out["rank_1m"]
        + 0.20 * df_out["rank_vol"] + 0.15 * df_out["rank_rsi"]
    ) * 100

    return df_out.sort_values("MomentumScore", ascending=False).reset_index(drop=True)


def get_benchmark(months: int) -> pd.DataFrame | None:
    try:
        bdf = yf.download("^NSEI", period=f"{months + 4}mo", interval="1d",
                           auto_adjust=True, progress=False)
        return bdf[["Close"]].dropna()
    except Exception as e:
        print(f"Benchmark fetch failed: {e}")
        return None


def run_backtest():
    universe_key = UNIVERSE_OVERRIDE if UNIVERSE_OVERRIDE in ("nifty50", "nifty200", "nifty500") else "nifty200"
    # temporarily point get_nse_universe at the right list by env var, since
    # it reads UNIVERSE from the environment itself
    os.environ["UNIVERSE"] = universe_key
    symbols = get_nse_universe()

    panel = download_history(symbols, BACKTEST_MONTHS)
    if not panel:
        print("No data downloaded, aborting.")
        return

    benchmark = get_benchmark(BACKTEST_MONTHS)

    all_dates = sorted(set().union(*[df.index for df in panel.values()]))
    all_dates = pd.DatetimeIndex(all_dates)

    warmup = 70
    rebal_positions = list(range(warmup, len(all_dates) - HOLD_DAYS, HOLD_DAYS))

    results = []
    for pos in rebal_positions:
        as_of_date = all_dates[pos]
        exit_date = all_dates[pos + HOLD_DAYS]

        scored = score_as_of(panel, as_of_date)
        if scored.empty:
            continue
        picks = scored.head(TOP_N)

        fwd_returns = []
        for _, row in picks.iterrows():
            df = panel[row["Symbol"]]
            entry = df[df.index <= as_of_date]["Close"]
            exit_ = df[df.index <= exit_date]["Close"]
            if entry.empty or exit_.empty:
                continue
            fwd_returns.append((exit_.iloc[-1] / entry.iloc[-1] - 1) * 100)

        if not fwd_returns:
            continue

        bench_ret = np.nan
        if benchmark is not None:
            b_entry = benchmark[benchmark.index <= as_of_date]["Close"]
            b_exit = benchmark[benchmark.index <= exit_date]["Close"]
            if not b_entry.empty and not b_exit.empty:
                bench_ret = float((b_exit.iloc[-1] / b_entry.iloc[-1] - 1) * 100)

        results.append({
            "RebalanceDate": as_of_date.date(),
            "ExitDate": exit_date.date(),
            "NumPicks": len(fwd_returns),
            "AvgReturn%": round(float(np.mean(fwd_returns)), 2),
            "WinRate%": round(float(np.mean([r > 0 for r in fwd_returns])) * 100, 1),
            "Best%": round(max(fwd_returns), 2),
            "Worst%": round(min(fwd_returns), 2),
            "Nifty50%": round(bench_ret, 2) if not np.isnan(bench_ret) else None,
        })

    if not results:
        print("No valid rebalance periods produced results — try a longer BACKTEST_MONTHS window.")
        send_telegram(["⚠️ Weekly backtest ran but produced no results "
                        "(check filters or BACKTEST_MONTHS)."])
        return

    res_df = pd.DataFrame(results)
    res_df.to_csv("backtest_results.csv", index=False)

    avg_strategy = res_df["AvgReturn%"].mean()
    avg_bench = res_df["Nifty50%"].mean() if res_df["Nifty50%"].notna().any() else None
    win_periods = (res_df["AvgReturn%"] > 0).mean() * 100
    avg_win_rate = res_df["WinRate%"].mean()

    print("\n" + "=" * 50)
    print("BACKTEST SUMMARY")
    print("=" * 50)
    print(f"Universe: {universe_key} | Hold period: {HOLD_DAYS} trading days (~1 week)")
    print(f"Period tested: {res_df['RebalanceDate'].min()} to {res_df['ExitDate'].max()}")
    print(f"Rebalances tested: {len(res_df)}")
    print(f"\nAvg return per {HOLD_DAYS}-day hold (strategy): {avg_strategy:.2f}%")
    if avg_bench is not None:
        print(f"Avg return per {HOLD_DAYS}-day hold (Nifty 50):  {avg_bench:.2f}%")
        print(f"Average edge over Nifty 50 per period: {avg_strategy - avg_bench:.2f} percentage points")
    print(f"\nAvg per-stock win rate within each pick list: {avg_win_rate:.1f}%")
    print(f"% of weeks where the strategy's AVERAGE pick was profitable: {win_periods:.1f}%")
    print(f"Best week (avg): {res_df['AvgReturn%'].max():.2f}%   Worst week (avg): {res_df['AvgReturn%'].min():.2f}%")

    approx_periods_per_year = 252 / HOLD_DAYS
    print(f"\nRough annualized return, NOT compounded, illustrative only: "
          f"{avg_strategy * approx_periods_per_year:.1f}%")
    print("\nFull period-by-period results saved to backtest_results.csv")
    print("\nReminder: past performance doesn't guarantee future results. "
          "This backtest also ignores brokerage, taxes (STCG), and slippage, "
          "all of which reduce real-world returns.")

    # --- Telegram summary ---
    edge_line = ""
    if avg_bench is not None:
        edge = avg_strategy - avg_bench
        edge_line = f"vs Nifty 50: <b>{avg_bench:+.2f}%</b>  (edge: {edge:+.2f} pts)\n"

    msg = (
        f"🔬 <b>Weekly Backtest Check</b>\n"
        f"<i>{res_df['RebalanceDate'].min()} to {res_df['ExitDate'].max()} "
        f"| {len(res_df)} weeks tested | {HOLD_DAYS}-day hold</i>\n\n"
        f"Avg return per week (strategy): <b>{avg_strategy:+.2f}%</b>\n"
        f"{edge_line}"
        f"Weeks profitable: <b>{win_periods:.0f}%</b>\n"
        f"Avg per-stock win rate: <b>{avg_win_rate:.0f}%</b>\n"
        f"Best week: {res_df['AvgReturn%'].max():+.2f}%  |  Worst week: {res_df['AvgReturn%'].min():+.2f}%\n\n"
        f"<i>Excludes brokerage, STCG tax (~20%), and slippage — real returns will be lower. "
        f"Not investment advice.</i>"
    )
    send_telegram([msg])


if __name__ == "__main__":
    run_backtest()
