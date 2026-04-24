# Strategy Failure Analysis — 3-Year Backtest (2026-04-19)

Source: PHASE3_3year_partial_9of10.log (9/10 strategies; DualMomentum killed mid-run)
Engine: original growing-window (f2d29cf), strategies/supertrend.py + bear_short.py vectorized.

---

## 3-Year Results Table (all 9 completed strategies)

| Strategy | Symbol | IS Ret% | OOS Ret% | IS MaxDD% | OOS MaxDD% | IS Sharpe | OOS Sharpe | IS Trades | OOS Trades | IS Win% | OOS Win% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DCA | BTC/USDT | +25.61% | -7.15% | 9.29% | 14.97% | +1.359 | -0.831 | 186 | 52 | 97.3% | 92.3% |
| Supertrend | ETH/USDT | -58.03% | -46.07% | 59.97% | 49.94% | -1.579 | -2.777 | 281 | 95 | 31.7% | 29.5% |
| MeanReversion | ETH/USDT | -11.11% | -4.19% | 12.19% | 4.21% | -1.862 | -2.268 | 41 | 13 | 21.9% | 15.4% |
| GridTrading | SOL/USDT | +2.32% | +0.20% | 0.38% | 0.39% | +2.604 | +0.725 | 946 | 359 | 85.4% | 79.4% |
| Breakout | AVAX/USDT | -43.29% | -36.16% | 47.62% | 38.88% | -1.327 | -2.778 | 121 | 47 | 25.6% | 25.5% |
| TrendFollowing | BTC/USDT | -55.77% | -38.17% | 57.24% | 40.69% | -1.725 | -2.640 | 350 | 119 | 32.0% | 28.6% |
| BearShort | BTC/USDT | +1.26% | +0.35% | 0.24% | 0.30% | +1.248 | +1.105 | 182 | 69 | 78.6% | 76.8% |
| VWAP | ETH/USDT | +22.43% | +17.43% | 12.09% | 5.34% | +1.002 | +2.297 | 294 | 123 | 49.3% | 48.0% |
| VolatilityBreakout | BTC/USDT | -69.94% | -21.87% | 70.23% | 24.64% | -3.623 | -2.980 | 1640 | 415 | 37.4% | 41.5% |
| DualMomentum | BTC/USDT | (killed mid-IS) | — | — | — | — | — | — | — | — | — |

Standouts: VWAP only strategy with strong OOS Sharpe at 3 years (+2.297, +17.43%).
BearShort (+1.105) and GridTrading (+0.725) also OOS-positive.
DCA flips negative OOS at 3-year scale despite 92.3% win rate.

---

## Failure Analysis — 4 Catastrophic Strategies

### 1. Supertrend — ETH/USDT

| | In-Sample | Out-of-Sample |
|---|---|---|
| Trades | 281 (192L / 89W) | 95 (67L / 28W) |
| Win rate | 31.7% | 29.5% |
| Avg loss | -$91.26 (-1.64%) | -$106.93 (-2.15%) |
| Avg win | +$135.65 (+2.22%) | +$95.07 (+1.67%) |

Top 5 losses (OOS): -$271, -$246, -$208, -$202, -$179 — all `Supertrend flipped BEARISH`
Top 5 wins (OOS): +$417, +$416, +$283, +$234, +$165 — all `stop_loss`
Exit reasons (OOS): 56.8% stop_loss, 43.2% Supertrend-flip

**Root cause:** The wins come from the trailing stop (SL runs profits when price keeps rising),
and the losses come from late signal exits (waiting for Supertrend to flip often means giving
back a large chunk). OOS avg win (+$95) is now smaller than avg loss (+$107). At 29.5% win
rate, expected value per trade = 0.295×95 − 0.705×107 = -$47.5 × 95 trades = ~-$4,500.

**Fixable?** Possibly — tighter trailing stop, or filter to only trade in aligned regimes.

---

### 2. TrendFollowing — BTC/USDT

| | In-Sample | Out-of-Sample |
|---|---|---|
| Trades | 350 (238L / 111W) | 119 (85L / 34W) |
| Win rate | 32.0% | 28.6% |
| Avg loss | -$54.70 (-0.97%) | -$76.83 (-1.15%) |
| Avg win | +$70.91 (+1.33%) | +$84.75 (+1.31%) |

Top 5 losses (OOS): -$187, -$170, -$168, -$159, -$149 — EMA crossover or stop_loss
Top 5 wins (OOS): +$345, +$210, +$182, +$169, +$167 — mostly stop_loss
Exit reasons (OOS): ~52% stop_loss, ~48% EMA crossover

**Root cause:** The avg win/loss ratio is actually fine (+1.31% vs -1.15%). The problem is win
rate: 28.6% is too low. A trend-following strategy needs >40% to be profitable at this ratio.
EMA9/21 on 1h generates too many false signals in BTC's choppy regime.
Expected value: 0.286×84.75 − 0.714×76.83 = -$30.6 per trade × 119 = ~-$3,640.

**Fixable?** Possibly — longer lookback EMAs (e.g. 21/55) or require ADX confirmation.

---

### 3. Breakout — AVAX/USDT

| | In-Sample | Out-of-Sample |
|---|---|---|
| Trades | 121 (90L / 31W) | 47 (35L / 12W) |
| Win rate | 25.6% | 25.5% |
| Avg loss | -$132.00 (-2.35%) | -$148.57 (-2.12%) |
| Avg win | +$248.50 (+4.11%) | +$137.66 (+2.01%) |

Top 5 losses (OOS): -$209, -$205, -$203, -$199, -$194 — all stop_loss, clustered at ~-2.2%
Top 5 wins (OOS): +$395, +$378, +$283, +$205, +$160 — take_profit
Exit reasons (OOS): 91.5% stop_loss, 8.5% take_profit

**Root cause:** IS was working — 31 winners averaging +4.1% gain. OOS the winners dried up:
avg win collapsed from +$248 (IS) to +$138 (OOS), barely exceeding avg loss. The breakout
signals mostly resolve as fakeouts on AVAX — 91.5% of exits are stop_loss.
Expected value: 0.255×137.66 − 0.745×148.57 = -$75.7 per trade × 47 = ~-$3,558.

**Fixable?** Hard — AVAX breakout strategy not working on this symbol/timeframe at 3 years.
IS performance looks like overfitting to a specific AVAX regime.

---

### 4. VolatilityBreakout — BTC/USDT

| | In-Sample | Out-of-Sample |
|---|---|---|
| Trades | 1,640 (1025L / 614W) | 415 (248L / 167W) |
| Win rate | 37.4% | ~40.2% |
| Avg loss | -$15.53 (-0.40%) | ~-$15 (est.) |
| Avg win | +$16.61 (+0.43%) | ~+$12 (est.) |

Top 5 losses (IS): -$194, -$191, -$183, -$178, -$143 — stop_loss or next-candle exit
Top 5 wins (IS): +$204, +$146, +$108, +$107, +$104 — all next-candle exits
Exit reasons: ~99% "VolBreakout: Next-candle exit at open=…"

**Root cause:** Trades too often (1,640 IS = ~90/month) and exits every trade at the next
candle's open regardless of profit/loss — never lets winners run. At 37% win rate with
nearly symmetric win/loss size, expected value is 0.37×16.61 − 0.63×15.53 = -$3.63 per
trade × 1,640 = -$5,950. Death by small cuts across high trade frequency.

**Fixable?** Hard — the exit structure (always 1 candle) is fundamental to the strategy design.
Would need a trailing stop or minimum hold period to capture larger moves.

---

## DCA Note (OOS concern)

DCA shows +25.61% IS but -7.15% OOS with 92.3% win rate. The high win rate with negative
return suggests most wins are small and a few large losses drag the total negative. Over 3
years, the martingale safety order sizing appears to encounter drawdowns that exceed the
strategy's recovery capacity in the OOS period. Worth monitoring vs the 3-month positive result.

---

## Strategies Worth Keeping (positive OOS Sharpe at 3 years)

| Strategy | OOS Sharpe | OOS Return | OOS MaxDD |
|---|---|---|---|
| VWAP | +2.297 | +17.43% | 5.34% |
| BearShort | +1.105 | +0.35% | 0.30% |
| GridTrading | +0.725 | +0.20% | 0.39% |

VWAP is the standout: OOS Sharpe improves from IS (+1.002 → +2.297), low drawdown (5.34%),
consistent win rate (49.3% IS / 48.0% OOS). Not overfit — OOS is better than IS.
