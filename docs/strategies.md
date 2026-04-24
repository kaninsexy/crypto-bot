# Strategies — Per-Strategy Roadmap

Last updated: 2026-04-25

This file holds the current status and next action for each strategy in the
portfolio. Diagnoses are drawn from the 3-year backtest analysis written up in
`docs/strategy_failure_analysis_2026-04-19.md`. The validation gating described
here will be run as part of Phase 3b (see `docs/validation_framework.md`).

Status legend:

- **Working** — positive OOS Sharpe on the 3-year backtest, keep pending formal DSR validation.
- **Borderline** — mixed OOS signal, likely needs rescue or retire in Phase 3b.
- **Failing** — net-negative OOS with a plausible rescue path.
- **Broken** — net-negative OOS with a diagnosed design or parameter failure.
- **Incomplete** — backtest did not finish; status unknown.

---

## VWAP — ETH/USDT

- **Status:** Working.
- **OOS:** +17.43% return, +2.30 Sharpe, 123 trades, ~48% win rate.
- **Diagnosis:** Only strategy with a strong OOS Sharpe on the 3-year run.
  OOS Sharpe actually improves over IS (+1.00 → +2.30), which is the opposite
  of the overfitting pattern — consistent with genuine mean-reversion edge
  around the daily VWAP on ETH.
- **Next action:** Keep as-is pending Phase 3b DSR validation on the holdout
  split.

---

## BearShort — BTC/USDT

- **Status:** Working.
- **OOS:** +0.35% return, +1.11 Sharpe, 69 trades.
- **Diagnosis:** Shorts are verified (trades open with `side="short"`). Returns
  are small but the risk-adjusted number is positive and drawdown is tiny
  (0.30% OOS). This is a hedge-style contributor, not a return driver.
- **Next action:** Keep as-is pending Phase 3b DSR validation. Watch whether the
  small absolute return survives multiple-testing correction.

---

## GridTrading — SOL/USDT

- **Status:** Working.
- **OOS:** +0.20% return, +0.73 Sharpe, 359 trades, ~79% win rate.
- **Diagnosis:** Grinding positive. High trade count and high win rate with
  small edge per trade — classic grid behaviour on a range-bound pair.
- **Next action:** Keep as-is pending Phase 3b DSR validation. Because the
  absolute return is small, DSR on 359 trades will be the real test.

---

## DCA — BTC/USDT

- **Status:** Borderline.
- **OOS:** -7.15% return, -0.83 Sharpe, 52 trades, 92.3% win rate.
- **Diagnosis:** A 92% win rate with a negative return is a tell: wins are
  small and a handful of large losses drag the total negative. Martingale
  safety orders encounter drawdowns in OOS that exceed the strategy's
  recovery capacity over three years. Risk/reward imbalance, not a signal
  problem.
- **Next action:** Rescue attempt in Phase 3b. Candidate variations: tighter
  max safety-order count, smaller martingale multiplier, hard per-cycle max
  loss, or a regime filter that blocks new cycles in BEAR/CRASH.

---

## MeanReversion — ETH/USDT

- **Status:** Failing.
- **OOS:** -4.19% return, -2.27 Sharpe, 13 trades, 15.4% win rate.
- **Diagnosis:** Barely fires (13 OOS trades in 3 years) and loses when it does.
  Current EMA filter appears too tight — signal is being gated away almost
  entirely, and the few trades that slip through have poor risk/reward.
- **Next action:** Rescue attempt in Phase 3b. Candidate variations: loosen or
  remove the EMA filter, retune RSI and Bollinger thresholds on training data,
  or consider an alternative pair better suited to mean reversion.

---

## Supertrend — ETH/USDT

- **Status:** Broken.
- **OOS:** -46.07% return, -2.78 Sharpe, 95 trades, 29.5% win rate.
- **Diagnosis:** OOS avg win ($95) is smaller than OOS avg loss ($107) at a
  29.5% win rate — expected value per trade is roughly -$47. Wins come from
  the trailing stop running profits; losses come from waiting for Supertrend
  to flip, which gives back too much. See
  `docs/strategy_failure_analysis_2026-04-19.md` §1 for detail.
- **Next action:** Rescue attempt in Phase 3b. Candidate variations: tighter
  trailing stop, regime alignment filter (only trade when higher-timeframe
  trend agrees), or replacing the flip-exit with an ATR-based exit.

---

## TrendFollowing — BTC/USDT

- **Status:** Broken.
- **OOS:** -38.17% return, -2.64 Sharpe, 119 trades, 28.6% win rate.
- **Diagnosis:** Avg win/loss ratio is actually fine (+1.31% vs -1.15%). The
  problem is that 28.6% win rate is too low for a trend strategy at this ratio
  — it needs 40%+ to be profitable. EMA9/21 on 1h BTC generates too many
  false signals in choppy regimes. See failure analysis §2.
- **Next action:** Rescue attempt in Phase 3b. Candidate variations: longer
  lookback EMAs (e.g. 21/55), ADX confirmation to filter chop, or a higher
  timeframe.

---

## Breakout — AVAX/USDT

- **Status:** Broken.
- **OOS:** -36.16% return, -2.78 Sharpe, 47 trades, 25.5% win rate.
- **Diagnosis:** 91.5% of OOS exits are stop_loss. IS avg win was +$248, OOS
  collapsed to +$138 — barely exceeds avg loss. Breakouts on AVAX mostly
  resolve as fakeouts; IS looks like overfitting to a specific AVAX regime.
  See failure analysis §3.
- **Next action:** Rescue attempt in Phase 3b, though prospects are poor on
  this pair. Candidate variations: volume-confirmation threshold, retest-based
  entry instead of break-and-go, or pair substitution (human approval needed,
  per `CLAUDE.md`).

---

## VolatilityBreakout — BTC/USDT

- **Status:** Broken.
- **OOS:** -21.87% return, -2.98 Sharpe, 415 trades, ~40% win rate.
- **Diagnosis:** Trades ~90× per month and exits every trade at the next candle's
  open regardless of profit or loss — never lets winners run. At ~40% win rate
  with near-symmetric win/loss size, expected value is negative by design.
  Death by many small cuts. See failure analysis §4.
- **Next action:** Rescue attempt in Phase 3b, though the issue is structural.
  Candidate variations: replace the hard 1-candle exit with a trailing stop or
  minimum hold period, or gate entries on a regime/volatility condition. If no
  variation survives, retire.

---

## DualMomentum — BTC/USDT (rotates BTC/ETH/BNB)

- **Status:** Incomplete.
- **OOS:** Not available. The 3-year run was killed at the 150-min process cap
  mid-IS. A 3-month smoke showed 55 rotations firing correctly.
- **Diagnosis:** Engine behaviour looks right on the smoke; the full-run result
  is simply missing.
- **Next action:** Complete the 3-year run (no time limit, or chunked by year)
  before evaluating. If the run remains infeasible, the documented decision
  will be to accept 9/10 strategies as the deploy baseline and park
  DualMomentum.

---

## Summary bucket

- **Keep as-is pending Phase 3b DSR validation:** VWAP, BearShort, GridTrading.
- **Rescue attempt in Phase 3b:** DCA, MeanReversion, Supertrend, TrendFollowing,
  Breakout, VolatilityBreakout.
- **Complete 3-year run first, then evaluate:** DualMomentum.
