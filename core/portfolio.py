"""
Portfolio tracker.
Maintains open positions, equity snapshots, and P&L calculation.
Backed by a CSV for persistence across restarts.
"""
from __future__ import annotations

import csv
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    side: str                      # buy (long)
    entry_price: float
    quantity: float                # base asset
    stop_loss: float
    take_profit: float
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    strategy: str = ""
    order_id: str = ""
    fee_entry: float = 0.0

    @property
    def cost_usdt(self) -> float:
        return self.entry_price * self.quantity

    def unrealised_pnl(self, current_price: float) -> float:
        if self.side == "buy":
            return (current_price - self.entry_price) * self.quantity - self.fee_entry
        return (self.entry_price - current_price) * self.quantity - self.fee_entry

    def pnl_pct(self, current_price: float) -> float:
        if self.cost_usdt <= 0:
            return 0.0
        return self.unrealised_pnl(current_price) / self.cost_usdt * 100

    def should_stop_loss(self, current_price: float) -> bool:
        if self.side == "buy":
            return current_price <= self.stop_loss
        return current_price >= self.stop_loss

    def should_take_profit(self, current_price: float) -> bool:
        if self.side == "buy":
            return current_price >= self.take_profit
        return current_price <= self.take_profit


class Portfolio:
    """
    Tracks open positions and equity curve.
    Thread-safe for single-threaded event loop (no locking needed).
    """

    def __init__(self, config: dict, initial_equity: float) -> None:
        log_cfg = config["logging"]
        self._log_dir = log_cfg["log_dir"]
        os.makedirs(self._log_dir, exist_ok=True)

        self._equity_csv = os.path.join(self._log_dir, log_cfg["equity_log_csv"])
        self._initial_equity = initial_equity
        self._positions: dict[str, Position] = {}  # order_id → Position

        self._write_equity_header()

    # ------------------------------------------------------------------ #
    #  Positions
    # ------------------------------------------------------------------ #

    def open_position(self, position: Position) -> None:
        self._positions[position.order_id] = position
        log.info(
            "Position opened: %s %s @ %.4f | SL: %.4f | TP: %.4f",
            position.symbol,
            position.side,
            position.entry_price,
            position.stop_loss,
            position.take_profit,
        )

    def close_position(self, order_id: str) -> Optional[Position]:
        pos = self._positions.pop(order_id, None)
        if pos:
            log.info("Position closed: %s (order %s)", pos.symbol, order_id)
        return pos

    def get_position(self, order_id: str) -> Optional[Position]:
        return self._positions.get(order_id)

    def positions_for_symbol(self, symbol: str) -> list[Position]:
        return [p for p in self._positions.values() if p.symbol == symbol]

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def open_count(self) -> int:
        return len(self._positions)

    # ------------------------------------------------------------------ #
    #  Equity
    # ------------------------------------------------------------------ #

    def snapshot_equity(self, equity: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        row = {"timestamp": now, "equity_usdt": f"{equity:.4f}"}
        self._append_csv(self._equity_csv, row, fieldnames=["timestamp", "equity_usdt"])
        log.debug("Equity snapshot: %.4f USDT", equity)

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _write_equity_header(self) -> None:
        if not os.path.exists(self._equity_csv):
            with open(self._equity_csv, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["timestamp", "equity_usdt"])
                writer.writeheader()

    @staticmethod
    def _append_csv(path: str, row: dict, fieldnames: list[str]) -> None:
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
