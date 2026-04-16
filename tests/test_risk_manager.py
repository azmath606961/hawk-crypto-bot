"""Unit tests for RiskManager gates."""
import pytest
from unittest.mock import MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.risk_manager import RiskManager

BASE_CONFIG = {
    "risk": {
        "risk_per_trade_pct": 1.0,
        "max_daily_loss_pct": 3.0,
        "max_drawdown_pct": 10.0,
        "max_open_trades": 3,
        "fee_rate": 0.001,
        "slippage_pct": 0.05,
    }
}


def make_rm(equity=1000.0):
    return RiskManager(BASE_CONFIG, equity)


def test_new_trade_allowed_initially():
    rm = make_rm()
    ok, reason = rm.check_new_trade()
    assert ok
    assert reason == "ok"


def test_max_open_trades_gate():
    rm = make_rm()
    rm.record_open()
    rm.record_open()
    rm.record_open()
    ok, reason = rm.check_new_trade()
    assert not ok
    assert "G3" in reason


def test_daily_loss_halt():
    rm = make_rm(equity=1000.0)
    # Simulate 3.5% daily loss
    rm.record_open()
    rm.record_close(-35.0)  # -3.5%
    ok, reason = rm.check_new_trade()
    assert not ok
    assert "G1" in reason


def test_drawdown_halt():
    rm = make_rm(equity=1000.0)
    # Simulate 11% drawdown
    rm._current_equity = 890.0
    ok, reason = rm.check_new_trade()
    assert not ok
    assert "G2" in reason


def test_position_sizing_1pct_rule():
    rm = make_rm(equity=1000.0)
    # Entry 100, SL 98 → 2% stop
    # raw size = risk(10) / sl_pct(0.02) = 500, but 20% cap = 200 → capped at 200
    size = rm.position_size_usdt(entry_price=100.0, stop_loss_price=98.0, equity=1000.0)
    assert size <= 200.0       # cap at 20% of equity
    assert size >= 10.0        # must be at least the risk amount


def test_position_sizing_capped_at_20pct():
    rm = make_rm(equity=1000.0)
    # Very tight stop → would give huge size, capped at 20%
    size = rm.position_size_usdt(entry_price=100.0, stop_loss_price=99.9, equity=1000.0)
    assert size <= 200.0  # 20% of 1000


def test_effective_entry_adds_cost_for_buy():
    rm = make_rm()
    effective = rm.effective_entry(100.0, "buy")
    assert effective > 100.0


def test_effective_entry_reduces_for_sell():
    rm = make_rm()
    effective = rm.effective_entry(100.0, "sell")
    assert effective < 100.0
