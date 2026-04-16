"""
Smart order executor.
1. Try limit order at best-bid/ask.
2. If not filled within timeout → fallback to market order.
3. All results normalised to OrderResult dataclass.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from core.exchange import ExchangeClient
from utils.helpers import round_step

log = logging.getLogger(__name__)

LIMIT_FILL_TIMEOUT_S = 30    # seconds to wait for limit fill
POLL_INTERVAL_S = 2          # check fill status every N seconds


class OrderStatus(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    OPEN = "open"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderResult:
    symbol: str
    side: str                       # buy | sell
    order_type: str                 # limit | market
    requested_amount: float
    filled_amount: float
    avg_price: float
    fee_usdt: float
    status: OrderStatus
    order_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict = field(default_factory=dict)

    @property
    def cost_usdt(self) -> float:
        return self.filled_amount * self.avg_price

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED


class OrderExecutor:
    """
    Executes orders with limit-first, market-fallback logic.
    Fee-aware: computes actual fee from exchange response.
    """

    def __init__(self, exchange: ExchangeClient, config: dict) -> None:
        self._ex = exchange
        self._fee_rate: float = config["risk"]["fee_rate"]
        self._slippage_pct: float = config["risk"]["slippage_pct"]

    # ------------------------------------------------------------------ #
    #  Public
    # ------------------------------------------------------------------ #

    def execute_buy(
        self,
        symbol: str,
        amount_usdt: float,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        return self._execute(symbol, "buy", amount_usdt, limit_price)

    def execute_sell(
        self,
        symbol: str,
        amount_base: float,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        return self._execute(symbol, "sell", amount_base, limit_price, is_base_amount=True)

    # ------------------------------------------------------------------ #
    #  Internal
    # ------------------------------------------------------------------ #

    def _execute(
        self,
        symbol: str,
        side: str,
        amount: float,
        limit_price: Optional[float],
        is_base_amount: bool = False,
    ) -> OrderResult:
        """
        For buys  : amount is in USDT → convert to base quantity.
        For sells : amount is in base asset.
        """
        ticker = self._ex.fetch_ticker(symbol)
        mid = (ticker["bid"] + ticker["ask"]) / 2

        if not is_base_amount:
            # Convert USDT → base quantity
            base_qty = round_step(amount / mid, self._ex.get_amount_precision(symbol))
        else:
            base_qty = round_step(amount, self._ex.get_amount_precision(symbol))

        min_qty = self._ex.get_min_amount(symbol)
        if base_qty < min_qty:
            log.warning(
                "Order size %.6f < min %.6f for %s — skipped", base_qty, min_qty, symbol
            )
            return self._failed_result(symbol, side, base_qty)

        # --- Try limit order first ---
        if limit_price is None:
            limit_price = ticker["bid"] if side == "buy" else ticker["ask"]

        limit_price = round_step(limit_price, self._ex.get_price_precision(symbol))

        try:
            order = self._ex.place_limit_order(symbol, side, base_qty, limit_price)
            order_id = order["id"]

            # Poll for fill
            filled_order = self._wait_for_fill(symbol, order_id)
            if filled_order["status"] == "closed":
                return self._build_result(filled_order, symbol, side, "limit")

            # Timeout — cancel and fall back to market
            log.info("Limit order %s not filled in time — cancelling and using market", order_id)
            self._ex.cancel_order(order_id, symbol)
            partially_filled = float(filled_order.get("filled", 0))
            remaining = base_qty - partially_filled
            if remaining > min_qty:
                market_order = self._ex.place_market_order(symbol, side, remaining)
                return self._build_result(market_order, symbol, side, "market")
            elif partially_filled > 0:
                return self._build_result(filled_order, symbol, side, "limit_partial")
            else:
                return self._failed_result(symbol, side, base_qty)

        except Exception as exc:
            log.error("Order execution failed for %s %s: %s", side, symbol, exc)
            # Last resort: market order
            try:
                market_order = self._ex.place_market_order(symbol, side, base_qty)
                return self._build_result(market_order, symbol, side, "market_fallback")
            except Exception as exc2:
                log.error("Market fallback also failed: %s", exc2)
                return self._failed_result(symbol, side, base_qty)

    def _wait_for_fill(self, symbol: str, order_id: str) -> dict:
        deadline = time.time() + LIMIT_FILL_TIMEOUT_S
        while time.time() < deadline:
            order = self._ex.fetch_order(order_id, symbol)
            if order["status"] in ("closed", "canceled"):
                return order
            time.sleep(POLL_INTERVAL_S)
        return self._ex.fetch_order(order_id, symbol)

    def _build_result(self, order: dict, symbol: str, side: str, order_type: str) -> OrderResult:
        filled = float(order.get("filled", 0) or 0)
        avg_price = float(order.get("average", 0) or order.get("price", 0) or 0)
        fee_info = order.get("fee", {}) or {}
        fee_usdt = float(fee_info.get("cost", 0) or 0)
        if fee_usdt == 0:
            fee_usdt = filled * avg_price * self._fee_rate

        status_map = {
            "closed": OrderStatus.FILLED,
            "open": OrderStatus.OPEN,
            "canceled": OrderStatus.CANCELLED,
        }
        status = status_map.get(order.get("status", ""), OrderStatus.PARTIAL)

        return OrderResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            requested_amount=float(order.get("amount", filled)),
            filled_amount=filled,
            avg_price=avg_price,
            fee_usdt=fee_usdt,
            status=status,
            order_id=str(order.get("id", "")),
            raw=order,
        )

    @staticmethod
    def _failed_result(symbol: str, side: str, amount: float) -> OrderResult:
        return OrderResult(
            symbol=symbol,
            side=side,
            order_type="none",
            requested_amount=amount,
            filled_amount=0.0,
            avg_price=0.0,
            fee_usdt=0.0,
            status=OrderStatus.FAILED,
        )
