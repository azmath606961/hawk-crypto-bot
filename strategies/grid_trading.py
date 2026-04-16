"""
Grid Trading Strategy
=====================
Places a ladder of BUY limit orders below current price and
SELL limit orders above, capturing profit from oscillation.

Features:
- Static or dynamic range (ATR-based)
- Auto-rebalance when price exits range
- Fee-aware P&L per grid level
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.exchange import ExchangeClient
from core.order_executor import OrderExecutor, OrderResult
from core.risk_manager import RiskManager
from monitor.logger import TradeLogger
from monitor.alerts import Alerter
from strategies.base_strategy import BaseStrategy
from utils.indicators import atr

log = logging.getLogger(__name__)


@dataclass
class GridLevel:
    price: float
    side: str          # buy | sell
    order_id: str = ""
    filled: bool = False
    fill_price: float = 0.0
    fill_time: Optional[datetime] = None


class GridTradingStrategy(BaseStrategy):
    """
    Grid trading: places N buy/sell orders across a price range.
    When a buy fills → place a paired sell one grid level above.
    When a sell fills → place a paired buy one grid level below.
    """

    @property
    def name(self) -> str:
        return "grid"

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

        grid_cfg = config["grid_trading"][symbol]
        self._lower: float = grid_cfg["lower_price"]
        self._upper: float = grid_cfg["upper_price"]
        self._num_grids: int = grid_cfg["num_grids"]
        self._order_usdt: float = grid_cfg["order_amount_usdt"]
        self._dynamic: bool = grid_cfg.get("dynamic_grid", True)
        self._atr_period: int = grid_cfg.get("atr_period", 14)
        self._atr_mult: float = grid_cfg.get("atr_multiplier", 3.0)
        self._rebalance_on_exit: bool = grid_cfg.get("rebalance_on_exit", True)

        self._levels: list[GridLevel] = []
        self._order_map: dict[str, GridLevel] = {}  # order_id → GridLevel
        self._initialised = False
        self._ohlcv_cache: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------ #
    #  Strategy lifecycle
    # ------------------------------------------------------------------ #

    def initialise_grid(self, current_price: float) -> None:
        """Call once at startup to place initial grid orders."""
        if self._dynamic:
            self._update_range_from_atr(current_price)

        self._levels = self._build_levels(current_price)
        self._cancel_all_existing()
        self._place_initial_orders(current_price)
        self._initialised = True
        log.info(
            "Grid initialised: %s | range [%.2f – %.2f] | %d levels",
            self.symbol,
            self._lower,
            self._upper,
            self._num_grids,
        )

    def on_tick(self, price: float, data: pd.DataFrame) -> None:
        if not self._initialised:
            self.initialise_grid(price)
            return

        # Cache OHLCV for ATR recalc
        self._ohlcv_cache = data

        # Check if price exited range
        if self._rebalance_on_exit and (price < self._lower or price > self._upper):
            log.warning(
                "Price %.4f outside grid range [%.2f–%.2f] — rebalancing",
                price,
                self._lower,
                self._upper,
            )
            self._alerter.notify("grid_rebalance", {
                "symbol": self.symbol,
                "price": price,
                "lower": self._lower,
                "upper": self._upper,
            })
            self.initialise_grid(price)
            return

        self._check_fills(price)

    # ------------------------------------------------------------------ #
    #  Fill checking & order pairing
    # ------------------------------------------------------------------ #

    def _check_fills(self, current_price: float) -> None:
        """Poll open orders — process any that have filled."""
        try:
            open_orders = self._ex.fetch_open_orders(self.symbol)
            open_ids = {o["id"] for o in open_orders}
        except Exception as exc:
            log.error("Could not fetch open orders: %s", exc)
            return

        for order_id, level in list(self._order_map.items()):
            if order_id not in open_ids and not level.filled:
                # Order no longer open → fetch to confirm fill
                try:
                    order = self._ex.fetch_order(order_id, self.symbol)
                    if order["status"] == "closed":
                        self._handle_fill(level, order, current_price)
                except Exception as exc:
                    log.warning("Could not check order %s: %s", order_id, exc)

    def _handle_fill(self, level: GridLevel, order: dict, current_price: float) -> None:
        fill_price = float(order.get("average", 0) or order.get("price", level.price))
        fee = float((order.get("fee") or {}).get("cost", 0) or 0)
        qty = float(order.get("filled", 0))
        level.filled = True
        level.fill_price = fill_price
        level.fill_time = datetime.now(timezone.utc)

        grid_interval = (self._upper - self._lower) / (self._num_grids - 1)

        if level.side == "buy":
            # Filled buy → place sell one level up
            sell_price = fill_price + grid_interval
            if sell_price <= self._upper:
                self._place_grid_order("sell", sell_price, qty)
            pnl = 0.0  # not realised yet
            log.info("Grid BUY filled @ %.4f for %s", fill_price, self.symbol)
            self._logger.log_trade(
                symbol=self.symbol,
                side="buy",
                price=fill_price,
                quantity=qty,
                pnl=pnl,
                fee=fee,
                strategy="grid",
                note="grid_buy_fill",
            )

        elif level.side == "sell":
            # Filled sell → realise profit (sell price - original buy price)
            buy_price = fill_price - grid_interval
            realised_pnl = (fill_price - buy_price) * qty - fee
            self._risk.record_close(realised_pnl)
            log.info(
                "Grid SELL filled @ %.4f | P&L: %+.4f USDT",
                fill_price,
                realised_pnl,
            )
            self._logger.log_trade(
                symbol=self.symbol,
                side="sell",
                price=fill_price,
                quantity=qty,
                pnl=realised_pnl,
                fee=fee,
                strategy="grid",
                note="grid_sell_fill",
            )
            # Place new buy at lower grid level
            buy_price_new = fill_price - grid_interval
            if buy_price_new >= self._lower:
                self._place_grid_order(
                    "buy", buy_price_new, self._order_usdt / buy_price_new
                )

    # ------------------------------------------------------------------ #
    #  Order helpers
    # ------------------------------------------------------------------ #

    def _build_levels(self, current_price: float) -> list[GridLevel]:
        interval = (self._upper - self._lower) / (self._num_grids - 1)
        levels = []
        for i in range(self._num_grids):
            price = self._lower + i * interval
            side = "buy" if price < current_price else "sell"
            levels.append(GridLevel(price=price, side=side))
        return levels

    def _place_initial_orders(self, current_price: float) -> None:
        for level in self._levels:
            if abs(level.price - current_price) / current_price < 0.001:
                continue  # skip levels too close to current price
            self._place_grid_order(
                level.side,
                level.price,
                self._order_usdt / level.price,
            )

    def _place_grid_order(self, side: str, price: float, qty: float) -> None:
        allowed, reason = self._risk.check_new_trade(self.symbol)
        if not allowed:
            log.warning("Grid order blocked by risk: %s", reason)
            return
        try:
            order = self._ex.place_limit_order(self.symbol, side, qty, price)
            level = GridLevel(price=price, side=side, order_id=order["id"])
            self._order_map[order["id"]] = level
        except Exception as exc:
            log.error("Failed to place grid %s @ %.4f: %s", side, price, exc)

    def _cancel_all_existing(self) -> None:
        cancelled = self._ex.cancel_all_orders(self.symbol)
        self._order_map.clear()
        log.info("Cancelled %d existing orders for %s", len(cancelled), self.symbol)

    def _update_range_from_atr(self, current_price: float) -> None:
        try:
            df = self._ex.fetch_ohlcv(self.symbol, timeframe="1h", limit=50)
            if len(df) < self._atr_period + 1:
                return
            atr_val = float(atr(df["high"], df["low"], df["close"], self._atr_period).iloc[-1])
            half_range = self._atr_mult * atr_val
            self._lower = max(0.0, current_price - half_range)
            self._upper = current_price + half_range
            log.info(
                "Dynamic grid range updated: [%.2f – %.2f] (ATR=%.4f)",
                self._lower,
                self._upper,
                atr_val,
            )
        except Exception as exc:
            log.warning("Could not update grid range from ATR: %s — using static config", exc)
