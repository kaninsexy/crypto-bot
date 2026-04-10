#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  run.sh — Convenience launcher for the crypto bot
#
#  Activates the virtual environment automatically, so you never need to
#  remember "source .venv/bin/activate" before running.
#
#  Usage:
#    ./run.sh                              → portfolio mode, one snapshot
#    ./run.sh --loop                       → portfolio mode, continuous
#    ./run.sh --strategy dca               → DCA strategy, one run
#    ./run.sh --strategy dca --loop        → DCA strategy, continuous
#    ./run.sh backtest                     → run the standalone backtest
#    ./run.sh help                         → show this help
# ─────────────────────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"

# ── Check setup has been run ──────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo ""
    echo "  Virtual environment not found. Run setup first:"
    echo "    ./setup_mac.sh"
    echo ""
    exit 1
fi

if [ ! -f ".env" ]; then
    echo ""
    echo "  .env file not found. Run setup first:"
    echo "    ./setup_mac.sh"
    echo ""
    exit 1
fi

# ── Activate venv ─────────────────────────────────────────────────────────────
source "$VENV/bin/activate"

# ── Route commands ────────────────────────────────────────────────────────────
case "${1:-}" in
    backtest)
        echo "Running standalone backtest (no API key needed)..."
        python backtest/standalone.py
        ;;
    backtest-full)
        echo "Running full backtest with real Binance data (downloads ~12mo of candles)..."
        python -m backtest.runner
        ;;
    dashboard)
        echo "Starting dashboard at http://localhost:5000 ..."
        python -m dashboard.server
        ;;
    help|--help|-h)
        echo ""
        echo "  Crypto Bot — run.sh commands:"
        echo ""
        echo "  ./run.sh                              Portfolio mode (one snapshot)"
        echo "  ./run.sh --loop                       Portfolio mode (continuous)"
        echo "  ./run.sh --strategy dca               DCA single run"
        echo "  ./run.sh --strategy dca --loop        DCA continuous"
        echo "  ./run.sh --strategy supertrend        Supertrend single run"
        echo "  ./run.sh --strategy meanrev           Mean reversion single run"
        echo "  ./run.sh --strategy grid              Grid trading single run"
        echo "  ./run.sh --strategy breakout          Breakout single run"
        echo "  ./run.sh backtest                     Standalone backtest (no API)
  ./run.sh backtest-full                Full backtest with real Binance data
  ./run.sh dashboard                    Start web dashboard at localhost:5000"
        echo "  ./run.sh help                         Show this help"
        echo ""
        ;;
    *)
        # Pass all arguments through to main.py
        # Default (no args): run portfolio mode once
        if [ $# -eq 0 ]; then
            python main.py --portfolio
        else
            python main.py "$@"
        fi
        ;;
esac
