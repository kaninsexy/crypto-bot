# DualMomentum — Phase 4.A resurrection hypothesis-of-record

**Date:** 2026-04-29
**Phase:** 4.A drop-in batch
**Status:** Starting hypothesis (pre-trial)

## Phase 3c failure (2026-04-26)

RETIRE, observed_sharpe −2.3906 / dist mean −2.2850, n_trades 1095 on BTC/USDT (rotating BTC/ETH/BNB) at 1H. The 1H + 3-asset config contracts the Antonacci/Liu/Tsyvinski/Wu framework by ~720× (12-month formation → 21-hour formation). See `docs/strategy_evidence_audit_2026-04-26.md`.

## Starting hypothesis

**Weekly timeframe with 3-week formation period, applied to a ≥5-major basket.**

Source: Liu, Tsyvinski, Wu (2022) "Common Risk Factors in Cryptocurrency", *Journal of Finance* 77(2). Establishes weekly momentum factor in crypto cross-section using 3-week formation — closest peer-reviewed analog to dual-momentum in crypto, and the only one at a frequency the bot can implement.

Antonacci (2014) provides the dual-momentum framework (relative-strength rotation + absolute-momentum filter). Liu/Tsyvinski/Wu establishes the relative-strength leg empirically in crypto; the absolute-momentum filter (asset return must beat cash/risk-free) carries over from Antonacci directly.

## Basket specification

Top-5 by market capitalisation at evaluation time, refreshed monthly. Current snapshot (2026-04-29): BTC, ETH, BNB, SOL, XRP. The mcap-rank rule prevents lookahead bias from a fixed historical pick.

## Source citations

- Liu, Tsyvinski, Wu (2022), JF 77(2) — weekly crypto momentum factor
- Antonacci (2014) — dual-momentum framework
- Asness, Moskowitz, Pedersen (2013), JF 68(3) — cross-asset momentum generality
- `docs/strategy_evidence_audit_2026-04-26.md` — empirical basis for 1H/3-asset rejection

## Variation discipline

Per `CLAUDE.md`:
- 20-variation cap; this is variation #1 (post-Phase-3c)
- 3-consecutive-failure escalation
- Variations beyond #1 require their own source-cited justification before trial
- Pre-justified batch authority covers this starting hypothesis only

## Open implementation question (BLOCKS TRIAL)

The current holdout manifest entry for DualMomentum covers BTC/ETH/BNB. Adding SOL and XRP changes the manifest symbol set. The manifest is sacred-harness; per `CLAUDE.md` this requires explicit human approval before the trial runs. **This stub is committable now; the manifest update is a separate gate that must clear before the dev_cpcv runs.**
