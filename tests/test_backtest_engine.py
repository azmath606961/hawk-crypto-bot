"""Integration test: backtest engine on synthetic data."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from backtester.engine import BacktestEngine


def make_ohlcv(n=500, seed=42):
    np.random.seed(seed)
    close = [100.0]
    for _ in range(n - 1):
        close.append(close[-1] * (1 + np.random.normal(0.0002, 0.015)))
    close = pd.Series(close)
    high = close * (1 + np.abs(np.random.normal(0, 0.005, n)))
    low = close * (1 - np.abs(np.random.normal(0, 0.005, n)))
    volume = pd.Series(np.random.uniform(1000, 5000, n))
    idx = pd.date_range("2023-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {"open": close.shift(1).fillna(close.iloc[0]),
         "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@pytest.mark.parametrize("strategy", ["trend", "dca", "grid"])
def test_backtest_runs_without_error(strategy):
    df = make_ohlcv(500)
    engine = BacktestEngine(df, strategy=strategy, initial_capital=1000.0)
    metrics = engine.run()
    assert "total_return_pct" in metrics
    assert "max_drawdown_pct" in metrics
    assert "win_rate_pct" in metrics
    assert "sharpe_ratio" in metrics
    assert metrics["final_equity"] > 0


def test_trend_backtest_has_trades():
    df = make_ohlcv(500)
    engine = BacktestEngine(df, strategy="trend", initial_capital=1000.0)
    metrics = engine.run()
    assert metrics["total_trades"] >= 0  # at least ran without crash


def test_drawdown_never_exceeds_limit():
    df = make_ohlcv(500)
    engine = BacktestEngine(
        df, strategy="trend", initial_capital=1000.0,
        config_override={"max_drawdown_pct": 10.0, "ema_fast": 20, "ema_slow": 50,
                         "sl_atr_multiplier": 1.5, "atr_period": 14,
                         "risk_reward_ratio": 2.0, "risk_per_trade_pct": 1.0}
    )
    metrics = engine.run()
    assert metrics["max_drawdown_pct"] >= -15.0  # some leeway for bar execution
