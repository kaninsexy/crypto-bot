"""
config.py — Central configuration loader.

Reads all settings from environment variables (loaded from .env file).
Every other module imports from here instead of touching os.environ directly.
This keeps secrets and settings in one place and easy to change.
"""

import os
from dotenv import load_dotenv

# Load .env file into environment variables (does nothing if file doesn't exist)
load_dotenv()


# ─── Binance credentials ──────────────────────────────────────────────────────

BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")


# ─── Trading settings ─────────────────────────────────────────────────────────

# "paper" → simulate trades locally, no real money
# "live"  → execute real orders on Binance
TRADING_MODE: str = os.getenv("TRADING_MODE", "paper").lower()

# The market pair to trade, e.g. "BTC/USDT", "ETH/USDT"
TRADING_PAIR: str = os.getenv("TRADING_PAIR", "BTC/USDT")

# Candle timeframe: 1m, 5m, 15m, 1h, 4h, 1d
TIMEFRAME: str = os.getenv("TIMEFRAME", "1h")

# How many historical candles to fetch for indicator calculations
# Most indicators (e.g. 200 EMA) need at least 200 candles.
CANDLE_LIMIT: int = int(os.getenv("CANDLE_LIMIT", "300"))


# ─── Paper trading settings ───────────────────────────────────────────────────

# Starting USDT balance for the paper trading simulator
PAPER_BALANCE: float = float(os.getenv("PAPER_BALANCE", "10000.0"))


# ─── Risk management ──────────────────────────────────────────────────────────

# Maximum % of portfolio to risk on a single trade (e.g. 0.02 = 2%)
MAX_RISK_PER_TRADE: float = float(os.getenv("MAX_RISK_PER_TRADE", "0.02"))

# Stop-loss percentage below entry price (e.g. 0.03 = 3%)
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.03"))

# Take-profit percentage above entry price (e.g. 0.06 = 6%)
TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "0.06"))


# ─── Multi-symbol strategy assignment ────────────────────────────────────────
# Each strategy runs on its own optimal coin.
# Override individual symbols via env vars, e.g:
#   SYMBOL_DCA=BTC/USDT
#   SYMBOL_GRID=SOL/USDT
#
# Rationale for defaults:
#   DCA / TrendFollowing — BTC/USDT: blue-chip, reliable long-term upside
#   MeanReversion        — LINK/USDT: strong range-reversion tendencies
#   GridTrading          — SOL/USDT: volatile but bounded, high grid profit density
#   Breakout             — AVAX/USDT: strong momentum moves, clean volume surges
#   Supertrend           — ETH/USDT: liquid, well-behaved trend structure
#   BearShort            — BTC/USDT: most reliable directional futures instrument
#   VWAP                 — ETH/USDT: institutional volume patterns present

STRATEGY_SYMBOLS: dict = {
    "DCA":                os.getenv("SYMBOL_DCA",           "BTC/USDT"),
    "Supertrend":         os.getenv("SYMBOL_SUPERTREND",     "ETH/USDT"),
    "MeanReversion":      os.getenv("SYMBOL_MEANREV",        "ETH/USDT"),
    "GridTrading":        os.getenv("SYMBOL_GRID",           "SOL/USDT"),
    "Breakout":           os.getenv("SYMBOL_BREAKOUT",       "AVAX/USDT"),
    "TrendFollowing":     os.getenv("SYMBOL_TREND",          "BTC/USDT"),
    "BearShort":          os.getenv("SYMBOL_BEARSHORT",      "BTC/USDT"),
    "VWAP":               os.getenv("SYMBOL_VWAP",           "ETH/USDT"),
    "VolatilityBreakout": os.getenv("SYMBOL_VOLBREAKOUT",    "BTC/USDT"),
    "DualMomentum":       os.getenv("SYMBOL_DUALMOMENTUM",   "BTC/USDT"),
}

# ─── Risk management — daily loss limit ──────────────────────────────────────

# If the portfolio loses more than this % of start-of-day equity in one day,
# all new buys are blocked until the next UTC day.
# Set to 0 to disable.
DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "5.0"))

# ─── Notifications (optional) ────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")


# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.path.join("logs", "bot.log")


# ─── Validation ───────────────────────────────────────────────────────────────

def validate():
    """
    Call this at startup to catch obvious misconfigurations early.
    Raises ValueError with a clear message if something is wrong.
    """
    if TRADING_MODE not in ("paper", "live"):
        raise ValueError(f"TRADING_MODE must be 'paper' or 'live', got: '{TRADING_MODE}'")

    if TRADING_MODE == "live":
        if not BINANCE_API_KEY or BINANCE_API_KEY == "your_api_key_here":
            raise ValueError("BINANCE_API_KEY is not set. Required for live trading.")
        if not BINANCE_API_SECRET or BINANCE_API_SECRET == "your_api_secret_here":
            raise ValueError("BINANCE_API_SECRET is not set. Required for live trading.")

    if MAX_RISK_PER_TRADE <= 0 or MAX_RISK_PER_TRADE > 0.5:
        raise ValueError(f"MAX_RISK_PER_TRADE should be between 0 and 0.5, got: {MAX_RISK_PER_TRADE}")

    print(f"[Config] Mode={TRADING_MODE.upper()} | Pair={TRADING_PAIR} | Timeframe={TIMEFRAME}")
