"""
Dollar-Cost Averaging Strategy
================================
Buys a fixed USDT amount at regular intervals.
Optional dip-buying: 2× allocation when price drops ≥ N% in 24h.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from core.exchange import ExchangeClient
from core.order_executor import OrderExecutor
from core.risk_manager import RiskManager
from monitor.alerts import Alerter
from monitor.logger import TradeLogger
from strategies.base_strategy import BaseStrategy

log = logging.getLogger(__name__)

FREQUENCY_TO_SECONDS = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
}


class DCAStrategy(BaseStrategy):
    """
    Scheduled DCA with optional dip-buy multiplier.
    State is in-memory (restarts reset the interval timer).
    """

    @property
    def name(self) -> str:
        return "dca"

    def __init__(
        self,
        symbol: str,
        config: dict,
        exchange: ExchangeClient,
        executor: OrderExecutor,
        risk: RiskManager,
        logger: TradeLogger,
        alerter: Alerter,
    ) -> None:
        super().__init__(symbol, config)
        self._ex = exchange
        self._executor = executor
        self._risk = risk
        self._logger = logger
        self._alerter = alerter

        dca_cfg = config["dca"][symbol]
        self._base_usdt: float = dca_cfg["base_amount_usdt"]
        freq = dca_cfg.get("frequency", "daily")
        self._interval_s: int = FREQUENCY_TO_SECONDS.get(freq, 86400)
        self._dip_enabled: bool = dca_cfg.get("dip_buy_enabled", True)
        self._dip_threshold: float = dca_cfg.get("dip_threshold_pct", 3.0)
        self._dip_mult: float = dca_cfg.get("dip_multiplier", 2.0)
        self._max_per_day: int = dca_cfg.get("max_dca_per_day", 3)

        self._last_buy_time: Optional[datetime] = None
        self._daily_buy_count: int = 0
        self._daily_count_date: Optional[datetime] = None
        self._price_24h_ago: Optional[float] = None
        self._last_price_check: Optional[datetime] = None

    # ------------------------------------------------------------------ #

    def on_tick(self, price: float, data: pd.DataFrame) -> None:
        now = datetime.now(timezone.utc)
        self._reset_daily_count_if_needed(now)
        self._update_24h_reference(price, now, data)

        if self._daily_buy_count >= self._max_per_day:
            return

        if self._risk.is_halted:
            return

        # Regular scheduled buy
        if self._is_due(now):
            self._execute_dca(price, now, reason="scheduled")
            return

        # Dip buy (independent of schedule)
        if self._dip_enabled and self._is_dip(price):
            log.info("[%s] Dip detected — executing dip buy", self.symbol)
            self._execute_dca(price, now, reason="dip", multiplier=self._dip_mult)

    # ------------------------------------------------------------------ #
    #  Execution
    # ------------------------------------------------------------------ #

    def _execute_dca(
        self,
        price: float,
        now: datetime,
        reason: str,
        multiplier: float = 1.0,
    ) -> None:
        allowed, block_reason = self._risk.check_new_trade(self.symbol)
        if not allowed:
            log.info("DCA blocked: %s", block_reason)
            return

        amount_usdt = self._base_usdt * multiplier
        result = self._executor.execute_buy(self.symbol, amount_usdt, limit_price=price)
        if not result.is_filled:
            log.warning("DCA buy failed: %s", result.status)
            return

        self._last_buy_time = now
        self._daily_buy_count += 1
        self._risk.record_open()
        # DCA is accumulation — no stop-loss. Record as closed immediately
        # (DCA positions are tracked by exchange balance, not this bot's state).
        self._risk.record_close(0.0)

        log.info(
            "DCA %s: %s %.4f @ %.4f USDT (%.2f USDT spent, reason=%s)",
            self.symbol,
            result.symbol,
            result.filled_amount,
            result.avg_price,
            result.cost_usdt,
            reason,
        )
        self._logger.log_trade(
            symbol=self.symbol,
            side="buy",
            price=result.avg_price,
            quantity=result.filled_amount,
            pnl=0.0,
            fee=result.fee_usdt,
            strategy="dca",
            note=reason,
        )
        self._alerter.notify("trade_open", {
            "symbol": self.symbol,
            "side": "DCA BUY",
            "price": result.avg_price,
            "amount_usdt": result.cost_usdt,
            "reason": reason,
        })

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _is_due(self, now: datetime) -> bool:
        if self._last_buy_time is None:
            return True
        elapsed = (now - self._last_buy_time).total_seconds()
        return elapsed >= self._interval_s

    def _is_dip(self, price: float) -> bool:
        if self._price_24h_ago is None or self._price_24h_ago <= 0:
            return False
        drop_pct = (self._price_24h_ago - price) / self._price_24h_ago * 100
        return drop_pct >= self._dip_threshold

    def _update_24h_reference(
        self, price: float, now: datetime, data: pd.DataFrame
    ) -> None:
        """Use OHLCV close from ~24h ago as reference for dip detection."""
        if len(data) < 25:
            return
        cutoff = now - timedelta(hours=24)
        past = data[data.index <= cutoff]
        if not past.empty:
            self._price_24h_ago = float(past["close"].iloc[-1])

    def _reset_daily_count_if_needed(self, now: datetime) -> None:
        today = now.date()
        if self._daily_count_date is None or self._daily_count_date != today:
            self._daily_buy_count = 0
            self._daily_count_date = today
