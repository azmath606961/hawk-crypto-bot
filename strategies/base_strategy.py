"""Abstract base class for all trading strategies."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    Every strategy must implement:
      - on_tick(symbol, price, data)  → called every price update
      - on_close(position)            → called when a position is closed
      - name property
    """

    def __init__(self, symbol: str, config: dict) -> None:
        self.symbol = symbol
        self.config = config
        self.enabled: bool = True
        log.info("Strategy '%s' initialised for %s", self.name, symbol)

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def on_tick(self, price: float, data: Any) -> None:
        """Process a new price tick / candle."""
        ...

    def on_close(self, pnl_usdt: float) -> None:
        """Called by the engine after a position is closed. Override if needed."""
        pass

    def disable(self) -> None:
        self.enabled = False
        log.warning("Strategy '%s' for %s disabled", self.name, self.symbol)
