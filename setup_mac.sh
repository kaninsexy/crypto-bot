#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  setup_mac.sh — One-command setup for macOS
#
#  Run this once from the crypto_bot folder:
#    chmod +x setup_mac.sh
#    ./setup_mac.sh
#
#  What it does:
#    1. Checks Python 3.11+
#    2. Creates an isolated virtual environment (.venv/)
#    3. Installs all Python dependencies
#    4. Creates your .env config file (if one doesn't exist yet)
#    5. Creates the logs/ directory
#    6. Runs a quick smoke-test to confirm everything works
# ─────────────────────────────────────────────────────────────────────────────

set -e   # Exit immediately if any command fails

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
RESET="\033[0m"

ok()   { echo -e "${GREEN}  ✓ $*${RESET}"; }
info() { echo -e "${CYAN}  → $*${RESET}"; }
warn() { echo -e "${YELLOW}  ⚠ $*${RESET}"; }
fail() { echo -e "${RED}  ✗ $*${RESET}"; exit 1; }

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════${RESET}"
echo -e "${CYAN}  Crypto Bot — macOS Setup${RESET}"
echo -e "${CYAN}═══════════════════════════════════════════════${RESET}"
echo ""

# ── Step 1: Check we're in the right directory ────────────────────────────────
if [ ! -f "main.py" ] || [ ! -f "requirements.txt" ]; then
    fail "Run this script from inside the crypto_bot folder. Example:
       cd ClaudeTrading/crypto_bot
       ./setup_mac.sh"
fi
ok "Running from correct directory"

# ── Step 2: Check Python version ─────────────────────────────────────────────
info "Checking Python version..."

# Try python3 first, then python
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 11 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    fail "Python 3.11 or newer is required but was not found.

  Install it from: https://www.python.org/downloads/macos/
  Or with Homebrew: brew install python@3.13

  After installing, rerun this script."
fi

ok "Found Python $VER ($PYTHON)"

# ── Step 3: Create virtual environment ────────────────────────────────────────
VENV_DIR=".venv"

if [ -d "$VENV_DIR" ]; then
    warn "Virtual environment already exists at .venv/ — skipping creation."
    warn "To rebuild from scratch: rm -rf .venv && ./setup_mac.sh"
else
    info "Creating virtual environment at .venv/ ..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtual environment created"
fi

# Activate the venv for the rest of this script
source "$VENV_DIR/bin/activate"

# ── Step 4: Upgrade pip silently ──────────────────────────────────────────────
info "Upgrading pip..."
pip install --upgrade pip --quiet
ok "pip up to date"

# ── Step 5: Install dependencies ─────────────────────────────────────────────
info "Installing dependencies from requirements.txt ..."
pip install -r requirements.txt --quiet
ok "All dependencies installed"

# ── Step 6: Create .env from template ────────────────────────────────────────
if [ -f ".env" ]; then
    ok ".env already exists — skipping (your settings are preserved)"
else
    if [ -f ".env.example" ]; then
        cp .env.example .env
        ok ".env created from .env.example"
        echo ""
        warn "ACTION REQUIRED: Open .env and fill in your settings:"
        warn "  PAPER mode (no real money) — you can leave API keys blank."
        warn "  LIVE mode — add your Binance API key and secret."
        echo ""
    else
        warn ".env.example not found. Creating a minimal .env..."
        cat > .env << 'ENVEOF'
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
TRADING_MODE=paper
TRADING_PAIR=BTC/USDT
TIMEFRAME=1h
CANDLE_LIMIT=300
PAPER_BALANCE=10000.0
MAX_RISK_PER_TRADE=0.02
STOP_LOSS_PCT=0.03
TAKE_PROFIT_PCT=0.06
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
LOG_LEVEL=INFO
ENVEOF
        ok "Minimal .env created"
    fi
fi

# ── Step 7: Create logs directory ─────────────────────────────────────────────
mkdir -p logs
ok "logs/ directory ready"

# ── Step 8: Smoke test ────────────────────────────────────────────────────────
info "Running smoke test (imports only, no network)..."
python - <<'PYEOF'
import sys
errors = []

packages = [
    ("ccxt",        "ccxt"),
    ("pandas",      "pandas"),
    ("numpy",       "numpy"),
    ("ta",          "ta"),
    ("dotenv",      "python-dotenv"),
    ("loguru",      "loguru"),
    ("schedule",    "schedule"),
    ("requests",    "requests"),
]

for module, pkg in packages:
    try:
        __import__(module)
    except ImportError:
        errors.append(f"  MISSING: {pkg}  (pip install {pkg})")

if errors:
    print("Smoke test FAILED — missing packages:")
    for e in errors:
        print(e)
    sys.exit(1)

# Quick internal import check
sys.path.insert(0, ".")
try:
    import config
    from strategies.base import Signal, BaseStrategy
    from paper_trading.simulator import PaperTrading
    from portfolio.regime_detector import RegimeDetector
    from portfolio.kelly import KellyCalculator, PHASE_C_PROFILES
    from portfolio.circuit_breaker import CircuitBreaker
    print("All imports OK")
except Exception as e:
    print(f"Import error: {e}")
    sys.exit(1)
PYEOF

ok "Smoke test passed — all imports work correctly"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════${RESET}"
echo -e "${GREEN}  Setup complete!${RESET}"
echo -e "${GREEN}═══════════════════════════════════════════════${RESET}"
echo ""
echo "  To activate the environment in a new terminal:"
echo -e "    ${CYAN}source .venv/bin/activate${RESET}"
echo ""
echo "  Quick commands (activate first, then):"
echo -e "    ${CYAN}python backtest/standalone.py${RESET}         # Run backtest (no API key needed)"
echo -e "    ${CYAN}python main.py --portfolio${RESET}            # Run portfolio once (paper)"
echo -e "    ${CYAN}python main.py --portfolio --loop${RESET}     # Run portfolio continuously"
echo -e "    ${CYAN}python main.py --strategy dca${RESET}         # Run single strategy"
echo ""
echo "  Edit your config:"
echo -e "    ${CYAN}nano .env${RESET}   (or open with any text editor)"
echo ""
