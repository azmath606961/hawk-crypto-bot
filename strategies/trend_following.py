"""
Trend Following Strategy (EMA20 / EMA50)
=========================================
Entry:  Price crosses above both EMAs (bullish) or below both (bearish).
        Waits for a pullback-and-confirm before entering.
Exit:   Take profit at RR=2 or stop-loss at 1.5×ATR.

One open trade per symbol at a time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import pandas as pd

from core.exchange import ExchangeClient
from core.order_executor import OrderExecutor
from core.portfolio import Portfolio, Position
from core.risk_manager import RiskManager
from monitor.alerts import Alerter
from monitor.logger import TradeLogger
from strategies.base_strategy import BaseStrategy
from utils.indicators import atr, ema

log = logging.getLogger(__name__)


class Signal(Enum):
    NONE = auto()
    LONG = auto()
    SHORT = auto()    # Spot: ignore shorts — treat as exit signal only
    EXIT = auto()


@dataclass
class TrendState:
    signal: Signal = Signal.NONE
    ema_fast_last: float = 0.0
    ema_slow_last: float = 0.0
    consecutive_above: int = 0   # bars where fast > slow
    consecutive_below: int = 0


class TrendFollowingStrategy(BaseStrategy):
    """EMA crossover with ATR stop-loss and fixed Risk:Reward take-profit."""

    @property
    def name(self) -> str:
        return "trend"

    def __init__(
        self,
        symbol: str,
        config: dict,
        exchange: ExchangeClient,
        executor: OrderExecutor,
        portfolio: Portfolio,
        risk: RiskManager,
        logger: TradeLogger,
        alerter: Alerter,
    ) -> None:
        super().__init__(symbol, config)
        self._ex = exchange
        self._executor = executor
        self._portfolio = portfolio
        self._risk = risk
        self._logger = logger
        self._alerter = alerter

        tf_cfg = config["trend_following"][symbol]
        self._timeframe: str = tf_cfg["timeframe"]
        self._ema_fast: int = tf_cfg["ema_fast"]
        self._ema_slow: int = tf_cfg["ema_slow"]
        self._rr: float = tf_cfg["risk_reward_ratio"]
        self._sl_atr_mult: float = tf_cfg["sl_atr_multiplier"]
        self._atr_period: int = tf_cfg["atr_period"]
        self._min_trend_bars: int = tf_cfg["min_trend_bars"]
        self._pos_size_pct: float = tf_cfg["position_size_pct"]

        self._state = TrendState()
        self._current_order_id: Optional[str] = None

    # ------------------------------------------------------------------ #
    #  Strategy lifecycle
    # ------------------------------------------------------------------ #

    def on_tick(self, price: float, data: pd.DataFrame) -> None:
        if len(data) < self._ema_slow + 5:
            return

        # Compute indicators on latest data
        close = data["close"]
        ema_fast_series = ema(close, self._ema_fast)
        ema_slow_series = ema(close, self._ema_slow)
        atr_series = atr(data["high"], data["low"], close, self._atr_period)

        ema_fast_val = float(ema_fast_series.iloc[-1])
        ema_slow_val = float(ema_slow_series.iloc[-1])
        atr_val = float(atr_series.iloc[-1])

        # Track trend bars
        if ema_fast_val > ema_slow_val:
            self._state.consecutive_above += 1
            self._state.consecutive_below = 0
        else:
            self._state.consecutive_below += 1
            self._state.consecutive_above = 0

        # Manage open position first
        if self._current_order_id:
            self._manage_open_position(price)
            return

        # Evaluate entry signal
        signal = self._evaluate_signal(price, ema_fast_val, ema_slow_val)
        if signal == Signal.LONG:
            self._enter_long(price, atr_val)

    def _evaluate_signal(
        self, price: float, ema_fast: float, ema_slow: float
    ) -> Signal:
        """
        Bullish: price > EMA_fast > EMA_slow for ≥ min_trend_bars,
                 then price pulls back to within 0.5% of EMA_fast (confirmation).
        """
        if self._state.consecutive_above < self._min_trend_bars:
            return Signal.NONE
        if ema_fast <= ema_slow:
            return Signal.NONE

        # Pullback to fast EMA (within 0.5%)
        near_ema = abs(price - ema_fast) / ema_fast < 0.005
        # Candle must close above EMA_fast (bullish confirmation)
        above_fast = price > ema_fast

        if near_ema and above_fast:
            log.info(
                "[%s] TREND SIGNAL: price %.4f near EMA%d %.4f → LONG",
                self.symbol,
                price,
                self._ema_fast,
                ema_fast,
            )
            return Signal.LONG

        return Signal.NONE

    # ------------------------------------------------------------------ #
    #  Execution
    # ------------------------------------------------------------------ #

    def _enter_long(self, price: float, atr_val: float) -> None:
        allowed, reason = self._risk.check_new_trade(self.symbol)
        if not allowed:
            log.info("Trend long blocked: %s", reason)
            return

        stop_loss = price - self._sl_atr_mult * atr_val
        take_profit = price + self._rr * self._sl_atr_mult * atr_val

        size_usdt = self._risk.position_size_usdt(price, stop_loss)
        if size_usdt < 10:
            log.warning("Position size too small (%.2f USDT) — skipping", size_usdt)
            return

        result = self._executor.execute_buy(self.symbol, size_usdt, limit_price=price)
        if not result.is_filled:
            log.warning("Trend long entry failed: %s", result.status)
            return

        position = Position(
            symbol=self.symbol,
            side="buy",
            entry_price=result.avg_price,
            quantity=result.filled_amount,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy="trend",
            order_id=result.order_id,
            fee_entry=result.fee_usdt,
        )
        self._portfolio.open_position(position)
        self._risk.record_open()
        self._current_order_id = result.order_id

        self._logger.log_trade(
            symbol=self.symbol,
            side="buy",
            price=result.avg_price,
            quantity=result.filled_amount,
            pnl=0.0,
            fee=result.fee_usdt,
            strategy="trend",
            note=f"SL={stop_loss:.4f} TP={take_profit:.4f}",
        )
        self._alerter.notify("trade_open", {
            "symbol": self.symbol,
            "side": "BUY",
            "price": result.avg_price,
            "sl": stop_loss,
            "tp": take_profit,
        })

    def _manage_open_position(self, current_price: float) -> None:
        pos = self._portfolio.get_position(self._current_order_id)
        if pos is None:
            self._current_order_id = None
            return

        exit_reason = None
        if pos.should_stop_loss(current_price):
            exit_reason = "stop_loss"
        elif pos.should_take_profit(current_price):
            exit_reason = "take_profit"

        # EMA crossover exit (bearish — fast crosses below slow)
        if exit_reason is None and self._state.consecutive_below >= self._min_trend_bars:
            exit_reason = "ema_reversal"

        if exit_reason:
            self._exit_position(pos, current_price, exit_reason)

    def _exit_position(self, pos: Position, price: float, reason: str) -> None:
        result = self._executor.execute_sell(self.symbol, pos.quantity, limit_price=price)
        exit_price = result.avg_price if result.avg_price > 0 else price
        pnl = (exit_price - pos.entry_price) * pos.quantity - pos.fee_entry - result.fee_usdt

        self._portfolio.close_position(pos.order_id)
        self._risk.record_close(pnl)
        self._current_order_id = None

        log.info(
            "Trend EXIT (%s): %s @ %.4f | P&L: %+.4f USDT",
            reason,
            self.symbol,
            exit_price,
            pnl,
        )
        self._logger.log_trade(
            symbol=self.symbol,
            side="sell",
            price=exit_price,
            quantity=pos.quantity,
            pnl=pnl,
            fee=result.fee_usdt,
            strategy="trend",
            note=reason,
        )
        event = "stop_loss_hit" if reason == "stop_loss" else "trade_close"
        self._alerter.notify(event, {
            "symbol": self.symbol,
            "reason": reason,
            "pnl": pnl,
            "price": exit_price,
        })
