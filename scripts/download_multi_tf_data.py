"""
HAWK Crypto Bot — Multi-Timeframe Data Downloader
==================================================
Downloads 30m and 4h OHLCV from Binance public REST API (no API key needed).
Matches the date range of the existing 1h CSVs.

Usage:
    python scripts/download_multi_tf_data.py

Output files (in data/):
    ETHUSDT_30m.csv  BTCUSDT_30m.csv  SOLUSDT_30m.csv
    ETHUSDT_4h.csv   BTCUSDT_4h.csv   SOLUSDT_4h.csv
"""
from __future__ import annotations

import os
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")

BINANCE_KLINE = "https://api.binance.com/api/v3/klines"
DATA_DIR      = os.path.join(os.path.dirname(__file__), "..", "data")

SYMBOLS    = ["ETHUSDT", "BTCUSDT", "SOLUSDT"]
INTERVALS  = ["30m", "4h"]

# Bars per ms for each interval (used to compute chunk sizes)
INTERVAL_MS = {
    "30m":  30  * 60 * 1_000,
    "4h":   4   * 60 * 60 * 1_000,
}


def _fetch_chunk(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Fetch up to 1000 bars from Binance starting at start_ms."""
    resp = requests.get(
        BINANCE_KLINE,
        params={
            "symbol":    symbol,
            "interval":  interval,
            "startTime": start_ms,
            "endTime":   end_ms,
            "limit":     1000,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def download_ohlcv(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """
    Download all OHLCV bars for symbol/interval between start_ms and end_ms.
    Paginates automatically using Binance's 1000-bar limit.
    """
    all_bars: list = []
    cursor = start_ms
    bar_ms = INTERVAL_MS[interval]

    while cursor < end_ms:
        chunk = _fetch_chunk(symbol, interval, cursor, end_ms)
        if not chunk:
            break
        all_bars.extend(chunk)
        # Advance cursor past the last bar returned
        cursor = int(chunk[-1][0]) + bar_ms
        # Respect rate limit — Binance public: 1200 weight/min, klines = 2 weight
        time.sleep(0.15)
        print(f"    {symbol} {interval}: {len(all_bars):>6} bars fetched "
              f"(up to {pd.Timestamp(chunk[-1][0], unit='ms', tz='UTC').date()})",
              end="\r")
        if len(chunk) < 1000:
            break

    print()  # newline after \r progress

    if not all_bars:
        raise RuntimeError(f"No data returned for {symbol} {interval}")

    # Parse into DataFrame
    df = pd.DataFrame(all_bars, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "_"
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    df = df[["open", "high", "low", "close", "volume"]].astype(float)

    # Drop the final (possibly incomplete) bar
    now_ms = int(time.time() * 1000)
    last_bar_ms = int(pd.Timestamp(df.index[-1]).timestamp() * 1000)
    if now_ms - last_bar_ms < bar_ms:
        df = df.iloc[:-1]

    return df


def _get_date_range() -> tuple[int, int]:
    """Read start/end timestamps from the existing ETH 1h CSV."""
    ref = os.path.join(DATA_DIR, "ETHUSDT_1h.csv")
    df  = pd.read_csv(ref, parse_dates=["timestamp"], index_col="timestamp")
    start_dt = df.index[0]
    end_dt   = df.index[-1]
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)
    print(f"  Date range from ETHUSDT_1h.csv: {start_dt.date()} → {end_dt.date()}")
    return start_ms, end_ms


def main() -> None:
    print("\n" + "=" * 65)
    print("  HAWK Multi-TF Data Downloader")
    print("  Binance public REST — no API key needed")
    print("=" * 65 + "\n")

    start_ms, end_ms = _get_date_range()

    for symbol in SYMBOLS:
        for interval in INTERVALS:
            fname  = f"{symbol}_{interval}.csv"
            fpath  = os.path.join(DATA_DIR, fname)

            if os.path.exists(fpath):
                existing = pd.read_csv(fpath, parse_dates=["timestamp"], index_col="timestamp")
                print(f"  SKIP  {fname:25s} (already exists, {len(existing):>6} bars)")
                continue

            print(f"  Downloading {fname} ...")
            try:
                df = download_ohlcv(symbol, interval, start_ms, end_ms)
                df.index.name = "timestamp"
                df.to_csv(fpath)
                bar_ms = INTERVAL_MS[interval]
                hours  = bar_ms / 3_600_000
                days   = len(df) * hours / 24
                print(f"  SAVED {fname:25s}  {len(df):>6} bars  (~{days:.0f} days)")
            except Exception as exc:
                print(f"  ERROR {fname}: {exc}")

    print("\n  Done.\n")
    print("  Files available in data/:")
    for f in sorted(os.listdir(DATA_DIR)):
        fpath = os.path.join(DATA_DIR, f)
        size  = os.path.getsize(fpath) / 1024
        print(f"    {f:<30s}  {size:>7.1f} KB")
    print()


if __name__ == "__main__":
    main()
