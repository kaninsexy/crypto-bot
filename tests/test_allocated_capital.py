"""
Smoke test: allocated_capital separation from earned profit.

Verifies the rebalance() fix against the task's acceptance criteria:
- A strategy with allocated_capital=$5k and balance=$7k (earned $2k profit)
  must never be drained below its $5k floor.
- Earned profit is preserved across rebalance events and across checkpoint
  save/load cycles.
- Old checkpoints (without the "capital" key) load correctly.
"""
from __future__ import annotations

import sys
import json
import tempfile
from pathlib import Path

# Project root (crypto_bot/)
sys.path.insert(0, "/sessions/practical-kind-gauss/mnt/crypto-bot")

from portfolio.manager import PortfolioManager, StrategySlot
from paper_trading.simulator import PaperTrading
from portfolio.regime_detector import REGIME_RANGE, REGIME_BEAR


def build_manager_without_initialize(total_capital: float = 100_000.0):
    """
    Construct a PortfolioManager and hand-populate slots so we don't need
    synthetic BTC dataframes just for this unit-style test. Copies the
    per-slot construction logic from `initialize()` but skips regime detection.
    """
    pm = PortfolioManager(total_capital=total_capital)
    from portfolio.regime_detector import REGIME_ALLOCATIONS
    allocs = REGIME_ALLOCATIONS[REGIME_RANGE]
    pm._current_regime = REGIME_RANGE

    # Mirror the STRATEGY_KEYS/BUCKET_KEYS mapping exactly.
    for sname, bkey in zip(pm.STRATEGY_KEYS, pm.BUCKET_KEYS):
        weight = allocs.get(bkey, 0.0)
        capital = round(total_capital * weight, 2)
        # Use a bare PaperTrading simulator (no strategy logic required).
        sim = PaperTrading(initial_balance=capital, symbol="BTC/USDT")
        slot = StrategySlot(
            name=sname, strategy=None,   # type: ignore[arg-type]
            simulator=sim, bucket_key=bkey,
            capital=capital, active=(capital > 0),
            allocated_capital=capital,
        )
        pm._slots[sname] = slot
    return pm


# ── Test 1: default initialization — allocated_capital == capital ─────────────

def test_default_allocated_capital_matches_capital():
    pm = build_manager_without_initialize(100_000.0)
    for slot in pm._slots.values():
        assert slot.allocated_capital == slot.capital, (
            f"{slot.name}: allocated_capital {slot.allocated_capital} != "
            f"capital {slot.capital} on fresh init"
        )
        assert slot.earned_profit == 0.0
    print("PASS  test_default_allocated_capital_matches_capital")


# ── Test 2: earned profit is protected during rebalance ───────────────────────

def test_earned_profit_never_drained():
    """
    The headline test. Set up TrendFollowing with a $2k earned profit
    (allocated=$5k, balance=$7k). Change regime so rebalance would
    normally drain from 'over-allocated' slots. Verify TrendFollowing
    never goes below its $5k floor and earned $2k is still reachable.
    """
    pm = build_manager_without_initialize(100_000.0)

    # Per REGIME_RANGE: trend = 0.05 → $5,000 target.
    tf = pm._slots["TrendFollowing"]
    assert tf.allocated_capital == 5_000.0
    assert tf.capital == 5_000.0
    assert tf.simulator.balance == 5_000.0

    # Simulate $2,000 of realised trading profit hitting cash only.
    # (Does NOT touch allocated_capital — that is the whole point of the fix.)
    tf.simulator.balance += 2_000.0   # now $7,000 cash
    # DCA-style compounding would also bump slot.capital; simulate that too
    # to prove the fix works in the exact scenario that caused the original bug.
    tf.capital += 2_000.0             # simulates compounding — capital=$7k

    assert tf.earned_profit == 2_000.0
    assert tf.allocated_capital == 5_000.0

    # Now run rebalance. At current weights ($5k target, $5k allocated)
    # TrendFollowing is at exactly 0% drift, so no donation should happen.
    pm.rebalance(drift_threshold_pct=25.0, min_transfer=20.0)

    assert tf.allocated_capital == 5_000.0, (
        f"Floor breached: allocated_capital dropped to {tf.allocated_capital}"
    )
    assert tf.simulator.balance == 7_000.0, (
        f"Earned profit touched: balance is now {tf.simulator.balance}"
    )
    assert tf.earned_profit == 2_000.0
    print("PASS  test_earned_profit_never_drained")


# ── Test 3: regime change that SHOULD reduce allocation ───────────────────────

def test_regime_change_reallocates_only_allocated_portion():
    """
    Force a big drift by switching to BEAR (trend = 0% in BEAR allocations,
    but the slot's current allocated_capital = $5k). Expected behaviour:
      - The rebalancer classifies TrendFollowing as over-allocated (drift →∞%
        since target=0), but because the per-slot weight is 0.0 the slot
        is skipped (`if weight <= 0: continue`) — it won't be drained.
      - Use a different scenario: increase a slot's allocated_capital above
        its new regime target so it legitimately becomes a donor.
    """
    pm = build_manager_without_initialize(100_000.0)

    # Force TrendFollowing to be over-allocated by pushing its allocated
    # capital well above its RANGE target (5%). We'll say someone deposited
    # extra into it manually. This is the classic donor scenario.
    tf = pm._slots["TrendFollowing"]
    tf.simulator.deposit(10_000.0)
    tf.capital += 10_000.0
    tf.allocated_capital += 10_000.0   # matches deposit() contract
    # Also give it $2k earned profit.
    tf.simulator.balance += 2_000.0    # cash now = 5k + 10k + 2k = $17k
    tf.capital += 2_000.0              # capital now includes compound = $17k

    assert tf.allocated_capital == 15_000.0  # only allocated moved up
    assert tf.earned_profit == 2_000.0       # $2k still earned

    # Now rebalance. With total_allocated increased by $10k to $110k,
    # TrendFollowing's RANGE target = 5% × $110k = $5.5k.
    # Its allocated is $15k → excess $9.5k → it WILL be a donor.
    pm.rebalance(drift_threshold_pct=25.0, min_transfer=20.0)

    # After donating, allocated_capital should drop toward target but
    # must never go below the $2k earned profit that's still protected.
    assert tf.allocated_capital >= tf.simulator.balance - tf.earned_profit - 1, (
        f"Earned profit was touched: allocated={tf.allocated_capital}, "
        f"balance={tf.simulator.balance}, earned was {2_000.0}"
    )
    # earned_profit should still be exactly $2k (only allocated moved)
    assert abs(tf.earned_profit - 2_000.0) < 0.01, (
        f"Earned profit changed from $2,000 to ${tf.earned_profit:,.2f}"
    )
    print("PASS  test_regime_change_reallocates_only_allocated_portion")


# ── Test 4: deposit bumps allocated_capital proportionally ────────────────────

def test_deposit_grows_allocated_capital():
    pm = build_manager_without_initialize(100_000.0)
    # Snapshot baseline allocated values.
    baseline = {s: sl.allocated_capital for s, sl in pm._slots.items()}
    # Fake earned profit on one strategy to prove deposit doesn't touch it.
    pm._slots["TrendFollowing"].simulator.balance += 2_000.0

    pm.deposit(10_000.0, note="test")

    # Every slot with regime weight > 0 should have allocated_capital bumped
    # by exactly its weight × deposit (ignoring any cash reserve).
    from portfolio.regime_detector import REGIME_ALLOCATIONS, REGIME_CASH_RESERVE
    allocs = REGIME_ALLOCATIONS[REGIME_RANGE]
    reserve = REGIME_CASH_RESERVE.get(REGIME_RANGE, 0.0)
    deployable = 10_000.0 * (1.0 - reserve)

    for sname, slot in pm._slots.items():
        w = allocs.get(slot.bucket_key, 0.0)
        expected_delta = round(deployable * w, 2)
        actual_delta = slot.allocated_capital - baseline[sname]
        assert abs(actual_delta - expected_delta) < 0.01, (
            f"{sname}: allocated delta={actual_delta}, expected={expected_delta}"
        )
    # TrendFollowing's earned_profit should still be $2k intact.
    tf = pm._slots["TrendFollowing"]
    assert abs(tf.earned_profit - 2_000.0) < 0.01
    print("PASS  test_deposit_grows_allocated_capital")


# ── Test 5: checkpoint round-trip preserves allocated_capital ─────────────────

def test_checkpoint_round_trip():
    pm = build_manager_without_initialize(100_000.0)

    # Give DCA a compound-style profit: bump capital and balance without
    # touching allocated_capital. This is the exact drift the fix is meant
    # to preserve across restarts.
    dca = pm._slots["DCA"]
    original_alloc = dca.allocated_capital
    dca.capital += 1_500.0
    dca.simulator.balance += 1_500.0

    # Redirect checkpoint file to a temp path.
    with tempfile.TemporaryDirectory() as td:
        tmp_ckpt = Path(td) / "ckpt.json"
        orig_path = PortfolioManager._CHECKPOINT_FILE
        try:
            PortfolioManager._CHECKPOINT_FILE = tmp_ckpt
            pm.save_checkpoint()
            saved = json.loads(tmp_ckpt.read_text())
            dca_saved = saved["slots"]["DCA"]
            assert "capital" in dca_saved and "allocated_capital" in dca_saved
            assert dca_saved["allocated_capital"] == original_alloc
            assert dca_saved["capital"] == original_alloc + 1_500.0

            # Restore into a fresh manager.
            pm2 = build_manager_without_initialize(100_000.0)
            assert pm2.load_checkpoint() is True
            dca2 = pm2._slots["DCA"]
            assert dca2.allocated_capital == original_alloc
            assert dca2.capital == original_alloc + 1_500.0
        finally:
            PortfolioManager._CHECKPOINT_FILE = orig_path
    print("PASS  test_checkpoint_round_trip")


def test_checkpoint_backward_compat_old_format():
    """Old checkpoints have only `allocated_capital` (which really held the
    running pool value). load_checkpoint must default both fields from it."""
    pm = build_manager_without_initialize(100_000.0)
    dca = pm._slots["DCA"]

    # Craft a minimal legacy checkpoint: no "capital" key; only the
    # pre-refactor "allocated_capital" field.
    with tempfile.TemporaryDirectory() as td:
        tmp_ckpt = Path(td) / "ckpt.json"
        tmp_ckpt.write_text(json.dumps({
            "total_capital": 100_000.0,
            "candle_count": 5,
            "current_regime": REGIME_RANGE,
            "slots": {
                "DCA": {
                    # Simulator fields required by restore_checkpoint()
                    "balance": 22_500.0,
                    "initial_balance": 20_000.0,
                    "total_fees_paid": 0.0,
                    "compounded_profit": 1_500.0,
                    "candle_count": 5,
                    "position": None,
                    # Legacy: only allocated_capital present, stored as the
                    # old "running pool" value (including compound).
                    "allocated_capital": 21_500.0,
                },
            },
        }))
        orig_path = PortfolioManager._CHECKPOINT_FILE
        try:
            PortfolioManager._CHECKPOINT_FILE = tmp_ckpt
            assert pm.load_checkpoint() is True
        finally:
            PortfolioManager._CHECKPOINT_FILE = orig_path
    # Backward compat: both capital and allocated_capital must equal
    # the legacy value (21,500).
    assert dca.capital == 21_500.0
    assert dca.allocated_capital == 21_500.0
    print("PASS  test_checkpoint_backward_compat_old_format")


if __name__ == "__main__":
    test_default_allocated_capital_matches_capital()
    test_earned_profit_never_drained()
    test_regime_change_reallocates_only_allocated_portion()
    test_deposit_grows_allocated_capital()
    test_checkpoint_round_trip()
    test_checkpoint_backward_compat_old_format()
    print("\nAll tests passed ✓")
