"""
Technical indicators used across strategies.
All functions accept a pandas Series / DataFrame and return a Series.
No external TA library required — pure pandas/numpy for transparency.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """Session VWAP (resets at midnight UTC)."""
    typical = (high + low + close) / 3
    cum_tp_vol = (typical * volume).cumsum()
    cum_vol = volume.cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, middle, lower)."""
    mid = sma(series, period)
    std = series.rolling(period).std(ddof=0)
    return mid + std_dev * std, mid, mid - std_dev * std


def ema_cross_signal(
    close: pd.Series,
    fast: int = 20,
    slow: int = 50,
    min_separation_bars: int = 3,
) -> pd.Series:
    """
    Returns 1 (bullish cross), -1 (bearish cross), 0 (no signal).
    Only fires after EMAs have been separated for min_separation_bars.
    """
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    above = (ema_fast > ema_slow).astype(int)
    cross_up = (above.diff() == 1)
    cross_dn = (above.diff() == -1)
    signal = pd.Series(0, index=close.index)
    signal[cross_up] = 1
    signal[cross_dn] = -1
    return signal
