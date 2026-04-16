"""
Trade Logger — appends every trade to a CSV with full metadata.
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

TRADE_FIELDS = [
    "timestamp",
    "symbol",
    "side",
    "price",
    "quantity",
    "cost_usdt",
    "pnl_usdt",
    "fee_usdt",
    "strategy",
    "note",
]


class TradeLogger:
    def __init__(self, config: dict) -> None:
        log_cfg = config["logging"]
        self._log_dir = log_cfg["log_dir"]
        os.makedirs(self._log_dir, exist_ok=True)
        self._csv_path = os.path.join(self._log_dir, log_cfg["trade_log_csv"])
        self._ensure_header()

        logging.basicConfig(
            level=getattr(logging, log_cfg.get("level", "INFO")),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(
                    os.path.join(self._log_dir, "bot.log"), encoding="utf-8"
                ),
            ],
        )

    def log_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: float,
        pnl: float,
        fee: float,
        strategy: str,
        note: str = "",
    ) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "price": f"{price:.6f}",
            "quantity": f"{quantity:.8f}",
            "cost_usdt": f"{price * quantity:.4f}",
            "pnl_usdt": f"{pnl:.4f}",
            "fee_usdt": f"{fee:.6f}",
            "strategy": strategy,
            "note": note,
        }
        with open(self._csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            writer.writerow(row)
        log.info(
            "TRADE | %s | %s | %s | qty=%.6f | price=%.4f | pnl=%+.4f",
            symbol,
            side.upper(),
            strategy,
            quantity,
            price,
            pnl,
        )

    def _ensure_header(self) -> None:
        if not os.path.exists(self._csv_path):
            with open(self._csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
                writer.writeheader()
