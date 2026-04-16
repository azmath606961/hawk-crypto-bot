"""Unit tests for technical indicators."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from utils.indicators import atr, ema, rsi, sma, ema_cross_signal


def make_close(n=100, start=100.0, drift=0.001):
    np.random.seed(42)
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + drift + np.random.normal(0, 0.01)))
    return pd.Series(prices)


def test_ema_length():
    s = make_close(100)
    result = ema(s, 20)
    assert len(result) == len(s)


def test_ema_last_value_reasonable():
    s = pd.Series([100.0] * 50)
    result = ema(s, 10)
    assert abs(result.iloc[-1] - 100.0) < 0.01


def test_sma_simple():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = sma(s, 3)
    assert abs(result.iloc[-1] - 4.0) < 0.001


def test_atr_non_negative():
    close = make_close(50, 100.0)
    high = close * 1.01
    low = close * 0.99
    result = atr(high, low, close, 14)
    assert (result.dropna() >= 0).all()


def test_rsi_bounds():
    s = make_close(100)
    result = rsi(s, 14)
    valid = result.dropna()
    assert (valid >= 0).all()
    assert (valid <= 100).all()


def test_ema_cross_signal_fires():
    # Create an uptrend that forces a bullish cross
    close = pd.Series(
        [90.0] * 30 + [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    )
    signal = ema_cross_signal(close, fast=5, slow=10, min_separation_bars=1)
    assert (signal == 1).any() or (signal == -1).any()
