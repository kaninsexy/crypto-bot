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
# Kept for reference / historical config. Active exchange is OKX (see below).

BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")


# ─── OKX credentials ─────────────────────────────────────────────────────────
# OKX requires three pieces: API key, secret, and a passphrase you set when
# creating the API key. All three are required for live trading.

OKX_API_KEY:    str = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET: str = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE: str = os.getenv("OKX_PASSPHRASE", "")


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

# Minimum USDT balance a strategy slot must hold before it is allowed to open
# a new position.  At small starting capital (~$5k), some regime allocations
# leave a slot with less than Binance's minimum notional order size (~$100–200).
# Any BUY signal from an underfunded slot is skipped and logged as a WARNING.
# Only BUY signals are blocked — SELL/HOLD always pass through.
# Override via: MIN_CAPITAL_PER_STRATEGY=200.0 in .env
MIN_CAPITAL_PER_STRATEGY: float = float(
    os.getenv("MIN_CAPITAL_PER_STRATEGY", "200.0")
)


# ─── DCA strategy sizing ──────────────────────────────────────────────────────
#
# base_amount for each DCA cycle is computed as:
#   base_amount = max($10, slot_capital × DCA_BASE_ORDER_PCT)
#
# At 1% (default):
#   $30,000 DCA capital  → base = $300
#   $2,000  DCA capital  → base = $20
#   $1,000  DCA capital  → base = $10  (floor enforced)
#
# $10 floor is Binance's minimum notional order size — orders below this
# are rejected by the exchange.
#
# Override via: DCA_BASE_ORDER_PCT=0.02 in .env
DCA_BASE_ORDER_PCT: float = float(os.getenv("DCA_BASE_ORDER_PCT", "0.01"))


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


# ─── Exchange reconciliation (live mode only) ────────────────────────────────
#
# After load_checkpoint(), the bot compares its internal positions against
# what actually exists on Binance Futures. See portfolio/reconciler.py.
#
# RECONCILE_AUTO_CLOSE_GHOST
#   false (default): log CRITICAL for unknown exchange positions, leave them open.
#   true:            send a reduce-only market close for ghost positions.
#                    Use with caution — only enable once you're confident the
#                    reconciler won't mistake a legitimate manual hedge as a ghost.
#
# RECONCILE_TIMEOUT_SECONDS
#   How long to wait for Binance to respond before giving up and continuing
#   startup without reconciliation. Default: 15 seconds.

RECONCILE_AUTO_CLOSE_GHOST: bool = os.getenv(
    "RECONCILE_AUTO_CLOSE_GHOST", "false"
).lower() in ("true", "1", "yes")

RECONCILE_TIMEOUT_SECONDS: int = int(os.getenv("RECONCILE_TIMEOUT_SECONDS", "15"))


# ─── Validation ───────────────────────────────────────────────────────────────

def validate():
    """
    Call this at startup to catch obvious misconfigurations early.
    Raises ValueError with a clear message if something is wrong.
    """
    if TRADING_MODE not in ("paper", "live"):
        raise ValueError(f"TRADING_MODE must be 'paper' or 'live', got: '{TRADING_MODE}'")

    if TRADING_MODE == "live":
        if not OKX_API_KEY:
            raise ValueError("OKX_API_KEY is not set.")
        if not OKX_API_SECRET:
            raise ValueError("OKX_API_SECRET is not set.")
        if not OKX_PASSPHRASE:
            raise ValueError("OKX_PASSPHRASE is not set.")

    if MAX_RISK_PER_TRADE <= 0 or MAX_RISK_PER_TRADE > 0.5:
        raise ValueError(f"MAX_RISK_PER_TRADE should be between 0 and 0.5, got: {MAX_RISK_PER_TRADE}")

    print(f"[Config] Mode={TRADING_MODE.upper()} | Pair={TRADING_PAIR} | Timeframe={TIMEFRAME}")
