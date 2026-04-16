"""
CLI Dashboard — renders a live terminal summary every N seconds.
Uses only stdlib (no rich/curses required) for maximum portability.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Optional

from core.portfolio import Portfolio
from core.risk_manager import RiskManager


def clear_screen() -> None:
    os.system("cls" if sys.platform == "win32" else "clear")


class Dashboard:
    def __init__(
        self,
        portfolio: Portfolio,
        risk: RiskManager,
        symbols: list[str],
        refresh_s: int = 10,
    ) -> None:
        self._portfolio = portfolio
        self._risk = risk
        self._symbols = symbols
        self._refresh_s = refresh_s
        self._prices: dict[str, float] = {}
        self._start_time = datetime.now(timezone.utc)

    def update_prices(self, prices: dict[str, float]) -> None:
        self._prices.update(prices)

    def render(self) -> None:
        clear_screen()
        now = datetime.now(timezone.utc)
        uptime = now - self._start_time
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        mins, secs = divmod(rem, 60)

        print("=" * 60)
        print(f"  CRYPTO BOT  |  {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"  Uptime: {hours:02d}h {mins:02d}m {secs:02d}s")
        print("=" * 60)

        # Account
        eq = self._risk.current_equity
        daily_pnl = self._risk.daily_pnl
        status = "HALTED" if self._risk.is_halted else "RUNNING"
        halted_str = f"  *** {status} ***" if self._risk.is_halted else ""
        print(f"\n  Equity   : {eq:>12,.2f} USDT{halted_str}")
        print(f"  Daily P&L: {daily_pnl:>+12.4f} USDT")
        print(f"  Open pos : {self._portfolio.open_count:>3}")

        # Open positions
        positions = self._portfolio.all_positions()
        if positions:
            print(f"\n  {'Symbol':<12} {'Side':<5} {'Entry':>10} {'Current':>10} {'UPnL':>10}")
            print("  " + "-" * 52)
            for pos in positions:
                cur = self._prices.get(pos.symbol, pos.entry_price)
                upnl = pos.unrealised_pnl(cur)
                pnl_str = f"{upnl:+.2f}"
                print(
                    f"  {pos.symbol:<12} {pos.side:<5} {pos.entry_price:>10.4f} "
                    f"{cur:>10.4f} {pnl_str:>10}"
                )

        # Market prices
        if self._prices:
            print(f"\n  {'Symbol':<12} {'Price':>12}")
            print("  " + "-" * 26)
            for sym, price in sorted(self._prices.items()):
                print(f"  {sym:<12} {price:>12,.4f}")

        print("\n" + "=" * 60)
        print("  Press Ctrl+C to stop")
        print("=" * 60)
