"""
Exchange wrapper around ccxt.
Handles authentication, market data fetching, order placement,
and normalises responses to simple dicts / DataFrames.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import ccxt
import pandas as pd

from utils.helpers import retry

log = logging.getLogger(__name__)


class ExchangeClient:
    """
    Thin ccxt wrapper with:
    - secure credential loading from environment variables
    - retry on network errors
    - fee/precision normalisation
    - OHLCV → DataFrame conversion
    """

    NETWORK_ERRORS = (
        ccxt.NetworkError,
        ccxt.RequestTimeout,
        ccxt.ExchangeNotAvailable,
    )

    def __init__(self, config: dict) -> None:
        ex_cfg = config["exchange"]
        api_key = os.environ.get(ex_cfg["api_key_env"], "")
        api_secret = os.environ.get(ex_cfg["api_secret_env"], "")

        exchange_class = getattr(ccxt, ex_cfg["name"])
        self._exchange: ccxt.Exchange = exchange_class(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": ex_cfg.get("rate_limit", True),
                "timeout": ex_cfg.get("timeout_ms", 10_000),
                "options": {"defaultType": "spot"},
            }
        )

        if ex_cfg.get("testnet", False):
            self._exchange.set_sandbox_mode(True)
            log.info("Exchange: %s TESTNET mode", ex_cfg["name"])
        else:
            log.warning("Exchange: %s LIVE mode", ex_cfg["name"])

        self._load_markets()
        self._fee_rate: float = config["risk"]["fee_rate"]

    def _load_markets(self) -> None:
        try:
            self._exchange.load_markets()
            log.info("Markets loaded: %d symbols", len(self._exchange.markets))
        except ccxt.BaseError as exc:
            log.error("Failed to load markets: %s", exc)
            raise

    # ------------------------------------------------------------------ #
    #  Market Data
    # ------------------------------------------------------------------ #

    @retry(max_attempts=3, delay_seconds=1.0, exceptions=NETWORK_ERRORS)
    def fetch_ticker(self, symbol: str) -> dict:
        return self._exchange.fetch_ticker(symbol)

    @retry(max_attempts=3, delay_seconds=1.0, exceptions=NETWORK_ERRORS)
    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        return self._exchange.fetch_order_book(symbol, limit)

    @retry(max_attempts=3, delay_seconds=1.5, exceptions=NETWORK_ERRORS)
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: int | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        raw = self._exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        if not raw:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df.sort_index()

    def get_mid_price(self, symbol: str) -> float:
        ob = self.fetch_order_book(symbol, limit=5)
        best_bid = ob["bids"][0][0] if ob["bids"] else 0.0
        best_ask = ob["asks"][0][0] if ob["asks"] else 0.0
        return (best_bid + best_ask) / 2

    # ------------------------------------------------------------------ #
    #  Account
    # ------------------------------------------------------------------ #

    @retry(max_attempts=3, delay_seconds=1.0, exceptions=NETWORK_ERRORS)
    def fetch_balance(self) -> dict[str, float]:
        bal = self._exchange.fetch_balance()
        return {k: float(v["free"]) for k, v in bal["total"].items() if float(v or 0) > 0}

    @retry(max_attempts=3, delay_seconds=1.0, exceptions=NETWORK_ERRORS)
    def fetch_open_orders(self, symbol: str) -> list[dict]:
        return self._exchange.fetch_open_orders(symbol)

    @retry(max_attempts=3, delay_seconds=1.0, exceptions=NETWORK_ERRORS)
    def fetch_order(self, order_id: str, symbol: str) -> dict:
        return self._exchange.fetch_order(order_id, symbol)

    # ------------------------------------------------------------------ #
    #  Order Placement
    # ------------------------------------------------------------------ #

    @retry(max_attempts=2, delay_seconds=0.5, exceptions=NETWORK_ERRORS)
    def place_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
    ) -> dict:
        self._log_order_intent("LIMIT", symbol, side, amount, price)
        order = self._exchange.create_order(symbol, "limit", side, amount, price)
        log.info("Limit order placed: %s", order["id"])
        return order

    @retry(max_attempts=2, delay_seconds=0.5, exceptions=NETWORK_ERRORS)
    def place_market_order(self, symbol: str, side: str, amount: float) -> dict:
        self._log_order_intent("MARKET", symbol, side, amount)
        order = self._exchange.create_order(symbol, "market", side, amount)
        log.info("Market order placed: %s", order["id"])
        return order

    @retry(max_attempts=2, delay_seconds=0.5, exceptions=NETWORK_ERRORS)
    def cancel_order(self, order_id: str, symbol: str) -> dict:
        result = self._exchange.cancel_order(order_id, symbol)
        log.info("Order cancelled: %s", order_id)
        return result

    def cancel_all_orders(self, symbol: str) -> list[dict]:
        open_orders = self.fetch_open_orders(symbol)
        cancelled = []
        for order in open_orders:
            try:
                cancelled.append(self.cancel_order(order["id"], symbol))
            except ccxt.BaseError as exc:
                log.warning("Could not cancel order %s: %s", order["id"], exc)
        return cancelled

    # ------------------------------------------------------------------ #
    #  Market info helpers
    # ------------------------------------------------------------------ #

    def get_min_amount(self, symbol: str) -> float:
        market = self._exchange.market(symbol)
        return float(market.get("limits", {}).get("amount", {}).get("min", 0) or 0)

    def get_amount_precision(self, symbol: str) -> float:
        market = self._exchange.market(symbol)
        return float(market.get("precision", {}).get("amount", 0.00001) or 0.00001)

    def get_price_precision(self, symbol: str) -> float:
        market = self._exchange.market(symbol)
        return float(market.get("precision", {}).get("price", 0.01) or 0.01)

    def get_fee_rate(self) -> float:
        return self._fee_rate

    # ------------------------------------------------------------------ #
    #  Internal
    # ------------------------------------------------------------------ #

    def _log_order_intent(
        self,
        order_type: str,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
    ) -> None:
        price_str = f"@ {price:.4f}" if price else "@ MARKET"
        log.info(
            "[ORDER] %s %s %s %.6f %s",
            order_type,
            side.upper(),
            symbol,
            amount,
            price_str,
        )
