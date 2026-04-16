"""
Performance metrics for backtesting results.
All functions accept a pandas Series of equity curve or a list of trade PnLs.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd


def total_return_pct(equity_curve: pd.Series) -> float:
    if len(equity_curve) < 2 or equity_curve.iloc[0] == 0:
        return 0.0
    return (equity_curve.iloc[-1] / equity_curve.iloc[0] - 1) * 100


def max_drawdown_pct(equity_curve: pd.Series) -> float:
    if len(equity_curve) < 2:
        return 0.0
    rolling_max = equity_curve.cummax()
    drawdown = (equity_curve - rolling_max) / rolling_max * 100
    return float(drawdown.min())


def sharpe_ratio(
    returns: pd.Series,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
) -> float:
    """Annualised Sharpe ratio from per-period returns."""
    if len(returns) < 2:
        return 0.0
    excess = returns - risk_free_rate / periods_per_year
    std = excess.std()
    if std == 0:
        return 0.0
    return float(excess.mean() / std * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: pd.Series,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.0,
) -> float:
    if len(returns) < 2:
        return 0.0
    excess = returns - risk_free_rate / periods_per_year
    downside = excess[excess < 0]
    downside_std = downside.std()
    if downside_std == 0:
        return 0.0
    return float(excess.mean() / downside_std * math.sqrt(periods_per_year))


def win_rate(pnl_series: pd.Series) -> float:
    if len(pnl_series) == 0:
        return 0.0
    return float((pnl_series > 0).sum() / len(pnl_series) * 100)


def profit_factor(pnl_series: pd.Series) -> float:
    gross_profit = pnl_series[pnl_series > 0].sum()
    gross_loss = pnl_series[pnl_series < 0].abs().sum()
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return float(gross_profit / gross_loss)


def avg_win_loss_ratio(pnl_series: pd.Series) -> float:
    wins = pnl_series[pnl_series > 0]
    losses = pnl_series[pnl_series < 0].abs()
    if losses.empty or wins.empty:
        return 0.0
    return float(wins.mean() / losses.mean())


def compute_all(
    equity_curve: pd.Series,
    pnl_series: pd.Series,
    periods_per_year: int = 252,
) -> dict:
    returns = equity_curve.pct_change().dropna()
    return {
        "total_return_pct": round(total_return_pct(equity_curve), 2),
        "max_drawdown_pct": round(max_drawdown_pct(equity_curve), 2),
        "sharpe_ratio": round(sharpe_ratio(returns, periods_per_year), 3),
        "sortino_ratio": round(sortino_ratio(returns, periods_per_year), 3),
        "win_rate_pct": round(win_rate(pnl_series), 2),
        "profit_factor": round(profit_factor(pnl_series), 3),
        "avg_win_loss_ratio": round(avg_win_loss_ratio(pnl_series), 3),
        "total_trades": len(pnl_series),
        "winning_trades": int((pnl_series > 0).sum()),
        "losing_trades": int((pnl_series < 0).sum()),
        "total_pnl_usdt": round(float(pnl_series.sum()), 4),
        "final_equity": round(float(equity_curve.iloc[-1]), 4),
    }
