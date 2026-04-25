"""
rescue/policy.py — Rescue-process policy constants.

Sourced by `backtest.runner`'s `dev_cpcv` mode and any future Phase 3c
tooling.  Centralised here so adjustments are one-line and reviewable.

Decisions encoded
─────────────────

`RESCUE_TRIAL_BUDGET = 20`
  The n_trials value passed to BLP's multiple-testing correction
  (Bailey & López de Prado eq. 7) for every dev_cpcv DSR computation
  during Phase 3c.

  Why 20, and why a fixed budget rather than `count_trials_for_dsr`:

  1. Phase 3c's iteration cap is 20 variations per strategy
     (`CLAUDE.md` § "Iteration cap per strategy").  Calibrating the
     gate to the maximum trial count the strategy is *allowed* to
     accumulate keeps the haircut symmetric across strategies and
     across attempts within a strategy — the gate doesn't get easier
     for early variations and doesn't punish strategies that
     accumulate trials early.

  2. Pre-3b search burden (the parameter sweeps the human ran during
     Phases 1–3a, before the validation harness existed) is out of
     scope.  Counting those would either require reconstructing them
     from git history (lossy) or assigning an arbitrary number
     (worse).  Phase 3c's gate is calibrated against the Phase 3c
     search budget only; pre-3b results are treated as exploratory
     priors that informed which strategies to put through the gate at
     all, not as multiple-testing trials within the gate.

  3. Using `trials.count_trials_for_dsr(strategy_id)` instead would
     mean the threshold drifts as a strategy accumulates trials
     during its own rescue iteration — early variations would face a
     lower bar than late ones.  That creates an incentive to commit
     early, which corrupts the search.  A fixed budget removes the
     incentive.

  This constant is the *only* number `dev_cpcv` uses for n_trials.
  It is recorded explicitly in each full_cpcv row's `n_trials` field,
  not derived at read time, so the row is self-describing for
  audit.

  Phase 3c only.  Phase 4 paper deploy and Phase 5 live deploy use
  different gating logic (monitor, then live cap) and don't consume
  this constant.
"""

RESCUE_TRIAL_BUDGET: int = 20
