"""
Paper Trading Exchange Simulator
==================================
Simulates order fills using real Binance prices (read-only API).
No real orders are placed. Suitable for validating strategies risk-free.

Drop-in replacement for ExchangeClient in paper mode.
Fill simulation:
  - Limit orders fill if price crosses the level within next tick.
  - Market orders fill immediately at current mid-price + slippage.
"""
from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

_PAPER_LOG_PREFIX = "[PAPER]"


class PaperOrder:
    def __init__(
        self,
        order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float,
    ) -> None:
        self.id = order_id
        self.symbol = symbol
        self.side = side
        self.type = order_type
        self.amount = amount
        self.price = price
        self.filled = 0.0
        self.status = "open"
        self.average: Optional[float] = None
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.fee = {"currency": "USDT", "cost": 0.0}


class PaperExchange:
    """
    Wraps a real ccxt exchange for market data but intercepts all order calls.
    Maintains a local order book for paper fills.
    """

    def __init__(self, real_exchange, config: dict) -> None:
        self._real = real_exchange        # real ccxt exchange (read-only)
        self._fee_rate: float = config["risk"]["fee_rate"]
        self._slippage_pct: float = config["risk"]["slippage_pct"]
        self._balances: dict[str, float] = {"USDT": config["risk"]["capital_usdt"]}
        self._open_orders: dict[str, PaperOrder] = {}
        log.info("%s Paper trading mode active (no real orders)", _PAPER_LOG_PREFIX)

    # ------------------------------------------------------------------ #
    #  Delegate market data to real exchange
    # ------------------------------------------------------------------ #

    def fetch_ticker(self, symbol: str) -> dict:
        return self._real.fetch_ticker(symbol)

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        return self._real.fetch_order_book(symbol, limit)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        return self._real.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)

    def get_mid_price(self, symbol: str) -> float:
        return self._real.get_mid_price(symbol)

    def get_min_amount(self, symbol: str) -> float:
        return self._real.get_min_amount(symbol)

    def get_amount_precision(self, symbol: str) -> float:
        return self._real.get_amount_precision(symbol)

    def get_price_precision(self, symbol: str) -> float:
        return self._real.get_price_precision(symbol)

    def get_fee_rate(self) -> float:
        return self._fee_rate

    # ------------------------------------------------------------------ #
    #  Paper account
    # ------------------------------------------------------------------ #

    def fetch_balance(self) -> dict[str, float]:
        return dict(self._balances)

    def fetch_open_orders(self, symbol: str) -> list[dict]:
        return [
            self._order_to_dict(o)
            for o in self._open_orders.values()
            if o.symbol == symbol
        ]

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        order = self._open_orders.get(order_id)
        if order is None:
            # Simulate an already-filled order
            return {
                "id": order_id,
                "status": "closed",
                "filled": 0.0,
                "average": 0.0,
                "fee": {"cost": 0.0},
            }
        # Simulate fill check: try to fill on current price
        self._try_fill_order(order)
        return self._order_to_dict(order)

    # ------------------------------------------------------------------ #
    #  Paper order placement
    # ------------------------------------------------------------------ #

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
    ) -> dict:
        order_id = str(uuid.uuid4())[:8]
        order = PaperOrder(order_id, symbol, side, "limit", amount, price)
        self._open_orders[order_id] = order

        # Try immediate fill if price is favourable
        self._try_fill_order(order)

        log.info(
            "%s LIMIT %s %s qty=%.6f @ %.4f [id=%s]",
            _PAPER_LOG_PREFIX,
            side.upper(),
            symbol,
            amount,
            price,
            order_id,
        )
        return self._order_to_dict(order)

    def place_market_order(self, symbol: str, side: str, amount: float) -> dict:
        order_id = str(uuid.uuid4())[:8]
        mid = self.get_mid_price(symbol)
        slip = self._slippage_pct / 100
        fill_price = mid * (1 + slip) if side == "buy" else mid * (1 - slip)
        fee_cost = amount * fill_price * self._fee_rate

        order = PaperOrder(order_id, symbol, side, "market", amount, fill_price)
        order.filled = amount
        order.average = fill_price
        order.status = "closed"
        order.fee = {"currency": "USDT", "cost": fee_cost}

        self._apply_fill(order, fill_price, amount)
        log.info(
            "%s MARKET %s %s qty=%.6f @ %.4f",
            _PAPER_LOG_PREFIX,
            side.upper(),
            symbol,
            amount,
            fill_price,
        )
        return self._order_to_dict(order)

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        order = self._open_orders.pop(order_id, None)
        if order:
            order.status = "canceled"
            log.info("%s Cancelled order %s", _PAPER_LOG_PREFIX, order_id)
            return self._order_to_dict(order)
        return {"id": order_id, "status": "canceled"}

    def cancel_all_orders(self, symbol: str) -> list[dict]:
        to_cancel = [
            oid for oid, o in self._open_orders.items() if o.symbol == symbol
        ]
        return [self.cancel_order(oid, symbol) for oid in to_cancel]

    def load_markets(self) -> None:
        pass  # delegated to real exchange at startup

    # ------------------------------------------------------------------ #
    #  Internal
    # ------------------------------------------------------------------ #

    def _try_fill_order(self, order: PaperOrder) -> None:
        if order.status != "open":
            return
        mid = self.get_mid_price(order.symbol)
        slip = self._slippage_pct / 100
        filled = False

        if order.side == "buy" and mid <= order.price * (1 + slip):
            fill_price = order.price * (1 + slip)
            filled = True
        elif order.side == "sell" and mid >= order.price * (1 - slip):
            fill_price = order.price * (1 - slip)
            filled = True
        else:
            return

        if filled:
            fee_cost = order.amount * fill_price * self._fee_rate
            order.filled = order.amount
            order.average = fill_price
            order.status = "closed"
            order.fee = {"currency": "USDT", "cost": fee_cost}
            self._apply_fill(order, fill_price, order.amount)
            self._open_orders.pop(order.id, None)

    def _apply_fill(self, order: PaperOrder, price: float, qty: float) -> None:
        base, quote = order.symbol.split("/")
        fee = qty * price * self._fee_rate

        if order.side == "buy":
            cost = qty * price + fee
            if self._balances.get("USDT", 0) >= cost:
                self._balances["USDT"] = self._balances.get("USDT", 0) - cost
                self._balances[base] = self._balances.get(base, 0.0) + qty
        else:
            proceeds = qty * price - fee
            self._balances[base] = max(0.0, self._balances.get(base, 0.0) - qty)
            self._balances["USDT"] = self._balances.get("USDT", 0.0) + proceeds

    @staticmethod
    def _order_to_dict(order: PaperOrder) -> dict:
        return {
            "id": order.id,
            "symbol": order.symbol,
            "side": order.side,
            "type": order.type,
            "amount": order.amount,
            "price": order.price,
            "filled": order.filled,
            "average": order.average,
            "status": order.status,
            "fee": order.fee,
            "timestamp": order.timestamp,
        }
