# IntradaySeasonalityEffects — Literature Review

## Pre-trial gates (locked)

1. Single-pair: BTC/USDT (manifest notation) on 1H timeframe only.
2. Long-only: strategy MUST NEVER emit Signal.SELL when
   self._position_open == False. No short entries under any condition.
3. Pure time filter: no indicators, no external data, no ta library
   imports.
4. Entry at candle timestamped hour==21 UTC, exit at candle timestamped
   hour==23 UTC.
5. trial_type = full_cpcv. Do NOT run the trial in this session — build
   infra only.
6. Manifest holdout_start must equal DCA's: 2025-09-12T14:12:00+00:00
   data_start: 2023-04-20T15:00:00+00:00
   data_end: 2026-04-19T14:00:00+00:00
7. strategy_factory in trial script must create a FRESH
   IntradaySeasonalityEffects instance each call (so _position_open
   resets to False at each CPCV block boundary).
8. CPCV: n_blocks=10, k_held_out=2, purge=0, embargo=0.

## Hypothesis

Crypto perpetual futures exhibit statistically significant positive
returns during specific UTC hours (21-23 UTC window hypothesised),
exploitable as a systematic long-only intraday filter on BTC/USDT 1H
perps.

## Substrate

BTC/USDT 1H perpetual futures (OKX USDT-M). Dev window:
2023-04-20 to 2025-09-12. Holdout sealed.

## Literature

(No citations yet. Citations pending — see trial_queue.json sq-003 notes.)

## Variations

| variation_id                  | trial_id | verdict | sharpe  | notes          |
|-------------------------------|----------|---------|---------|----------------|
| intraday-hourly-long-21-23utc | d6d0e252a9494982bed3fad470dc5dba | RETIRE | -1.17 | sr=-1.17 vs baseline=1.69; dsr=5.2e-72; 7/10 blocks negative (mean=-1.03, std=1.46); 846 trades. Consistent loser across blocks — no edge in 21-23 UTC window. |

## Open questions

- What is the mechanism driving the 21-23 UTC window, if any?
  (US session close / Asia open overlap hypothesis.)
- Does the edge persist across multiple BTC pairs or is it BTC-specific?
- Sensitivity to entry_hour: test neighbouring windows (20-22, 22-00)
  after initial verdict to bound the data-snooping risk.

## Trial outcomes

| variation_id | date | verdict | sr_observed | dsr | n_trades |
|---|---|---|---|---|---|
| intraday-hourly-long-21-23utc | 2026-05-04 | dry-run | nan | nan | 0 |

## Verdict: RETIRE

Variation intraday-hourly-long-21-23utc retired after first full_cpcv trial
(trial_id d6d0e252a9494982bed3fad470dc5dba).

sr_observed=-1.17 vs BTC buy-and-hold baseline=1.69 (margin=-2.86).
DSR=5.2e-72. 7 of 10 CPCV blocks negative; block Sharpe mean=-1.03,
std=1.46. 846 trades across blocks — adequate sample, result is real.

Consistent with Baur et al. (2019) negative prior: no persistent
exploitable pattern at hourly UTC granularity. The 21-23 UTC window has
no edge. The 3 positive blocks (0.88, 1.12, 0.04) are within the noise
envelope of the std.

No follow-on variations planned. If hourly seasonality is revisited,
the correct entry point is a systematic scan across all 24 windows
with multiple-testing correction applied upfront — not cherry-picking
a second window after this one failed.

