"""
Alert system — Telegram bot integration + console fallback.
Telegram is optional; bot gracefully degrades if not configured.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False


class Alerter:
    """
    Sends trade/system alerts to Telegram.
    Falls back to logging only if Telegram is disabled or unavailable.
    """

    TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, config: dict) -> None:
        alert_cfg = config.get("alerts", {})
        self._enabled: bool = alert_cfg.get("telegram_enabled", False)
        self._token: str = os.environ.get(
            alert_cfg.get("telegram_token_env", "TELEGRAM_BOT_TOKEN"), ""
        )
        self._chat_id: str = os.environ.get(
            alert_cfg.get("telegram_chat_id_env", "TELEGRAM_CHAT_ID"), ""
        )
        self._notify_on: set[str] = set(alert_cfg.get("notify_on", []))

        if self._enabled and (not self._token or not self._chat_id):
            log.warning("Telegram alerts enabled but token/chat_id not set — disabling")
            self._enabled = False

    def notify(self, event: str, data: dict[str, Any]) -> None:
        if event not in self._notify_on:
            return
        message = self._format_message(event, data)
        log.info("[ALERT] %s: %s", event, message)
        if self._enabled:
            self._send_telegram(message)

    def _format_message(self, event: str, data: dict[str, Any]) -> str:
        symbol = data.get("symbol", "")
        if event == "trade_open":
            return (
                f"TRADE OPEN {symbol}\n"
                f"Side: {data.get('side', '')}\n"
                f"Price: {data.get('price', 0):.4f}\n"
                f"SL: {data.get('sl', 'N/A')}\n"
                f"TP: {data.get('tp', 'N/A')}"
            )
        elif event in ("trade_close", "stop_loss_hit"):
            emoji = "🔴" if event == "stop_loss_hit" else "🟢"
            pnl = float(data.get("pnl", 0))
            return (
                f"{emoji} TRADE CLOSED {symbol}\n"
                f"Reason: {data.get('reason', event)}\n"
                f"Price: {data.get('price', 0):.4f}\n"
                f"P&L: {pnl:+.4f} USDT"
            )
        elif event == "daily_loss_limit":
            return f"DAILY LOSS LIMIT HIT {symbol}\nTrading halted for today."
        elif event == "grid_rebalance":
            return (
                f"GRID REBALANCE {symbol}\n"
                f"Price {data.get('price', 0):.4f} exited "
                f"[{data.get('lower', 0):.2f} – {data.get('upper', 0):.2f}]"
            )
        elif event == "system_error":
            return f"SYSTEM ERROR\n{data.get('error', 'Unknown error')}"
        else:
            return f"{event.upper()}: {data}"

    def _send_telegram(self, message: str) -> None:
        if not _REQUESTS_AVAILABLE:
            log.warning("requests library not installed — Telegram alerts disabled")
            return
        url = self.TELEGRAM_API.format(token=self._token)
        try:
            resp = requests.post(
                url,
                json={"chat_id": self._chat_id, "text": message, "parse_mode": "HTML"},
                timeout=5,
            )
            if not resp.ok:
                log.warning("Telegram send failed: %s", resp.text)
        except Exception as exc:
            log.warning("Telegram send error: %s", exc)
