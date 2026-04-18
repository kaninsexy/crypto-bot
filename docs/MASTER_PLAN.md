# MASTER PLAN — Crypto Trading Bot
# Last Updated: 2026-04-17
# Status: Paper trading Week 1, bot running on OKX

## CURRENT STATE
- Bot: Python multi-strategy, OKX USDT-M futures, paper trading
- Server: kanin@104.248.145.189 (DigitalOcean Singapore)
- Repo: kaninsexy/crypto-bot (~/Documents/crypto-bot on Mac)
- Deploy: git push Mac → ssh server → sudo bash -c "cd /home/botuser/crypto_bot && git pull" → sudo systemctl restart cryptobot cryptodashboard
- Dashboard: http://104.248.145.189:8080 (gunicorn)
- Paper capital: $100,000 (fresh restart Apr 17)

## STRATEGIES (10 active)
DCA, Supertrend, MeanReversion, GridTrading, Breakout,
TrendFollowing, BearShort, VWAP, VolatilityBreakout, DualMomentum

## REGIME ALLOCATIONS
- STRONG_BULL: Breakout(30%), Supertrend(25%), TrendFollowing(10%), DCA(15%)
- BULL: Breakout(20%), Supertrend(25%), MeanReversion(5%), TrendFollowing(10%), DCA(15%)
- RANGE: GridTrading(32%), DCA(20%), MeanReversion(10%), VWAP(10%), DualMomentum(10%)
- BEAR: DCA(56%), BearShort(15%), GridTrading(18%)
- CRASH: DCA(79%), BearShort(15%), GridTrading(6%)
- VOLATILE: DCA(45%), GridTrading(30%), Supertrend(10%), TrendFollowing(10%)

## COMPLETED FEATURES
- Graduated daily loss caps (5 tiers: WARNING/CAUTIOUS/HALT/REDUCE/EMERGENCY)
- ATR trailing stops (TrendFollowing 3x, Supertrend 3x, Breakout 3.5x, DualMomentum 2.5x)
- GridTrading consecutive SL circuit breaker
- Exchange reconciliation (Q2) — paper no-op, live 3-pass engine
- LeverageGuard: correct Binance/OKX formula (MMR=0.4%), correlated risk, regime caps
- Minimum capital guard ($200 floor, blocks BUY not SELL)
- DCA base_amount scaled to 1% of allocated capital (min $10)
- DCA MACD filter enabled
- MeanReversion activated RANGE(10%) BULL(5%)
- CorrCap proportional scaling (forward-looking)
- Rebalancer: protects open positions, 25% threshold
- Post-restore rebalance + circuit breaker guard on $0 equity
- OKX migration (from Binance, geo-blocked)
- Server hardened: non-root user (kanin), SSH locked, port 8080 IP-restricted
- Dashboard on gunicorn
- Kelly profiles built from PHASE_C_PROFILES (needs regime-aware upgrade)
- Per-strategy return % uses equity-based initial_balance

## KNOWN ISSUES TO FIX (Priority Order)
1. Rebalancer conflates capital allocation with earned profit — needs redesign
2. Kelly sizing never fires (recommended_kelly=0 for low-trade strategies)
3. Kelly not regime-aware (same profile used regardless of market state)
4. Backtesting infrastructure missing (no way to validate Kelly inputs)
5. Total capital accounting (deposits vs profits not tracked separately)
6. OKX live trading: transfer functions raise NotImplementedError
7. OKX contract sizes for live trading (uses contracts not base currency)
8. Rolling Sharpe not tracked in log_summary.py

## PHASE 2 — Architecture Fixes (Current Priority)
Build with Claude Code + Graphify

### 2a. Rebalancer Redesign ✅ COMPLETE
- Track initial_capital (deposited) separately from earned_profit
- Only rebalance initial_capital proportions
- Earned profits compound within each strategy untouched
- Reference: pysystemtrade capital allocation pattern

### 2b. Backtesting Infrastructure ✅ COMPLETE
- Fetch 3 years OKX historical data (2022-2025)
- Run all 10 strategies against historical data
- Get per-strategy, per-regime win rates for Kelly inputs
- Why 3 years: captures 2022 bear, 2023 recovery, 2024-2025 bull

### 2c. Regime-Aware Kelly Blending
- Separate Kelly profiles per strategy per regime
- Blend PHASE_C_PROFILES prior with live trade results (Bayesian)
- Rebuild every 50 candles + on regime change
- Formula: blended = (prior × n_prior + live × n_live) / (n_prior + n_live)

### 2d. Total Capital Accounting ✅ COMPLETE
- total_capital only increases on deposit() calls
- Track deposits separately from strategy returns

## PHASE 3 — Intelligence Layer (Week 2-4, after 30+ trades)

### Rolling Sharpe Tracking
sharpe = returns.rolling(30).mean() / returns.rolling(30).std() * sqrt(365*24)
Add to log_summary.py and weekly review

### Forecast Combining (replace CorrCap hard blocks)
Pattern from pysystemtrade: each strategy produces normalized signal (-20 to +20)
Portfolio combines with weights — conflicts cancel mathematically
Eliminates need for separate conflict detection system

### Profit Reserve System
Continuous formula: reserve_contribution = current_profit_pct × 0.3
Move to OKX Earn (flexible, daily interest) automatically
Withdrawal function: only from profits, never from principal

### Passivbot Evolutionary Parameter Optimization
Use genetic algorithms to optimize strategy parameters
(ATR multipliers, RSI thresholds, grid spacing) vs historical data

### Strategy Chaining (from 3commas composite bots)
One strategy's exit triggers another's entry
Implement as event hooks in portfolio/manager.py

## PHASE 4 — Alpha Expansion (Month 2)

### Funding Rate Arbitrage
- References: 50shadesofgwei/funding-rate-arbitrage, aoki-h-jp/funding-rate-arbitrage
- Start 5% allocation, paper trade 2 weeks first
- Only viable when annualized rate > 8-10%
- Monitor via OKX funding rate API

### ML Regime Detection
- Replace/supplement EMA/RSI with HMM or K-means clustering
- Reference: Sakeeb91/market-regime-detection
- Only if current detector shows misclassification patterns
- Requires 60+ days labeled regime data

### TradingView Pro ($15/month — subscribe when live)
- Webhook alerts for confluence signals
- Pine Script for rapid parameter iteration
- Screener for multi-coin regime conditions

### LLM as Signal in Regime Detector
- Pattern from OctoBot GPTEvaluator
- Claude sentiment score as one input (weight 10-15%)
- Multi-agent review: Pro/Neutral/Opposing stances (claude-trader pattern)
- Via OpenClaw's existing multi-agent setup

### Multi-Provider LLM Resilience
- Auto-fallback: Claude → GPT-4o → local model
- Pattern from LLM_trader
- Already partially handled by OpenClaw/OpenRouter

## PHASE 5 — Pre-Live Preparation (Month 2-3)

### OKX Live Trading Audit
- Implement transfer_spot_to_swap() properly
- OKX contract sizes: amount_to_contracts conversion
- Position mode: one-way vs hedge
- Rewrite reconciler for OKX position structure
- Test all order types on OKX testnet

### Crisis-Alpha Strategy
- 7-10% allocation, CRASH/HIGH_VOL only
- Liquidation cascade detection via OKX WebSocket
- Reference: kukapay/crypto-liquidations-mcp

### Pre-Live Checklist (ALL required before going live)
- [ ] 50+ closed trades per active strategy
- [ ] Rolling Sharpe > 0.8 on paper trading
- [ ] Max drawdown < 15% on paper trading
- [ ] Funding rate arb paper traded 2+ weeks
- [ ] OKX live trading audit complete
- [ ] Withdrawal + profit reserve functions built
- [ ] Regime-aware Kelly with real backtest data
- [ ] Rebalancer redesign complete

## PHASE 6 — Live Trading (Month 3+)

### Capital Flow
Bitkub (THB bank transfer → USDT) → OKX TRC20 → Trading Account

### Deployment by Capital
- <$5,000: DCA, GridTrading, TrendFollowing, VolBreakout only
- $5,000-10,000: Add Supertrend, DualMomentum, VWAP
- $10,000+: All 10 strategies
- $28,000+ (1M THB): Full deployment after 6 months proven

### Monthly: 60k THB (~$1,680) via Bitkub → OKX

### Profit Reserve (continuous formula)
reserve = profit_pct × 0.3 → OKX Earn flexible savings
Safe withdrawal: only from profits, maintain min capital per strategy

## TOOLS STACK
- Claude Code Pro ($20/month) — large refactors NOW
- Graphify (free) — token compression for Claude Code, install immediately
- OpenClaw — scheduled research, daily monitoring (running)
- OpenRouter ($20/month) — LLM routing (running)
- TradingView Pro ($15/month) — when live, not before
- OKX Earn (free) — profit reserve when profitable

## OPENCLAW RESEARCH SCHEDULE
Sunday weekly:
  openclaw agent --agent research --message "Weekly review: analyze strategies.md and WEEKLY_REVIEW.md. Which strategies underperformed and why?"

At 30 closed trades:
  openclaw agent --agent research --message "Bot has 30+ closed trades on OKX. Analyze performance by strategy and regime. Which has worst risk-adjusted return? Search GitHub for specific improvements."

At 50 closed trades:
  openclaw agent --agent research --message "50+ closed trades. Research pysystemtrade forecast combining pattern — can we replace CorrCap with this? Also check LLM-as-evaluator patterns in OctoBot/claude-trader repos."

Monthly (12th):
  openclaw agent --agent research --message "Monthly GitHub search: new multi-strategy crypto bot patterns, Kelly improvements, OKX-specific optimizations, funding rate arbitrage viability. Check freqtrade, pysystemtrade, OctoBot recent commits."

## GITHUB REFERENCES
- pysystemtrade: forecast combining, capital allocation
- claude-trader (Byte-Ventures): multi-agent signal review, 5 risk layers
- OctoBot (Drakkar-Software): LLM-as-signal, tentacles architecture
- Passivbot (enarjord): evolutionary parameter optimization
- LLM_trader (qrak): multi-provider LLM resilience, vision AI candles
- freqtrade: Edge Positioning for Kelly improvement
- Sakeeb91/market-regime-detection: HMM/K-means regime
- kukapay/crypto-liquidations-mcp: crisis-alpha liquidation detection
- 50shadesofgwei/funding-rate-arbitrage: funding arb implementation
- aoki-h-jp/funding-rate-arbitrage: funding arb reference

## INVESTMENT PLAN (separate from bot)
- IBKR cash account + Wise account (Thailand residence)
- SCB Easy: SCBS&P500E mutual fund (max 1M THB, 0.1% TER, CGT exempt)
- Target allocation: 60% VOO/VT, 15% VXUS, 10% QQQ, 10% Gold, 5% cash
- Foreign income not taxed if not remitted same year
- Emergency fund: 1,000,000 THB (hard floor, never invest)
- 1M THB in savings for bot — deploy gradually after 6 months proven
