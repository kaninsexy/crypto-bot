"""
notifications/telegram.py — Telegram bot notifications for the trading bot.

WHY TELEGRAM?
─────────────
When the bot runs on a remote server (Digital Ocean), you can't watch logs.
Telegram gives you real-time alerts on your phone for:
  - Trade opens/closes with P&L
  - Circuit breaker trips
  - Daily P&L summaries
  - Error alerts (exceptions, crashes)
  - Heartbeat (bot is alive)

SETUP (5 minutes)
──────────────────
1. Message @BotFather on Telegram → /newbot → get your BOT_TOKEN
2. Start a chat with your bot, then visit:
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   Find "chat" → "id" — that's your CHAT_ID
3. Add to your .env:
   TELEGRAM_BOT_TOKEN=123456789:ABCdef...
   TELEGRAM_CHAT_ID=987654321

USAGE
─────
  from notifications.telegram import TelegramNotifier

  notifier = TelegramNotifier()   # reads from config automatically

  # Trade alert
  notifier.trade_opened("DCA", "SOL/USDT", price=175.40, size_usdt=200)
  notifier.trade_closed("DCA", "SOL/USDT", entry=175.40, exit=183.20, pnl=+8.90, pnl_pct=+4.47)

  # System alerts
  notifier.circuit_breaker_tripped(drawdown_pct=31.2, equity=6880)
  notifier.error_alert("MeanReversion", "IndexError: list index out of range")

  # Heartbeat (call every ~30 min from main loop)
  notifier.heartbeat(equity=10_241, open_positions=2, candle_count=842)

  # Daily summary
  notifier.daily_summary(date="2024-12-01", pnl=+142.30, trades=7, win_rate=71.4)

SILENT MODE
───────────
  If BOT_TOKEN / CHAT_ID are not configured, ALL methods silently no-op.
  The bot never crashes because notifications failed.
"""

from __future__ import annotations

import time
import json
import threading
from datetime import datetime, timezone
from typing import Optional

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

try:
    import config as _config
    _BOT_TOKEN = _config.TELEGRAM_BOT_TOKEN
    _CHAT_ID   = _config.TELEGRAM_CHAT_ID
except Exception:
    _BOT_TOKEN = ""
    _CHAT_ID   = ""


# ── Emoji constants ────────────────────────────────────────────────────────────
_BUY   = "🟢"
_SELL  = "🔴"
_WARN  = "⚠️"
_ERR   = "🚨"
_OK    = "✅"
_PULSE = "💓"
_CHART = "📊"
_MONEY = "💰"
_CLOCK = "🕐"


class TelegramNotifier:
    """
    Sends Telegram messages for all significant bot events.

    All send methods are fire-and-forget (run in a background thread) so they
    never block the main trading loop. Failures are silently swallowed.

    Args:
        bot_token:  Telegram Bot API token. Defaults to config.TELEGRAM_BOT_TOKEN.
        chat_id:    Telegram chat / channel ID. Defaults to config.TELEGRAM_CHAT_ID.
        enabled:    Explicitly enable/disable. Auto-detects from token/chat_id.
        timeout_s:  HTTP request timeout (seconds). Default 10.
    """

    def __init__(
        self,
        bot_token: str = "",
        chat_id:   str = "",
        enabled:   Optional[bool] = None,
        timeout_s: float = 10.0,
    ):
        self.token    = bot_token or _BOT_TOKEN
        self.chat_id  = chat_id  or _CHAT_ID
        self.timeout  = timeout_s
        self._last_heartbeat: Optional[datetime] = None

        # Auto-enable if both token and chat_id are set
        if enabled is None:
            self.enabled = bool(self.token and self.chat_id)
        else:
            self.enabled = enabled

        if self.enabled:
            print(f"[Telegram] Notifications ENABLED (chat_id={self.chat_id})")
        else:
            print("[Telegram] Notifications DISABLED "
                  "(set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env to enable)")

    # ── Trade events ─────────────────────────────────────────────────────────

    def trade_opened(
        self,
        strategy:   str,
        symbol:     str,
        price:      float,
        size_usdt:  float,
        sl_pct:     float = 0.0,
        tp_pct:     float = 0.0,
    ) -> None:
        lines = [
            f"{_BUY} *TRADE OPENED*",
            f"Strategy : `{strategy}`",
            f"Symbol   : `{symbol}`",
            f"Price    : `{price:,.4f}`",
            f"Size     : `${size_usdt:,.2f}`",
        ]
        if sl_pct:
            lines.append(f"Stop-Loss: `{sl_pct:.1f}%`")
        if tp_pct:
            lines.append(f"Take-Prof: `{tp_pct:.1f}%`")
        lines.append(f"_At {_now()}_")
        self._send("\n".join(lines))

    def trade_closed(
        self,
        strategy:  str,
        symbol:    str,
        entry:     float,
        exit_price:float,
        pnl:       float,
        pnl_pct:   float,
        reason:    str = "signal",
        hold_hours:float = 0.0,
    ) -> None:
        emoji = _BUY if pnl >= 0 else _SELL
        sign  = "+" if pnl >= 0 else ""
        lines = [
            f"{emoji} *TRADE CLOSED*",
            f"Strategy : `{strategy}`",
            f"Symbol   : `{symbol}`",
            f"Entry    : `{entry:,.4f}`",
            f"Exit     : `{exit_price:,.4f}`",
            f"P&L      : `{sign}{pnl:,.2f} USDT  ({sign}{pnl_pct:.2f}%)`",
            f"Reason   : `{reason}`",
        ]
        if hold_hours:
            lines.append(f"Held     : `{hold_hours:.1f}h`")
        lines.append(f"_At {_now()}_")
        self._send("\n".join(lines))

    # ── Risk events ───────────────────────────────────────────────────────────

    def circuit_breaker_tripped(self, drawdown_pct: float, equity: float) -> None:
        msg = (
            f"{_ERR} *CIRCUIT BREAKER TRIPPED*\n"
            f"Drawdown : `{drawdown_pct:.1f}%`\n"
            f"Equity   : `${equity:,.2f}`\n"
            f"Status   : All new buys blocked until equity recovers.\n"
            f"_At {_now()}_"
        )
        self._send(msg)

    def circuit_breaker_warning(self, drawdown_pct: float, equity: float) -> None:
        msg = (
            f"{_WARN} *Circuit Breaker WARNING*\n"
            f"Drawdown : `{drawdown_pct:.1f}%`\n"
            f"Equity   : `${equity:,.2f}`\n"
            f"_Position sizes reduced to 50%_\n"
            f"_At {_now()}_"
        )
        self._send(msg)

    def daily_loss_limit_hit(self, daily_loss_pct: float, equity: float) -> None:
        msg = (
            f"{_ERR} *DAILY LOSS LIMIT HIT*\n"
            f"Loss today : `{daily_loss_pct:.1f}%`\n"
            f"Equity     : `${equity:,.2f}`\n"
            f"Status     : No new trades until tomorrow UTC.\n"
            f"_At {_now()}_"
        )
        self._send(msg)

    def regime_change(self, old_regime: str, new_regime: str, confidence: float) -> None:
        msg = (
            f"{_CHART} *REGIME CHANGE*\n"
            f"{old_regime} → `{new_regime}` "
            f"(confidence {confidence:.0%})\n"
            f"_Reallocating strategy weights._\n"
            f"_At {_now()}_"
        )
        self._send(msg)

    # ── System events ─────────────────────────────────────────────────────────

    def bot_started(self, mode: str, symbol: str, balance: float) -> None:
        msg = (
            f"{_OK} *BOT STARTED*\n"
            f"Mode    : `{mode.upper()}`\n"
            f"Symbol  : `{symbol}`\n"
            f"Balance : `${balance:,.2f}`\n"
            f"_At {_now()}_"
        )
        self._send(msg)

    def bot_stopped(self, reason: str = "Ctrl+C", equity: float = 0.0) -> None:
        msg = (
            f"{_WARN} *BOT STOPPED*\n"
            f"Reason : `{reason}`\n"
            f"Equity : `${equity:,.2f}`\n"
            f"_At {_now()}_"
        )
        self._send(msg)

    def error_alert(self, context: str, error: str) -> None:
        msg = (
            f"{_ERR} *ERROR*\n"
            f"Context : `{context}`\n"
            f"Error   : `{error[:200]}`\n"
            f"_At {_now()}_"
        )
        self._send(msg)

    def heartbeat(
        self,
        equity:         float,
        open_positions: int = 0,
        candle_count:   int = 0,
        regime:         str = "",
        min_interval_m: float = 30.0,
    ) -> None:
        """
        Send a heartbeat message. Throttled to at most once every min_interval_m
        minutes so the bot doesn't spam you every candle.
        """
        now = datetime.now(timezone.utc)
        if self._last_heartbeat is not None:
            elapsed = (now - self._last_heartbeat).total_seconds() / 60
            if elapsed < min_interval_m:
                return   # Too soon

        self._last_heartbeat = now
        lines = [
            f"{_PULSE} *Heartbeat*",
            f"Equity   : `${equity:,.2f}`",
            f"Positions: `{open_positions}`",
            f"Candles  : `{candle_count:,}`",
        ]
        if regime:
            lines.append(f"Regime   : `{regime}`")
        lines.append(f"_At {_now()}_")
        self._send("\n".join(lines))

    def daily_summary(
        self,
        date:      str,
        pnl:       float,
        trades:    int,
        win_rate:  float,
        equity:    float = 0.0,
    ) -> None:
        sign  = "+" if pnl >= 0 else ""
        emoji = _MONEY if pnl >= 0 else _WARN
        lines = [
            f"{emoji} *Daily Summary — {date}*",
            f"P&L      : `{sign}{pnl:,.2f} USDT`",
            f"Trades   : `{trades}`",
            f"Win Rate : `{win_rate:.1f}%`",
        ]
        if equity:
            lines.append(f"Equity   : `${equity:,.2f}`")
        self._send("\n".join(lines))

    # ── Internal send ─────────────────────────────────────────────────────────

    def _send(self, text: str) -> None:
        """Fire-and-forget: send message in background thread. Never raises."""
        if not self.enabled:
            return
        t = threading.Thread(target=self._send_sync, args=(text,), daemon=True)
        t.start()

    def _send_sync(self, text: str) -> None:
        """Synchronous send. Called from background thread."""
        if not _REQUESTS_AVAILABLE:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }
        for attempt in range(3):
            try:
                resp = _requests.post(url, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    return
                # Rate limit: wait and retry
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(retry_after)
                    continue
                # Other error — log to stderr, don't crash
                print(f"[Telegram] Send failed ({resp.status_code}): {resp.text[:100]}")
                return
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    print(f"[Telegram] Send error (suppressed): {e}")


# ── Singleton accessor ────────────────────────────────────────────────────────

_instance: Optional[TelegramNotifier] = None

def get_notifier() -> TelegramNotifier:
    """Return the shared TelegramNotifier singleton (auto-initialized from config)."""
    global _instance
    if _instance is None:
        _instance = TelegramNotifier()
    return _instance


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
