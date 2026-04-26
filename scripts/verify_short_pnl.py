"""
scripts/verify_short_pnl.py — Independent verification of short-PnL sign.

Question: does paper_trading.simulator correctly handle realized PnL on
short closes, or is the sign inverted as the path-finder diagnostic claimed?

Method: instantiate the simulator with $10k balance. Force two short trades
through it — one where price drops (short wins, balance should go up) and
one where price rises (short loses, balance should go down). Compare
expected vs actual balance.

This script does NOT rely on the strategy layer, the runner, or any cache
data. It calls the simulator directly with synthetic OHLCV ticks.

Run: venv/bin/python scripts/verify_short_pnl.py
"""

from __future__ import annotations
import sys
from pathlib import Path

# Make repo root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Discovery: find the right module path ────────────────────────────────────
# The diagnostic referenced "paper_trading/simulator.py". Verify it exists.
SIMULATOR_PATH = Path(__file__).resolve().parent.parent / "paper_trading" / "simulator.py"
if not SIMULATOR_PATH.exists():
    # Fallback: try other plausible locations
    candidates = [
        Path(__file__).resolve().parent.parent / "simulator.py",
        Path(__file__).resolve().parent.parent / "backtest" / "simulator.py",
    ]
    for c in candidates:
        if c.exists():
            print(f"NOTE: simulator found at {c}, not paper_trading/simulator.py")
            SIMULATOR_PATH = c
            break
    else:
        print(f"ERROR: simulator.py not found at any expected location.")
        print(f"Tried: paper_trading/, ./, backtest/")
        sys.exit(1)

print(f"Verifying simulator at: {SIMULATOR_PATH}")
print(f"File size: {SIMULATOR_PATH.stat().st_size} bytes")
print()

# ── Read the relevant source lines so we can see what we're testing ─────────
print("─" * 70)
print("Source inspection: lines around the suspect _handle_full_sell")
print("─" * 70)
src = SIMULATOR_PATH.read_text().splitlines()
for i, line in enumerate(src, start=1):
    if "_handle_full_sell" in line or "def _handle" in line.lower():
        # Print a window of context
        lo = max(0, i - 2)
        hi = min(len(src), i + 30)
        print(f"\n  --- {SIMULATOR_PATH.name}:{i} ---")
        for j in range(lo, hi):
            print(f"  {j+1:5d}  {src[j]}")
        print()

print("─" * 70)
print("Source inspection: Position.unrealized_pnl (claimed correct)")
print("─" * 70)
for i, line in enumerate(src, start=1):
    if "unrealized_pnl" in line and ("def " in line or "@property" in src[max(0,i-2)]):
        lo = max(0, i - 3)
        hi = min(len(src), i + 12)
        print(f"\n  --- {SIMULATOR_PATH.name}:{i} ---")
        for j in range(lo, hi):
            print(f"  {j+1:5d}  {src[j]}")
        print()
        break  # just one is enough


# ── Behavioral test ──────────────────────────────────────────────────────────
print("─" * 70)
print("Behavioral test: force short trades through the simulator")
print("─" * 70)

# Import lazily so we can keep the source-inspection output even if import fails.
# Silence loguru — the simulator is chatty on every signal.
try:
    from loguru import logger as _logger  # type: ignore
    _logger.remove()
except ImportError:
    pass

try:
    from paper_trading.simulator import PaperTrading  # type: ignore
    from strategies.base import Signal               # type: ignore
except ImportError as e:
    print(f"\nImport failed: {e}")
    sys.exit(1)


# Adapter to the real PaperTrading API:
#   - constructor: PaperTrading(initial_balance=..., symbol=...)
#   - open: execute_signal(Signal(action="BUY", is_short=True, ...), price)
#       → quantity is derived from metadata["amount_usdt"] / price
#   - close: execute_signal(Signal(action="SELL", quantity_pct=1.0, ...), price)
#       → routes to _handle_full_sell(signal, price, signal.reason)
# We use a SELL signal (not a tick-driven SL/TP) so the close fires at the
# exact exit_price, with no slippage from candle high/low boundary effects.
def run_short_test(
    label: str,
    entry_price: float,
    exit_price: float,
    expect_wins: bool,
    cost_usdt: float = 3_000.0,
    starting_balance: float = 10_000.0,
) -> None:
    """
    Open a short at entry_price, close at exit_price, report whether the
    simulator's resulting balance moved the direction it should.

    A short wins when exit_price < entry_price (you sold high, bought back low).
    A short loses when exit_price > entry_price.
    """
    print(f"\n  Test: {label}")
    print(f"    entry_price={entry_price}  exit_price={exit_price}  cost_usdt={cost_usdt}")
    print(f"    expected: short {'WINS' if expect_wins else 'LOSES'}, "
          f"balance should {'rise' if expect_wins else 'fall'} from {starting_balance}")

    sim = PaperTrading(initial_balance=starting_balance, symbol="BTC/USDT")

    # ── Open short via execute_signal ─────────────────────────────────────
    open_signal = Signal(
        action="BUY",
        strategy="VerifyShortPnL",
        price=entry_price,
        reason="open short (synthetic)",
        stop_loss=entry_price * 1.05,    # SL above entry (correct for short)
        take_profit=entry_price * 0.95,  # TP below entry (correct for short)
        is_short=True,
        leverage=1,
        order_type="market",
        quantity_pct=1.0,
        metadata={"amount_usdt": cost_usdt},
    )
    sim.execute_signal(open_signal, entry_price)
    if sim.position is None:
        print("    !! sim.position is None after BUY — open failed.")
        return
    qty = sim.position.quantity
    pre_close_balance = sim.balance
    print(f"    qty opened: {qty:.6f}  | balance after open: {pre_close_balance:.2f}")

    # ── Close short via SELL signal at exit_price ─────────────────────────
    close_signal = Signal(
        action="SELL",
        strategy="VerifyShortPnL",
        price=exit_price,
        reason="close short (synthetic)",
        is_short=True,
        leverage=1,
        order_type="market",
        quantity_pct=1.0,
    )
    sim.execute_signal(close_signal, exit_price)

    post_close_balance = sim.balance
    delta = post_close_balance - starting_balance

    # The trade record's recorded PnL is what gets reported by the engine
    # and downstream metrics — that's the value most relevant to the bug claim.
    if sim.trade_history:
        rec = sim.trade_history[-1]
        recorded_pnl = rec.pnl
        recorded_side = rec.side
    else:
        recorded_pnl = float("nan")
        recorded_side = "n/a"

    # Real economic PnL for a short: (entry - exit) × qty − fees.
    # (We don't know fees precisely here without re-deriving them, but the
    # sign of (entry - exit) × qty is the ground truth.)
    real_pnl_pre_fees = (entry_price - exit_price) * qty

    print(f"    balance after close: {post_close_balance:.2f}")
    print(f"    delta from start: {delta:+.4f}")
    print(f"    sim.trade_history[-1].pnl  = {recorded_pnl:+.4f}  (side={recorded_side})")
    print(f"    real-economics short PnL  ≈ {real_pnl_pre_fees:+.4f}  (pre-fees)")

    # Verdict — primary check is the BALANCE movement direction.
    if expect_wins:
        if delta > 0:
            print(f"    ✓ CORRECT: short won, balance rose")
        else:
            print(f"    ✗ BUG CONFIRMED: short should have won (price dropped) "
                  f"but balance fell")
    else:
        if delta < 0:
            print(f"    ✓ CORRECT: short lost, balance fell")
        else:
            print(f"    ✗ BUG CONFIRMED: short should have lost (price rose) "
                  f"but balance rose")

    # Secondary check: does the recorded PnL sign agree with real economics?
    if recorded_pnl == 0 or real_pnl_pre_fees == 0:
        print(f"    (skip pnl-sign cross-check — zero PnL)")
    elif (recorded_pnl > 0) == (real_pnl_pre_fees > 0):
        print(f"    ✓ TradeRecord.pnl sign matches real economics")
    else:
        print(f"    ✗ TradeRecord.pnl sign INVERTED relative to real economics")


# Test 1: short that should WIN (price drops 10%)
run_short_test(
    label="winning short — price drops from $30k to $27k",
    entry_price=30_000.0, exit_price=27_000.0,
    expect_wins=True,
)

# Test 2: short that should LOSE (price rises 10%)
run_short_test(
    label="losing short — price rises from $30k to $33k",
    entry_price=30_000.0, exit_price=33_000.0,
    expect_wins=False,
)

# Test 3: long control — same setup but as a long. Should behave correctly
# regardless of any short-specific bug. Sanity check that the test harness
# itself is wired correctly.
def run_long_control(label, entry_price, exit_price, expect_wins,
                     cost_usdt=3_000.0, starting_balance=10_000.0):
    print(f"\n  Test: {label}")
    sim = PaperTrading(initial_balance=starting_balance, symbol="BTC/USDT")
    open_signal = Signal(
        action="BUY", strategy="VerifyLongCtrl", price=entry_price,
        reason="open long (synthetic)",
        stop_loss=entry_price * 0.95, take_profit=entry_price * 1.05,
        is_short=False, leverage=1, order_type="market", quantity_pct=1.0,
        metadata={"amount_usdt": cost_usdt},
    )
    sim.execute_signal(open_signal, entry_price)
    qty = sim.position.quantity if sim.position else 0
    close_signal = Signal(
        action="SELL", strategy="VerifyLongCtrl", price=exit_price,
        reason="close long (synthetic)", is_short=False, leverage=1,
        order_type="market", quantity_pct=1.0,
    )
    sim.execute_signal(close_signal, exit_price)
    delta = sim.balance - starting_balance
    rec = sim.trade_history[-1] if sim.trade_history else None
    print(f"    qty={qty:.6f} | delta={delta:+.4f} | "
          f"trade.pnl={rec.pnl if rec else float('nan'):+.4f} | side={rec.side if rec else 'n/a'}")
    if expect_wins and delta > 0:
        print(f"    ✓ long control passes (price up, long won)")
    elif (not expect_wins) and delta < 0:
        print(f"    ✓ long control passes (price down, long lost)")
    else:
        print(f"    ✗ long control FAILED — test harness itself may be broken")

run_long_control(
    label="control: winning long — price rises from $30k to $33k",
    entry_price=30_000.0, exit_price=33_000.0, expect_wins=True,
)
run_long_control(
    label="control: losing long — price drops from $30k to $27k",
    entry_price=30_000.0, exit_price=27_000.0, expect_wins=False,
)

print()
print("─" * 70)
print("Verification complete.")
print()
print("Interpretation:")
print("  - Both ✓  → simulator handles shorts correctly. Claude Code misread.")
print("  - Both ✗  → bug confirmed. Sign is inverted on every short close.")
print("  - Mixed   → something stranger; needs deeper inspection.")
print("─" * 70)
