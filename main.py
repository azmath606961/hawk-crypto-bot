"""
Crypto Trading Bot — Main Entry Point
======================================
Usage:
    python main.py                         # use config.yaml defaults
    python main.py --config my_config.yaml
    python main.py --paper                 # force paper mode
    python main.py --symbol BTC/USDT --strategy grid

The bot runs a continuous event loop, fetching candles on each strategy's
timeframe and calling on_tick() for every active strategy.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import yaml

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from core.exchange import ExchangeClient
from core.order_executor import OrderExecutor
from core.portfolio import Portfolio
from core.risk_manager import RiskManager
from monitor.alerts import Alerter
from monitor.dashboard import Dashboard
from monitor.logger import TradeLogger
from paper_trading import PaperExchange
from strategies.dca_strategy import DCAStrategy
from strategies.grid_trading import GridTradingStrategy
from strategies.trend_following import TrendFollowingStrategy

log = logging.getLogger(__name__)

TICK_INTERVAL_S = 60    # poll interval between candle fetches


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def build_exchange(config: dict) -> ExchangeClient | PaperExchange:
    real_ex = ExchangeClient(config)
    if config["trading"].get("paper_mode", True):
        log.info("Paper mode ON — wrapping exchange with PaperExchange simulator")
        return PaperExchange(real_ex, config)
    return real_ex


def build_strategies(
    config: dict,
    exchange,
    executor: OrderExecutor,
    portfolio: Portfolio,
    risk: RiskManager,
    logger: TradeLogger,
    alerter: Alerter,
) -> dict[str, list]:
    """Returns {symbol: [strategy, ...]}"""
    strategy_map: dict[str, list] = {}
    active = config["trading"]["active_strategies"]

    for symbol, names in active.items():
        strategies = []
        for name in names:
            if name == "grid" and symbol in config.get("grid_trading", {}):
                strategies.append(
                    GridTradingStrategy(
                        symbol, config, exchange, executor, risk, logger, alerter
                    )
                )
            elif name == "trend" and symbol in config.get("trend_following", {}):
                strategies.append(
                    TrendFollowingStrategy(
                        symbol, config, exchange, executor, portfolio, risk, logger, alerter
                    )
                )
            elif name == "dca" and symbol in config.get("dca", {}):
                strategies.append(
                    DCAStrategy(
                        symbol, config, exchange, executor, risk, logger, alerter
                    )
                )
            else:
                log.warning("Strategy '%s' for %s skipped (not configured)", name, symbol)
        if strategies:
            strategy_map[symbol] = strategies

    return strategy_map


def run_event_loop(
    config: dict,
    exchange,
    strategy_map: dict[str, list],
    dashboard: Dashboard,
    risk: RiskManager,
    portfolio: Portfolio,
) -> None:
    timeframes: dict[str, str] = {}
    for symbol, strategies in strategy_map.items():
        for strat in strategies:
            if hasattr(strat, "_timeframe"):
                timeframes[symbol] = strat._timeframe
                break
        if symbol not in timeframes:
            timeframes[symbol] = "1h"

    log.info("Event loop started. Symbols: %s", list(strategy_map.keys()))

    while True:
        try:
            if risk.is_halted:
                log.warning("Bot halted. Sleeping 60s before recheck.")
                time.sleep(60)
                continue

            prices: dict[str, float] = {}
            for symbol, strategies in strategy_map.items():
                try:
                    tf = timeframes.get(symbol, "1h")
                    df = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=200)
                    if df.empty:
                        log.warning("No OHLCV data for %s", symbol)
                        continue

                    price = float(df["close"].iloc[-1])
                    prices[symbol] = price

                    # Update equity from balance
                    try:
                        bal = exchange.fetch_balance()
                        equity_usdt = bal.get("USDT", risk.current_equity)
                        risk.update_equity(equity_usdt)
                        portfolio.snapshot_equity(equity_usdt)
                    except Exception:
                        pass

                    # Dispatch tick to all strategies for this symbol
                    for strat in strategies:
                        if strat.enabled:
                            try:
                                strat.on_tick(price, df)
                            except Exception as exc:
                                log.error(
                                    "Strategy %s on_tick error for %s: %s",
                                    strat.name,
                                    symbol,
                                    exc,
                                )

                except Exception as exc:
                    log.error("Error processing %s: %s", symbol, exc)

            dashboard.update_prices(prices)
            dashboard.render()

        except KeyboardInterrupt:
            log.info("Bot stopped by user")
            break
        except Exception as exc:
            log.error("Event loop error: %s", exc)

        time.sleep(TICK_INTERVAL_S)


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Trading Bot")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "config", "config.yaml"),
        help="Path to config.yaml",
    )
    parser.add_argument("--paper", action="store_true", help="Force paper trading mode")
    parser.add_argument("--symbol", help="Override: trade only this symbol")
    parser.add_argument("--strategy", help="Override: use only this strategy")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.paper:
        config["trading"]["paper_mode"] = True

    if args.symbol:
        config["trading"]["active_strategies"] = {
            args.symbol: config["trading"]["active_strategies"].get(args.symbol, ["trend"])
        }

    if args.strategy:
        for sym in list(config["trading"]["active_strategies"].keys()):
            config["trading"]["active_strategies"][sym] = [args.strategy]

    # Bootstrap
    logger = TradeLogger(config)
    alerter = Alerter(config)
    exchange = build_exchange(config)

    initial_equity = config["risk"]["capital_usdt"]
    try:
        bal = exchange.fetch_balance()
        initial_equity = bal.get("USDT", initial_equity)
    except Exception:
        pass

    risk = RiskManager(config, initial_equity)
    portfolio = Portfolio(config, initial_equity)
    executor = OrderExecutor(exchange, config)

    strategy_map = build_strategies(
        config, exchange, executor, portfolio, risk, logger, alerter
    )

    symbols = list(strategy_map.keys())
    dashboard = Dashboard(portfolio, risk, symbols)

    mode = "PAPER" if config["trading"].get("paper_mode") else "LIVE"
    log.info("=" * 50)
    log.info("  Crypto Bot starting in %s mode", mode)
    log.info("  Symbols  : %s", symbols)
    log.info("  Capital  : %.2f USDT", initial_equity)
    log.info("=" * 50)

    run_event_loop(config, exchange, strategy_map, dashboard, risk, portfolio)


if __name__ == "__main__":
    main()
