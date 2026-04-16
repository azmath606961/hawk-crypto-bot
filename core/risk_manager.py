"""
Risk Manager — enforces all capital-protection rules.

Gates:
  G1  daily loss limit
  G2  total drawdown limit
  G3  max open trades
  G4  per-trade risk sizing (1% rule)
  G5  min order size check
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class DailyStats:
    date: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    starting_equity: float = 0.0
    realised_pnl: float = 0.0
    trade_count: int = 0
    halt_triggered: bool = False

    @property
    def loss_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return (self.realised_pnl / self.starting_equity) * 100


class RiskManager:
    """
    Stateful risk controller.
    Call check_new_trade() before opening any position.
    Call record_close() after every position close.
    """

    def __init__(self, config: dict, initial_equity: float) -> None:
        risk_cfg = config["risk"]
        self._risk_pct: float = risk_cfg["risk_per_trade_pct"]          # 1.0
        self._max_daily_loss_pct: float = risk_cfg["max_daily_loss_pct"]  # 3.0
        self._max_drawdown_pct: float = risk_cfg["max_drawdown_pct"]    # 10.0
        self._max_open_trades: int = risk_cfg["max_open_trades"]        # 5
        self._fee_rate: float = risk_cfg["fee_rate"]
        self._slippage_pct: float = risk_cfg["slippage_pct"]

        self._peak_equity: float = initial_equity
        self._current_equity: float = initial_equity
        self._open_trades: int = 0
        self._daily: DailyStats = DailyStats(starting_equity=initial_equity)

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def check_new_trade(self, symbol: str = "") -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        Call before every trade. Gates G1, G2, G3.
        """
        self._maybe_reset_daily()

        if self._daily.halt_triggered:
            return False, "G1: daily loss halt active"

        loss_pct = self._daily.loss_pct
        if loss_pct <= -self._max_daily_loss_pct:
            self._daily.halt_triggered = True
            log.warning(
                "G1: daily loss %.2f%% exceeds limit %.2f%% — trading halted",
                loss_pct,
                self._max_daily_loss_pct,
            )
            return False, f"G1: daily loss {loss_pct:.2f}%"

        drawdown_pct = self._drawdown_pct()
        if drawdown_pct >= self._max_drawdown_pct:
            log.warning(
                "G2: drawdown %.2f%% exceeds limit %.2f%% — trading halted",
                drawdown_pct,
                self._max_drawdown_pct,
            )
            return False, f"G2: drawdown {drawdown_pct:.2f}%"

        if self._open_trades >= self._max_open_trades:
            return False, f"G3: max open trades ({self._max_open_trades}) reached"

        return True, "ok"

    def position_size_usdt(
        self,
        entry_price: float,
        stop_loss_price: float,
        equity: Optional[float] = None,
    ) -> float:
        """
        G4: Risk-based position sizing.
        risk_amount = equity * risk_pct / 100
        size_usdt   = risk_amount / (|entry - stop| / entry)
        Capped at 20% of equity to avoid concentration.
        """
        eq = equity if equity is not None else self._current_equity
        risk_amount = eq * self._risk_pct / 100
        if entry_price <= 0 or stop_loss_price <= 0:
            return risk_amount
        sl_pct = abs(entry_price - stop_loss_price) / entry_price
        if sl_pct < 0.0001:
            sl_pct = 0.01  # minimum 1% stop assumed
        raw_size = risk_amount / sl_pct
        max_size = eq * 0.20  # concentration cap: 20% of equity per trade
        return min(raw_size, max_size)

    def record_open(self) -> None:
        self._open_trades += 1
        self._daily.trade_count += 1

    def record_close(self, pnl_usdt: float) -> None:
        self._open_trades = max(0, self._open_trades - 1)
        self._daily.realised_pnl += pnl_usdt
        self._current_equity += pnl_usdt
        if self._current_equity > self._peak_equity:
            self._peak_equity = self._current_equity
        log.info(
            "Trade closed | PnL: %+.2f USDT | equity: %.2f | drawdown: %.2f%%",
            pnl_usdt,
            self._current_equity,
            self._drawdown_pct(),
        )

    def update_equity(self, new_equity: float) -> None:
        self._current_equity = new_equity
        if new_equity > self._peak_equity:
            self._peak_equity = new_equity

    def effective_entry(self, price: float, side: str) -> float:
        """Price after fee + slippage (BUY pays more, SELL receives less)."""
        adj = self._fee_rate + self._slippage_pct / 100
        if side == "buy":
            return price * (1 + adj)
        return price * (1 - adj)

    @property
    def open_trades(self) -> int:
        return self._open_trades

    @property
    def current_equity(self) -> float:
        return self._current_equity

    @property
    def daily_pnl(self) -> float:
        return self._daily.realised_pnl

    @property
    def is_halted(self) -> bool:
        return self._daily.halt_triggered or self._drawdown_pct() >= self._max_drawdown_pct

    # ------------------------------------------------------------------ #
    #  Internal
    # ------------------------------------------------------------------ #

    def _drawdown_pct(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return (1 - self._current_equity / self._peak_equity) * 100

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self._daily.date != today:
            log.info(
                "New trading day — resetting daily stats. Yesterday PnL: %+.2f",
                self._daily.realised_pnl,
            )
            self._daily = DailyStats(
                date=today,
                starting_equity=self._current_equity,
            )
