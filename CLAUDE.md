# Crypto Bot — Project Context for Claude

This file gives Claude instant context when opening this project in a new session.
Read this fully before doing anything else.

---

## What This Is

A **6-strategy automated crypto trading bot** built for Binance, written in Python.
Owner: Kanin (kanin.srij@gmail.com) — non-developer, so explain concepts clearly with examples.

The bot runs in **paper mode** (simulated trades, no real money) by default.
When ready, switching to live mode requires one `.env` change (`TRADING_MODE=live`).

---

## Project Status: COMPLETE (all phases built and bug-fixed)

| Phase | What | Status |
|-------|------|--------|
| A | 6 trading strategies | ✅ Done |
| B | Paper trading simulator | ✅ Done |
| C | Backtesting engine | ✅ Done |
| D | Portfolio Manager + Regime Detector | ✅ Done |
| E | Kelly Criterion sizing + Circuit Breaker | ✅ Done |
| — | Full codebase audit + all bugs fixed | ✅ Done |
| — | macOS setup scripts | ✅ Done |

---

## The 6 Strategies

| Strategy | File | What it does |
|----------|------|--------------|
| DCA | `strategies/dca.py` | Dollar-cost averaging with martingale safety orders, tranche exits (30/30/40%), trailing TP, panic protection |
| Supertrend | `strategies/supertrend.py` | ATR-based trend-following with BTC market filter and dynamic trailing stop |
| MeanReversion | `strategies/mean_reversion.py` | RSI + Bollinger Bands — buys oversold, sells overbought |
| GridTrading | `strategies/grid_trading.py` | Adaptive grid with BB range + ATR step size, pauses in trending markets |
| Breakout | `strategies/breakout.py` | Volume-confirmed S/R breakout with 4h MTF confirmation and sentiment sizing |
| TrendFollowing | `strategies/trend_following.py` | EMA crossover (9/21) confirmed by MACD histogram |

---

## Architecture

```
PortfolioManager  (portfolio/manager.py)
  ├── RegimeDetector        — detects BULL/RANGE/BEAR/CRASH from BTC data
  ├── KellyCalculator       — sizes trades from Phase C OOS backtest results
  ├── CircuitBreaker        — trips at -30% drawdown, 24-candle lockout
  └── StrategySlot × 6     — each strategy + isolated PaperTrading simulator
```

### Market Regime Detection (`portfolio/regime_detector.py`)
6 regimes: `STRONG_BULL`, `BULL`, `RANGE`, `VOLATILE`, `BEAR`, `CRASH`
Detection uses: EMA50 vs EMA200 (trend), RSI (momentum), ATR% (volatility), drawdown from 50-candle high.
3-candle hysteresis prevents flip-flopping.
Each regime has a weight allocation dict in `REGIME_ALLOCATIONS` (all sum to 1.0).
Cash reserve (0–15%) held back in BEAR/CRASH via `REGIME_CASH_RESERVE`.

### Kelly Criterion (`portfolio/kelly.py`)
Formula: `f* = (p×b − q) / b` where p=win rate, b=avg_win/avg_loss
Uses half-Kelly by default. Quarter-Kelly for <20 OOS trades.
Hard cap: 35% per strategy.
`PHASE_C_PROFILES` dict holds the seeded OOS backtest results — update with real Binance data from `backtest/runner.py` when available.

### Circuit Breaker (`portfolio/circuit_breaker.py`)
States: `NORMAL` → `WARNING` (−15%, halve sizes) → `TRIPPED` (−30%, block all buys) → `RESETTING` → `NORMAL`
Peak equity only updates in NORMAL/WARNING states.

### Paper Trading Simulator (`paper_trading/simulator.py`)
Supports: DCA multi-entry, tranche exits (quantity_pct), trailing TP, panic protection (2 SL closes), time-based exit, fee simulation (0.02% limit / 0.04% market).

---

## Key Design Decisions (don't change without reason)

- **DepositManager** (`portfolio/deposit_manager.py`) is a standalone manual deposit tool — it is NOT used inside PortfolioManager. Portfolio distributes capital directly via `REGIME_ALLOCATIONS`.
- **BEAR/CRASH regime allocations** sum to 1.0 in the dict. The cash hold-back is handled separately by `REGIME_CASH_RESERVE` at portfolio level.
- **Breakout strategy** has `mtf_enabled=False` in PortfolioManager (because the portfolio loop doesn't fetch 4h data) but `mtf_enabled=True` in single-strategy `main.py` mode.
- **Binance API for wallet transfers**: Spot ↔ Futures internal transfers use `/sapi/v1/asset/transfer`. Requires only "Enable Futures" permission — NOT withdrawal permission. Functions: `transfer_spot_to_futures()` and `transfer_futures_to_spot()` in `core/exchange.py`.

---

## File Structure

```
crypto_bot/
├── CLAUDE.md                   ← You are here
├── main.py                     ← Entry point
├── config.py                   ← Loads .env settings
├── requirements.txt            ← pip dependencies (use `ta`, not `pandas-ta`)
├── .env                        ← Your secrets (never commit this)
├── .env.example                ← Template — copy to .env and fill in
├── setup_mac.sh                ← One-command macOS setup script
├── run.sh                      ← Convenience launcher
│
├── strategies/
│   ├── base.py                 ← BaseStrategy + Signal dataclass
│   ├── dca.py
│   ├── supertrend.py
│   ├── mean_reversion.py
│   ├── grid_trading.py
│   ├── breakout.py
│   └── trend_following.py
│
├── paper_trading/
│   └── simulator.py            ← PaperTrading class
│
├── portfolio/
│   ├── manager.py              ← PortfolioManager (Phase D+E entry point)
│   ├── regime_detector.py      ← RegimeDetector + REGIME_ALLOCATIONS
│   ├── kelly.py                ← KellyCalculator + PHASE_C_PROFILES
│   ├── circuit_breaker.py      ← CircuitBreaker state machine
│   └── deposit_manager.py      ← Standalone deposit tool (not used by PM)
│
├── backtest/
│   ├── engine.py               ← BacktestEngine (candle replay)
│   ├── runner.py               ← Downloads real Binance OHLCV, runs all strategies
│   ├── report.py               ← Coloured comparison table
│   └── standalone.py           ← Self-contained backtest, no API needed (numpy/pandas only)
│
├── core/
│   ├── exchange.py             ← Binance connector (ccxt) + transfer functions
│   ├── data_fetcher.py         ← fetch_ohlcv(), is_new_candle()
│   └── sentiment.py            ← LunarCrush sentiment scores
│
└── watchlist/
    └── manager.py              ← 3-tier coin rotation with 8 safeguards
```

---

## How to Run (macOS)

**First time only:**
```bash
cd ~/Desktop/ClaudeTrading/crypto_bot
./setup_mac.sh
```

**Every time after:**
```bash
cd ~/Desktop/ClaudeTrading/crypto_bot
./run.sh                          # Portfolio snapshot (paper mode)
./run.sh --loop                   # Continuous (acts on each new candle)
./run.sh backtest                 # Backtest, no API key needed
./run.sh --strategy dca           # Single strategy
```

---

## Bugs Fixed (for reference — don't reintroduce these)

1. `grid_trading.py` — f-string `${nearest_below:.2f if nearest_below else 0}` was invalid Python (crashes). Fixed to `${nearest_below if nearest_below is not None else 0:.2f}`.
2. `simulator.py` — `and/or` short-circuit anti-pattern for DCA cost calculation. Replaced with proper ternary.
3. `watchlist/manager.py` — dead variable `new_slots` (computed but never used). Removed.
4. `portfolio/manager.py` — `if price:` fails when price is 0.0. Fixed to `if price is not None:`.
5. `main.py` — both branches of if/else fetched BTC the same way (copy-paste). Collapsed to one line.
6. `deposit_manager.py` — `DEFAULT_WEIGHTS` summed to 1.05 (breakout was 0.10, should be 0.05). Fixed.
7. `portfolio/manager.py` — `DepositManager` was being instantiated internally and called with bucket key "trend" which doesn't exist. Removed DepositManager from PortfolioManager entirely.
8. `requirements.txt` — listed `pandas-ta` but code does `import ta`. Fixed to `ta>=0.11.0`.

---

## Kanin's Preferences (always follow these)

- Explain concepts thoroughly — don't assume prior knowledge. Use examples.
- Show reasoning, cover edge cases, suggest next steps.
- Be honest and direct. Push back if approaching something the wrong way.
- Main languages: Python and JavaScript/TypeScript.
- Use code blocks and structure for technical topics; prose for discussion.
- No unnecessary filler or padding in responses.

---

## What's Not Built Yet (possible future work)

- Telegram alerts (config keys exist in `.env`, sending code doesn't)
- Live execution (paper mode is fully working; switch `TRADING_MODE=live` in `.env` when ready)
- Real Binance data backtest (run `backtest/runner.py` with API key to replace synthetic GBM results in `PHASE_C_PROFILES`)
- Web dashboard / HTML performance report
