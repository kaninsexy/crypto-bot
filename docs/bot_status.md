# Bot Status

Last updated: 2026-04-25 (post Phase 3b Chunks 1-6)
Supersedes the 2026-04-17 snapshot (preserved in git history).

## Current state

- **Current commit:** `80fc2c9` — Phase 3b Chunks 1-6 stack pushed 
  to `main` (6 commits on top of `f2d29cf`: holdout accessor + 
  JSONL plumbing; manifest generator + initial manifest; cache 
  enforcement + runner dev-only loading; validation_framework.md 
  correction; `trials.py` writer + `cpcv.py` skeleton; `cpcv.py` 
  block Sharpe distribution). Plus the earlier research 
  consolidation commits (Phase 3a.1 vectorization, docs 
  consolidation, milestone backtest reports, research_log 
  Phase 1-3a findings).
- **Working tree:** clean.
- **Server deployment:** `kanin@104.248.145.189`, still on older commit `4a51f0b`
  (pre-Phase 3a). The server has NOT been updated with Phase 3a changes yet.
- **Bot state:** OKX paper mode. 10 strategies configured in code, of which
  3 are genuinely working, 4 are broken (diagnosed), 3 are borderline
  (need rescue or retire decision).

## Per-strategy status

From the 3-year backtest analysis (9/10 completed; DualMomentum killed mid-run).

| Strategy | Symbol | Status | OOS Ret% | OOS Sharpe | Trades | Notes |
|---|---|---|---|---|---|---|
| VWAP | ETH/USDT | Working | +17.43% | +2.30 | 123 | Only strong OOS Sharpe in 3-year |
| BearShort | BTC/USDT | Working | +0.35% | +1.11 | 69 | Shorts verified working (side="short") |
| GridTrading | SOL/USDT | Working | +0.20% | +0.73 | 359 | Grinding positive |
| DCA | BTC/USDT | Concerning | -7.15% | -0.83 | 52 | 92% win rate but losing — risk/reward imbalance |
| MeanReversion | ETH/USDT | Failing | -4.19% | -2.27 | 13 | Barely fires; EMA filter too tight |
| Supertrend | ETH/USDT | Broken | -46.07% | -2.78 | 95 | 29.5% win rate, avg loss > avg win |
| TrendFollowing | BTC/USDT | Broken | -38.17% | -2.64 | 119 | 28.6% win rate too low for EMA9/21 |
| Breakout | AVAX/USDT | Broken | -36.16% | -2.78 | 47 | 91.5% stop_loss exits — breakouts fail on AVAX |
| VolatilityBreakout | BTC/USDT | Broken | -21.87% | -2.98 | 415 | 1-candle exit design flaw, net-negative EV |
| DualMomentum | BTC/USDT (rotates BTC/ETH/BNB) | Incomplete | — | — | — | 3-year run killed at 150-min timeout; 3-month smoke showed 55 rotations working |

Bucket summary: 3 working (VWAP, BearShort, GridTrading), 4 broken
(Supertrend, TrendFollowing, Breakout, VolatilityBreakout), 2 borderline
(DCA, MeanReversion), 1 incomplete (DualMomentum).

See `docs/strategies.md` for per-strategy diagnosis and next actions, and
`docs/strategy_failure_analysis_2026-04-19.md` for the underlying failure
write-up on the four broken strategies.

## Phase 3b infrastructure (Chunks 1-6 complete)

The holdout split foundation is live. Future backtest data access 
goes through `backtest/holdout.py`, not directly through the L1 
cache.

| File | Role |
|------|------|
| `backtest/holdout_manifest.json` | Per-strategy `data_start`, `dev_end`, `holdout_start`, `data_end`, `timeframe`. Source of truth for the split. |
| `backtest/holdout.py` | Public accessors `load_dev(strategy_id)` and `load_holdout(strategy_id, caller=..., reason=...)`. Strict single-access enforced on holdout. |
| `backtest/holdout_access.log` | Append-only audit log. Every `load_holdout` call appends one event; every regenerate appends one `regenerated=true` event. |
| `backtest/generate_holdout_manifest.py` | `generate_initial()` for first setup; `regenerate_manifest()` for redrawing the split. |
| `backtest/cache.py` | Enforces `HoldoutBypass` on any read overlapping holdout. Pass `until_ts=get_symbol_dev_cutoff(symbol)` to restrict to dev. |
| `backtest/logs.py` | JSONL read/write plumbing, schema-agnostic. |
| `backtest/trials.py` | Schema-validating JSONL writer for `backtest/trials.log`. Schema v1, sacred-harness-adjacent. Per-trial-type required-field enforcement; canonical sha256 `params_hash`; final-gate guard cross-referenced against `holdout_access.log`. |
| `backtest/cpcv.py` | Block Sharpe distribution. Equal-row block split of the dev window, fresh `strategy_factory()` per block, per-block Sharpe via the engine's formula, purge/embargo at boundaries. Replaces López de Prado path-CPCV (see `docs/validation_framework.md` § "Block Sharpe distribution"). |

Caller convention for `load_holdout`: 
`<phase>.<strategy_id>.<purpose>` — phase ∈ {phase3c, phase3d, 
phase4, phase5, manual}, strategy_id matches manifest key 
case-sensitively, purpose ∈ {final_dsr, regression_check, 
manual_inspection}. Validated by regex; invalid callers raise 
without writing to the audit log.

Modifying `backtest/holdout.py`, `backtest/holdout_manifest.json` 
schema, or `docs/validation_framework.md` requires human approval 
per `CLAUDE.md` (sacred harness).

## Kelly sizing state

Phase 2c regime-aware wiring is in place (`KellyCalculator` checks
`REGIME_PRIORS[regime][strategy]` first). However, `REGIME_PRIORS` at
`portfolio/kelly.py:223` is currently an empty dict. Every Kelly calculation
falls back to `ALL_REGIME_FALLBACK` at line 141. Result: the bot is
functionally using pre-Phase 2c Kelly logic until Phase 3d produces
per-regime Sharpe data to populate `REGIME_PRIORS`.

Additionally, `portfolio/kelly.py:353` sets `recommended_kelly = half_kelly
if n_trades >= 20 else quarter_kelly`, and line 336 returns
`recommended_kelly=0.0` in an early-return case. Low-trade strategies
therefore have Kelly sizing at zero or quarter fractions. Phase 3c/3d will
resolve this as validated strategies accumulate enough trades.

## Backtest performance

- Full 3-year run currently takes ~245 min on the dev machine with vectorized
  Supertrend and BearShort committed in the working tree.
- Supertrend alone accounts for roughly 50 min of that.
- This is acceptable for deploy-gate runs but too slow for tight iteration —
  Phase 3b work will lean heavily on shorter CPCV paths and cached data.

## Infrastructure

- **L1 OHLCV parquet cache** with a 24-hour TTL — persistent across runs, keyed
  by `(exchange, symbol, timeframe, since)`.
- **Per-strategy symbols** sourced from `config.STRATEGY_SYMBOLS`, replacing
  the earlier "everything trades BTC" assumption.
- **Multi-symbol rotation** support in the engine for DualMomentum
  (BTC/ETH/BNB basket, best-momentum wins each evaluation window).

### Development infrastructure

- **Mac (`kanin@MacBook-Pro-khxngKanin`):** primary development. Code edits,
  chat with Claude, git operations.
- **Threadripper PC (specs pending from Kanin):** planned compute node for
  heavy backtest runs. Integration via Claude Code SSH tunnel (Option B).
  Setup pending Phase 3b start.
- **DigitalOcean Singapore (`kanin@104.248.145.189`):** paper-trading
  production. Currently on commit `4a51f0b` (pre-Phase 3a).

### Deploy gates

- **Iteration:** 3-month smoke test (fast, local, exploratory).
- **Deploy gate:** 3-year full backtest + CPCV + DSR on holdout (slow,
  complete validation).

Iterative work uses the former; nothing ships to paper without passing the
latter.

## Key files

- Checkpoint (server): `/home/botuser/crypto_bot/dashboard/data/portfolio_checkpoint.json`
- Paper state (server): `/home/botuser/crypto_bot/dashboard/data/paper_state.json`
- Bot logs (server): `sudo journalctl -u cryptobot --no-pager`

## Server commands

- SSH: `ssh kanin@104.248.145.189`
- Git pull: `sudo bash -c "cd /home/botuser/crypto_bot && git pull"`
- Restart: `sudo systemctl restart cryptobot cryptodashboard`
- Logs: `sudo journalctl -u cryptobot -n 50 --no-pager`

## What changed since the 2026-04-17 snapshot

- Phase 2c (regime-aware Kelly) was completed at commit `4a51f0b`.
- Phase 3a shipped (commit `f2d29cf`): backtest redesign with per-strategy
  symbols, L1 OHLCV cache, DualMomentum multi-symbol rotation, and the
  `base.py` kwarg fix.
- 3-year backtest was run across 9/10 strategies; DualMomentum hit the
  150-min process cap.
- Supertrend and BearShort were vectorized in the working tree for the
  ~10× speedup noted above.
- Strategy reality check: only 3 of 10 show positive OOS Sharpe on 3-year data,
  which forces the Phase 3b validation framework before any further
  iteration or deploy.
- Phase 3b chunk 5 shipped: `trials.py` writer for `backtest/trials.log`,
  CPCV skeleton, schema-validating JSONL append, final-gate guard.
- Phase 3b chunk 6 shipped: block Sharpe distribution implementation in
  `backtest/cpcv.py` replacing path-CPCV (rule-based strategies have no
  fit/predict split, so path reassembly degenerates; block Sharpe preserves
  the multi-sample basis DSR requires). `docs/validation_framework.md` §
  "Block Sharpe distribution" updated to match.
