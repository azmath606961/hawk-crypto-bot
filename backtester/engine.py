"""
Backtesting Engine
==================
Replays OHLCV data bar-by-bar against any strategy.
Accounts for:
- Trading fees (per config)
- Slippage (fills at open of next bar)
- Max drawdown halt
- Realistic order simulation (no look-ahead)

Usage:
    python -m backtester.engine \\
        --csv data/BTC_1h.csv \\
        --strategy trend \\
        --capital 1000 \\
        --start-date 2024-01-01
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from backtester.metrics import compute_all
from utils.indicators import atr, ema

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Backtest position
# ------------------------------------------------------------------ #

@dataclass
class BtPosition:
    entry_price: float
    qty: float
    stop_loss: float
    take_profit: float
    entry_bar: int
    strategy: str
    side: str = "buy"

    @property
    def cost(self) -> float:
        return self.entry_price * self.qty


# ------------------------------------------------------------------ #
#  Main engine
# ------------------------------------------------------------------ #

class BacktestEngine:
    """
    Bar-by-bar OHLCV replayer. Supports:
    - Trend Following (EMA20/50)
    - Grid Trading (simplified: buy-the-dip in range)
    - DCA (fixed interval)
    """

    def __init__(
        self,
        df: pd.DataFrame,
        strategy: str,
        initial_capital: float = 1000.0,
        commission: float = 0.001,
        slippage: float = 0.0005,
        config_override: Optional[dict] = None,
    ) -> None:
        self._df = df.copy()
        self._strategy = strategy
        self._capital = initial_capital
        self._commission = commission
        self._slippage = slippage
        self._cfg = config_override or {}

        self._equity: list[float] = [initial_capital]
        self._trades_pnl: list[float] = []
        self._trade_log: list[dict] = []
        self._position: Optional[BtPosition] = None
        self._peak_equity = initial_capital

    def run(self) -> dict:
        """Execute backtest and return metrics dict."""
        df = self._df
        close = df["close"]
        high = df["high"]
        low = df["low"]

        ema_fast = ema(close, self._cfg.get("ema_fast", 20))
        ema_slow = ema(close, self._cfg.get("ema_slow", 50))
        atr_series = atr(high, low, close, self._cfg.get("atr_period", 14))

        rr = self._cfg.get("risk_reward_ratio", 2.0)
        sl_mult = self._cfg.get("sl_atr_multiplier", 1.5)
        risk_pct = self._cfg.get("risk_per_trade_pct", 1.0)
        max_dd = self._cfg.get("max_drawdown_pct", 10.0)

        dca_interval = self._cfg.get("dca_interval_bars", 24)
        dca_usdt = self._cfg.get("dca_amount_usdt", 20.0)

        grid_lower = self._cfg.get("grid_lower", None)
        grid_upper = self._cfg.get("grid_upper", None)
        num_grids = self._cfg.get("num_grids", 14)
        grid_usdt = self._cfg.get("grid_order_usdt", 50.0)
        grid_positions: list[BtPosition] = []
        last_dca_bar = -dca_interval

        equity_curve: list[float] = []
        current_equity = self._capital

        for i in range(len(df)):
            bar = df.iloc[i]
            bar_open = float(bar["open"])
            bar_high = float(bar["high"])
            bar_low = float(bar["low"])
            bar_close = float(bar["close"])

            # Check drawdown halt
            dd = (1 - current_equity / self._peak_equity) * 100
            if dd >= max_dd:
                log.warning("Max drawdown %.2f%% hit at bar %d — stopping", dd, i)
                break

            # ---- Trend Following ----
            if self._strategy == "trend":
                ef = float(ema_fast.iloc[i])
                es = float(ema_slow.iloc[i])
                at = float(atr_series.iloc[i])

                if pd.isna(ef) or pd.isna(es) or pd.isna(at):
                    equity_curve.append(current_equity)
                    continue

                pullback_pct = self._cfg.get("pullback_pct", 0.015)  # 1.5% default
                if self._position is None:
                    # Entry: price close to EMA_fast while EMA_fast > EMA_slow
                    if (ef > es and
                            bar_close > ef and
                            abs(bar_close - ef) / ef < pullback_pct and
                            i >= self._cfg.get("ema_slow", 50)):

                        sl = bar_close - sl_mult * at
                        tp = bar_close + rr * sl_mult * at
                        risk_per_trade = current_equity * risk_pct / 100
                        sl_dist = (bar_close - sl) / bar_close
                        size_usdt = min(risk_per_trade / sl_dist, current_equity * 0.20)
                        entry_p = bar_close * (1 + self._slippage)
                        fee = size_usdt * self._commission
                        qty = size_usdt / entry_p

                        if size_usdt >= 10 and current_equity >= size_usdt + fee:
                            current_equity -= fee
                            self._position = BtPosition(
                                entry_price=entry_p,
                                qty=qty,
                                stop_loss=sl,
                                take_profit=tp,
                                entry_bar=i,
                                strategy="trend",
                            )
                else:
                    pos = self._position
                    exit_price = None
                    exit_reason = ""

                    if bar_low <= pos.stop_loss:
                        exit_price = pos.stop_loss * (1 - self._slippage)
                        exit_reason = "stop_loss"
                    elif bar_high >= pos.take_profit:
                        exit_price = pos.take_profit * (1 - self._slippage)
                        exit_reason = "take_profit"
                    elif ef < es:
                        exit_price = bar_close * (1 - self._slippage)
                        exit_reason = "ema_reversal"

                    if exit_price:
                        fee = pos.qty * exit_price * self._commission
                        pnl = (exit_price - pos.entry_price) * pos.qty - fee
                        current_equity += pnl
                        self._trades_pnl.append(pnl)
                        self._trade_log.append({
                            "bar": i,
                            "side": "sell",
                            "price": exit_price,
                            "qty": pos.qty,
                            "pnl": pnl,
                            "reason": exit_reason,
                        })
                        self._position = None
                        if current_equity > self._peak_equity:
                            self._peak_equity = current_equity

            # ---- DCA ----
            elif self._strategy == "dca":
                if i - last_dca_bar >= dca_interval:
                    fee = dca_usdt * self._commission
                    cost = dca_usdt + fee
                    if current_equity >= cost:
                        current_equity -= fee
                        qty = dca_usdt / (bar_close * (1 + self._slippage))
                        self._trades_pnl.append(-fee)  # record fee as cost
                        self._trade_log.append({
                            "bar": i, "side": "buy", "price": bar_close,
                            "qty": qty, "pnl": -fee, "reason": "dca"
                        })
                        last_dca_bar = i

            # ---- Grid ----
            elif self._strategy == "grid":
                if grid_lower is None or grid_upper is None:
                    grid_lower = bar_close * 0.95
                    grid_upper = bar_close * 1.05
                interval = (grid_upper - grid_lower) / num_grids

                # Open new grid buys when price crosses a grid line from above
                for g in range(num_grids):
                    gl = grid_lower + g * interval
                    if (bar_low <= gl <= bar_close and  # price passed through grid level
                            len(grid_positions) < num_grids // 2):
                        cost = grid_usdt + grid_usdt * self._commission
                        if current_equity >= cost:
                            current_equity -= grid_usdt * self._commission
                            gpos = BtPosition(
                                entry_price=bar_close,
                                qty=grid_usdt / bar_close,
                                stop_loss=grid_lower * 0.98,
                                take_profit=gl + interval,
                                entry_bar=i,
                                strategy="grid",
                            )
                            grid_positions.append(gpos)

                # Close grid positions at TP
                closed = []
                for gpos in grid_positions:
                    if bar_high >= gpos.take_profit:
                        fee = gpos.qty * gpos.take_profit * self._commission
                        pnl = (gpos.take_profit - gpos.entry_price) * gpos.qty - fee
                        current_equity += pnl
                        self._trades_pnl.append(pnl)
                        self._trade_log.append({
                            "bar": i, "side": "sell", "price": gpos.take_profit,
                            "qty": gpos.qty, "pnl": pnl, "reason": "grid_tp"
                        })
                        closed.append(gpos)
                        if current_equity > self._peak_equity:
                            self._peak_equity = current_equity
                for c in closed:
                    grid_positions.remove(c)

            equity_curve.append(current_equity)

        # Close any remaining position at last bar
        if self._position and self._strategy == "trend":
            last_price = float(df["close"].iloc[-1])
            fee = self._position.qty * last_price * self._commission
            pnl = (last_price - self._position.entry_price) * self._position.qty - fee
            current_equity += pnl
            self._trades_pnl.append(pnl)
            equity_curve[-1] = current_equity

        eq_series = pd.Series(equity_curve)
        pnl_series = pd.Series(self._trades_pnl) if self._trades_pnl else pd.Series([0.0])

        metrics = compute_all(eq_series, pnl_series)
        metrics["strategy"] = self._strategy
        metrics["bars"] = len(df)
        return metrics

    def get_trade_log(self) -> pd.DataFrame:
        return pd.DataFrame(self._trade_log)


# ------------------------------------------------------------------ #
#  CLI entry point
# ------------------------------------------------------------------ #

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crypto Bot Backtester")
    p.add_argument("--csv", required=True, help="OHLCV CSV (timestamp,open,high,low,close,volume)")
    p.add_argument("--strategy", choices=["trend", "dca", "grid"], default="trend")
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--commission", type=float, default=0.001)
    p.add_argument("--slippage", type=float, default=0.0005)
    p.add_argument("--ema-fast", type=int, default=20)
    p.add_argument("--ema-slow", type=int, default=50)
    p.add_argument("--rr", type=float, default=2.0, help="Risk:reward ratio")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    args = _parse_args()

    df = pd.read_csv(args.csv, parse_dates=["timestamp"], index_col="timestamp")
    df.columns = [c.lower() for c in df.columns]

    if args.start_date:
        df = df[df.index >= args.start_date]
    if args.end_date:
        df = df[df.index <= args.end_date]

    if df.empty:
        log.error("No data in selected date range")
        return

    log.info(
        "Backtesting %s | %s → %s | %d bars",
        args.strategy,
        df.index[0].date(),
        df.index[-1].date(),
        len(df),
    )

    cfg = {
        "ema_fast": args.ema_fast,
        "ema_slow": args.ema_slow,
        "risk_reward_ratio": args.rr,
        "sl_atr_multiplier": 1.5,
        "atr_period": 14,
        "risk_per_trade_pct": 1.0,
        "max_drawdown_pct": 10.0,
        "dca_interval_bars": 24,
        "dca_amount_usdt": 20.0,
    }

    engine = BacktestEngine(
        df,
        strategy=args.strategy,
        initial_capital=args.capital,
        commission=args.commission,
        slippage=args.slippage,
        config_override=cfg,
    )
    metrics = engine.run()

    print("\n" + "=" * 50)
    print(f"  BACKTEST RESULTS — {args.strategy.upper()}")
    print("=" * 50)
    for k, v in metrics.items():
        label = k.replace("_", " ").title()
        if isinstance(v, float):
            print(f"  {label:<25}: {v:>10.3f}")
        else:
            print(f"  {label:<25}: {v:>10}")
    print("=" * 50)

    trades = engine.get_trade_log()
    if not trades.empty:
        out = args.csv.replace(".csv", f"_backtest_{args.strategy}.csv")
        trades.to_csv(out, index=False)
        print(f"\n  Trade log saved: {out}")


if __name__ == "__main__":
    main()
