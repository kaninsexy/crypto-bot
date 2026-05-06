# IdiosyncraticResidualTSMOM — literature stub

Strategy id: `IdiosyncraticResidualTSMOM`
Substrate: ETH/USDT (traded) + BTC/USDT (factor only) at 1H
Trial queue id: sq-009

## Hypothesis-of-record

Time-series momentum applied to the idiosyncratic residual returns of
ETH/USDT (after regressing against BTC/USDT as the market factor)
generates positive risk-adjusted returns vs ETH buy-and-hold.

## Sources

- Blitz, D., Huij, J., & Martens, M. (2011). "Residual momentum."
  Journal of Empirical Finance.
- Kim, J. (2022). "Idiosyncratic momentum in cryptocurrency markets."
  Applied Economics.

## Pre-trial gates (locked)

1. Long-only. BTC/USDT is the market-factor leg and is never traded
   (always emits HOLD).
2. ETH/USDT is the only traded leg.
3. Single concurrent ETH long; flat otherwise.
4. Single-pair test (ETH+BTC pair). No multi-pair extensions until
   Variation #1 verdict resolves.

## Variation table

| # | id | params | trial_type | sharpe | dsr | verdict | notes |
|---|----|--------|------------|--------|-----|---------|-------|
| 1 | idio-residual-tsmom-v1 | beta=720, mom=168 | full_cpcv | TBD | TBD | TBD | first trial |
