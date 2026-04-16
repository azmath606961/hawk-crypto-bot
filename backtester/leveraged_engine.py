"""
Leveraged Compounding Backtest Engine  v2
==========================================
Simulates Binance Futures (perpetual) trading with:

  - Configurable leverage (1x–20x) — AMPLIFIES both gains and losses
  - Full compounding: position sizes grow with equity after every profit
  - Funding rate cost: 0.01% per 8h (realistic Binance perp rate)
  - Liquidation check every bar
  - Futures fee: 0.04% taker (not 0.1% spot)
  - Multi-timeframe: 1h entries, 4h trend bias
  - ADX trend-strength filter (skip choppy markets)

How leverage affects returns here
-----------------------------------
  risk_usdt    = equity  * risk_pct / 100
  position_qty = risk_usdt / (sl_dist_pct * entry_price)   ← 1× size
  levered_qty  = position_qty * leverage                    ← L× size

  A favourable move of sl_dist earns  leverage × risk_usdt   (big win)
  An adverse  move of sl_dist loses   risk_usdt               (capped loss)
  Liquidation if price moves  > (1/leverage) from entry       (catastrophic)

  → leverage amplifies wins without proportionally amplifying per-trade loss
    (stop-loss catches it first), BUT liquidation is the tail-risk.

GBP conversion: 1 GBP = 1.27 USDT (update GBP_TO_USDT as needed)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from backtester.metrics import compute_all
from utils.indicators import atr, ema, rsi

log = logging.getLogger(__name__)

TAKER_FEE               = 0.0004    # 0.04% Binance Futures taker
FUNDING_RATE_8H         = 0.0001    # 0.01% per 8h (longs pay, shorts receive)
MAINTENANCE_MARGIN_RATE = 0.005     # 0.5% of notional — liquidation floor
GBP_TO_USDT             = 1.27


# ──────────────────────────────────────────────────────────────────────────── #

def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength 0-100."""
    prev_h = high.shift(1)
    prev_l = low.shift(1)
    prev_c = close.shift(1)

    tr  = pd.concat([high - low,
                     (high - prev_c).abs(),
                     (low  - prev_c).abs()], axis=1).max(axis=1)
    dm_plus  = ((high - prev_h).clip(lower=0)
                .where((high - prev_h) > (prev_l - low), 0))
    dm_minus = ((prev_l - low).clip(lower=0)
                .where((prev_l - low) > (high - prev_h), 0))

    atr_s   = tr.ewm(span=period, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm(span=period,  adjust=False).mean() / atr_s.replace(0, np.nan)
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx       = (100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan))
    return dx.ewm(span=period, adjust=False).mean()


def resample_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1h OHLCV to 4h."""
    return df.resample("4h").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()


# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class LevTrade:
    side:         str
    entry:        float
    qty:          float        # base asset quantity (levered)
    leverage:     int
    stop_loss:    float        # initial hard stop
    take_profit:  float
    trail_stop:   float        # dynamic trailing stop
    entry_bar:    int
    strategy:     str
    margin_used:  float        # USDT margin locked
    liq_price:    float
    bars_held:    int = 0

    @property
    def notional(self) -> float:
        return self.entry * self.qty


@dataclass
class BacktestResult:
    metrics:       dict
    equity_curve:  pd.Series
    trade_log:     pd.DataFrame
    peak_equity:   float
    lowest_equity: float
    liquidations:  int
    funding_paid:  float


# ──────────────────────────────────────────────────────────────────────────── #

class LeveragedEngine:
    """
    Bar-by-bar leveraged futures backtest with full compounding.
    Runs on 1h bars with 4h trend bias.
    """

    def __init__(
        self,
        df: pd.DataFrame,                    # 1h OHLCV
        strategy:               str   = "breakout",
        initial_capital_usdt:   float = 635.0,
        leverage:               int   = 3,
        risk_per_trade_pct:     float = 1.5,
        max_drawdown_pct:       float = 25.0,
        allow_shorts:           bool  = True,
        cfg:            Optional[dict] = None,
    ) -> None:
        self._df1h    = df.copy()
        self._df4h    = resample_4h(df)
        self._strat   = strategy
        self._equity  = initial_capital_usdt
        self._initial = initial_capital_usdt
        self._lev     = leverage
        self._risk    = risk_per_trade_pct
        self._max_dd  = max_drawdown_pct
        self._shorts  = allow_shorts
        self._cfg     = cfg or {}

        self._peak    = initial_capital_usdt
        self._pos:    Optional[LevTrade] = None
        self._trades: list[dict]         = []
        self._eq_log: list[float]        = []
        self._liqs    = 0
        self._funding = 0.0

    # ──────────────────────────────────────────────────────────────────── #
    #  Run                                                                 #
    # ──────────────────────────────────────────────────────────────────── #

    def run(self) -> BacktestResult:
        df1h = self._df1h
        df4h = self._df4h

        fast  = self._cfg.get("ema_fast",       20)
        slow  = self._cfg.get("ema_slow",       50)
        atrp  = self._cfg.get("atr_period",     14)
        rsip  = self._cfg.get("rsi_period",     14)
        brkp  = self._cfg.get("breakout_period",20)
        adxp  = self._cfg.get("adx_period",     14)
        adx_min = self._cfg.get("adx_min",      20)
        trail = self._cfg.get("trail_atr_mult", 2.0)
        sl_m  = self._cfg.get("sl_atr_mult",    1.5)
        tp_m  = self._cfg.get("tp_atr_mult",    3.5)   # ~2.3 RR
        vmult = self._cfg.get("volume_mult",    1.5)

        # 1h indicators
        c1 = df1h["close"]; h1 = df1h["high"]; l1 = df1h["low"]; v1 = df1h["volume"]
        ema_f1  = ema(c1, fast)
        ema_s1  = ema(c1, slow)
        atr1    = atr(h1, l1, c1, atrp)
        rsi1    = rsi(c1, rsip)
        rh1     = c1.rolling(brkp).max().shift(1)
        rl1     = c1.rolling(brkp).min().shift(1)
        va1     = v1.rolling(20).mean()

        # 4h indicators — for trend bias
        c4 = df4h["close"]; h4 = df4h["high"]; l4 = df4h["low"]
        ema_f4 = ema(c4, fast)
        ema_s4 = ema(c4, slow)
        atr4   = atr(h4, l4, c4, atrp)
        adx4   = _adx(h4, l4, c4, adxp)
        rsi4   = rsi(c4, rsip)

        warmup = max(slow, brkp) + 5

        for i in range(warmup, len(df1h)):
            ts   = df1h.index[i]
            bc   = float(c1.iloc[i])
            bh   = float(h1.iloc[i])
            bl   = float(l1.iloc[i])
            bv   = float(v1.iloc[i])
            batr = float(atr1.iloc[i])
            brsi = float(rsi1.iloc[i])
            bef  = float(ema_f1.iloc[i])
            bes  = float(ema_s1.iloc[i])
            brhh = float(rh1.iloc[i])
            brll = float(rl1.iloc[i])
            bva  = float(va1.iloc[i])

            if any(pd.isna(x) for x in [batr, brsi, bef, bes, brhh, brll]):
                self._eq_log.append(self._equity)
                continue

            # Get 4h context for this 1h bar (last 4h bar before this timestamp)
            mask4 = df4h.index <= ts
            if not mask4.any():
                self._eq_log.append(self._equity)
                continue
            j4     = int(np.searchsorted(df4h.index, ts, side='right')) - 1
            if j4 < max(slow, adxp):
                self._eq_log.append(self._equity)
                continue

            h4_ef  = float(ema_f4.iloc[j4])
            h4_es  = float(ema_s4.iloc[j4])
            h4_adx = float(adx4.iloc[j4])
            h4_rsi = float(rsi4.iloc[j4])
            h4_atr = float(atr4.iloc[j4])

            if any(pd.isna(x) for x in [h4_ef, h4_es, h4_adx]):
                self._eq_log.append(self._equity)
                continue

            # Drawdown halt
            if self._equity <= 0:
                break
            dd = (1 - self._equity / self._peak) * 100
            if dd >= self._max_dd:
                log.warning("Drawdown %.1f%% at bar %d — halting", dd, i)
                break

            # Funding (every 8 bars = 8h)
            if self._pos and i % 8 == 0:
                fund = self._pos.notional * FUNDING_RATE_8H
                self._equity   -= fund if self._pos.side == "long" else -fund
                self._funding  += fund if self._pos.side == "long" else 0

            # Manage open position
            if self._pos:
                self._pos.bars_held += 1
                p = self._pos

                # Breakeven + trailing stop logic:
                # Phase 1: price hasn't reached 1×SL in profit → use fixed SL
                # Phase 2: price reaches 1×SL profit → move stop to breakeven
                # Phase 3: price reaches 2×SL profit → trail at 2.5×ATR (lock gains)
                sl_dist = getattr(p, '_sl_dist', batr * 1.5)
                be_moved = getattr(p, '_be_moved', False)

                if p.side == "long":
                    profit_dist = bc - p.entry
                    if profit_dist >= 2.0 * sl_dist:
                        # Phase 3: trailing
                        new_ts = bc - trail * batr
                        p.trail_stop = max(p.trail_stop, new_ts)
                    elif profit_dist >= 1.0 * sl_dist and not be_moved:
                        # Phase 2: move to breakeven
                        p.trail_stop = max(p.trail_stop, p.entry + 0.001 * p.entry)
                        p._be_moved = True
                    # Phase 1: trail_stop stays at original SL
                else:
                    profit_dist = p.entry - bc
                    if profit_dist >= 2.0 * sl_dist:
                        new_ts = bc + trail * batr
                        p.trail_stop = min(p.trail_stop, new_ts)
                    elif profit_dist >= 1.0 * sl_dist and not be_moved:
                        p.trail_stop = min(p.trail_stop, p.entry - 0.001 * p.entry)
                        p._be_moved = True

                # Liquidation
                if self._check_liq(p, bl, bh):
                    self._liquidate(p, bc, i)
                    self._eq_log.append(self._equity)
                    continue

                # Exit
                ep, reason = self._check_exit(p, bc, bh, bl)
                if ep:
                    self._close(p, ep, reason, i)
                self._eq_log.append(self._equity)
                continue

            # Entry conditions
            if self._equity < 5:
                self._eq_log.append(self._equity)
                continue

            trend_up   = (h4_ef > h4_es) and (h4_adx > adx_min)
            trend_down = (h4_ef < h4_es) and (h4_adx > adx_min)

            signal, ep = None, bc

            # Strict 4h direction gate — only trade WITH the macro trend
            # Avoids shorts in bull runs and longs in bear crashes
            can_long  = trend_up   and h4_rsi < 75   # not overextended
            can_short = trend_down and self._shorts and h4_rsi > 25

            # ── Breakout ───────────────────────────────────────────────
            if self._strat in ("breakout", "combined"):
                vol_ok = bv > vmult * bva
                if (can_long  and bc > brhh and vol_ok and 48 < brsi < 73):
                    signal = "long";  ep = bc * 1.0005
                elif (can_short and bc < brll and vol_ok and 27 < brsi < 52):
                    signal = "short"; ep = bc * 0.9995

            # ── EMA Trend pullback ─────────────────────────────────────
            if signal is None and self._strat in ("ema_trend", "combined"):
                near_fast = abs(bc - bef) / bef < 0.025
                if (can_long  and bef > bes and near_fast and bc > bef and 42 < brsi < 63):
                    signal = "long";  ep = bc * 1.0005
                elif (can_short and bef < bes and near_fast and bc < bef and 37 < brsi < 58):
                    signal = "short"; ep = bc * 0.9995

            if signal:
                self._open(signal, ep, batr, h4_atr, sl_m, tp_m, i)

            self._eq_log.append(self._equity)

        if self._pos:
            lp = float(self._df1h["close"].iloc[-1])
            self._close(self._pos, lp, "end_of_data", len(self._df1h)-1)

        return self._build_result()

    # ──────────────────────────────────────────────────────────────────── #
    #  Position helpers                                                    #
    # ──────────────────────────────────────────────────────────────────── #

    def _open(
        self,
        side:    str,
        entry:   float,
        atr1h:   float,
        atr4h:   float,
        sl_m:    float,
        tp_m:    float,
        bar_idx: int,
    ) -> None:
        # Use blended ATR: 70% 4h (smoother), 30% 1h (reactive)
        combined_atr = atr4h * 0.25 * 0.7 + atr1h * 0.3
        combined_atr = max(combined_atr, atr1h)  # at least 1h ATR
        sl_dist = sl_m * combined_atr

        if side == "long":
            sl  = entry - sl_dist
            tp  = entry + tp_m * combined_atr
            liq = entry * (1 - (1 / self._lev) + MAINTENANCE_MARGIN_RATE)
        else:
            sl  = entry + sl_dist
            tp  = entry - tp_m * combined_atr
            liq = entry * (1 + (1 / self._lev) - MAINTENANCE_MARGIN_RATE)

        sl_pct = sl_dist / entry
        if sl_pct < 0.003:
            return

        # ── Leveraged position sizing ─────────────────────────────────
        # risk_usdt is fixed (how much equity we're willing to lose if SL hits)
        # But with leverage L, we control L× the notional:
        #   qty_base  = risk_usdt / sl_dist           (unlevered qty for this risk)
        #   qty_lev   = qty_base * leverage             (leverage multiplies qty)
        #   margin    = qty_lev * entry / leverage      = qty_base * entry
        #   max_loss  = qty_lev * sl_dist               = risk_usdt * leverage
        #
        # In practice we cap: margin ≤ equity × 0.5 (never risk >50% as margin)
        risk_usdt  = self._equity * self._risk / 100
        qty_base   = risk_usdt / sl_dist
        qty_lev    = qty_base * self._lev           # ← leverage amplifies qty
        margin_req = qty_lev * entry / self._lev    # = qty_base * entry
        fee        = qty_lev * entry * TAKER_FEE

        # Safety cap: margin + fee ≤ 50% of equity
        max_margin = self._equity * 0.50
        if margin_req + fee > max_margin:
            scale      = max_margin / (margin_req + fee)
            qty_lev   *= scale
            margin_req = qty_lev * entry / self._lev
            fee        = qty_lev * entry * TAKER_FEE

        if qty_lev * entry < 2.0:   # min $2 notional
            return

        self._equity -= fee
        self._pos = LevTrade(
            side=side, entry=entry, qty=qty_lev, leverage=self._lev,
            stop_loss=sl, take_profit=tp,
            trail_stop=sl,          # trail starts at SL, moves to BE then trails
            entry_bar=bar_idx, strategy=self._strat,
            margin_used=margin_req, liq_price=liq,
        )
        # Store SL distance for breakeven logic
        self._pos._sl_dist   = sl_dist
        self._pos._be_moved  = False    # has stop been moved to break-even yet?

    def _close(self, pos: LevTrade, ep: float, reason: str, bar_idx: int) -> None:
        fee = pos.qty * ep * TAKER_FEE
        pnl = ((ep - pos.entry) if pos.side == "long"
               else (pos.entry - ep)) * pos.qty - fee
        self._equity += pnl
        if self._equity > self._peak:
            self._peak = self._equity
        self._trades.append({
            "bar": bar_idx, "side": pos.side, "strategy": pos.strategy,
            "entry": pos.entry, "exit": ep, "qty": pos.qty,
            "leverage": pos.leverage, "pnl_usdt": pnl,
            "pnl_pct": pnl / max(pos.margin_used, 0.01) * 100,
            "reason": reason, "bars_held": pos.bars_held,
            "equity_after": self._equity,
        })
        self._pos = None

    def _liquidate(self, pos: LevTrade, bc: float, bar_idx: int) -> None:
        log.warning("LIQUIDATION bar=%d %s entry=%.2f liq=%.2f cur=%.2f",
                    bar_idx, pos.side, pos.entry, pos.liq_price, bc)
        loss = -pos.margin_used
        self._equity = max(self._equity + loss, 0)
        self._liqs  += 1
        self._trades.append({
            "bar": bar_idx, "side": pos.side, "strategy": pos.strategy,
            "entry": pos.entry, "exit": pos.liq_price, "qty": pos.qty,
            "leverage": pos.leverage, "pnl_usdt": loss,
            "pnl_pct": -100.0, "reason": "liquidation",
            "bars_held": pos.bars_held, "equity_after": self._equity,
        })
        self._pos = None

    def _check_liq(self, pos: LevTrade, bl: float, bh: float) -> bool:
        return ((pos.side == "long"  and bl <= pos.liq_price) or
                (pos.side == "short" and bh >= pos.liq_price))

    def _check_exit(self, pos, bc, bh, bl) -> tuple[Optional[float], str]:
        if pos.side == "long":
            if bl <= pos.trail_stop:  return pos.trail_stop * 0.9995, "trail_stop"
            if bh >= pos.take_profit: return pos.take_profit * 0.9995, "take_profit"
        else:
            if bh >= pos.trail_stop:  return pos.trail_stop * 1.0005, "trail_stop"
            if bl <= pos.take_profit: return pos.take_profit * 1.0005, "take_profit"
        return None, ""

    # ──────────────────────────────────────────────────────────────────── #

    def _build_result(self) -> BacktestResult:
        eq = pd.Series(self._eq_log if self._eq_log else [self._initial])
        tdf = pd.DataFrame(self._trades)
        pnl = tdf["pnl_usdt"] if not tdf.empty else pd.Series([0.0])
        m = compute_all(eq, pnl, periods_per_year=8760)
        m.update({
            "leverage":          self._lev,
            "strategy":          self._strat,
            "funding_paid_usdt": round(self._funding, 4),
            "liquidations":      self._liqs,
            "initial_usdt":      round(self._initial, 2),
            "initial_gbp":       round(self._initial / GBP_TO_USDT, 2),
            "final_usdt":        round(self._equity, 2),
            "final_gbp":         round(self._equity / GBP_TO_USDT, 2),
        })
        return BacktestResult(
            metrics=m, equity_curve=eq, trade_log=tdf,
            peak_equity=self._peak,
            lowest_equity=float(min(self._eq_log)) if self._eq_log else self._initial,
            liquidations=self._liqs, funding_paid=self._funding,
        )
