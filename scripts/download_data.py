"""
Download historical OHLCV data from Binance (public endpoint, no API key needed).
Saves to data/<SYMBOL>_<TIMEFRAME>.csv for backtesting.

Usage:
    python scripts/download_data.py --symbol BTC/USDT --timeframe 1h --days 730
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import ccxt
import pandas as pd

log = logging.getLogger(__name__)
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def download(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    exchange = ccxt.binance({"enableRateLimit": True})
    exchange.load_markets()

    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    all_ohlcv = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        all_ohlcv.extend(batch)
        since = batch[-1][0] + tf_ms
        if since >= int(datetime.now(timezone.utc).timestamp() * 1000):
            break
        log.info("Fetched %d bars so far...", len(all_ohlcv))

    df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df.drop_duplicates(inplace=True)
    df.sort_index(inplace=True)
    return df


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="Download OHLCV data from Binance")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=730)
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    safe_sym = args.symbol.replace("/", "")
    out_path = os.path.join(DATA_DIR, f"{safe_sym}_{args.timeframe}.csv")

    log.info("Downloading %s %s for %d days...", args.symbol, args.timeframe, args.days)
    df = download(args.symbol, args.timeframe, args.days)
    df.to_csv(out_path)
    log.info("Saved %d bars → %s", len(df), out_path)


if __name__ == "__main__":
    main()
