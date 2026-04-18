# Bot Status — Updated 2026-04-17

## Current State
- Exchange: OKX (migrated from Binance — geo-blocked Apr 17)
- Server: kanin@104.248.145.189
- Status: RUNNING, paper trading, fresh $100k restart
- Equity: $100,000 (clean restart Apr 17 2026)
- All positions: FLAT (clean restart)

## Key Files
- Checkpoint: /home/botuser/crypto_bot/dashboard/data/portfolio_checkpoint.json
- Paper state: /home/botuser/crypto_bot/dashboard/data/paper_state.json
- Bot logs: sudo journalctl -u cryptobot --no-pager

## Server Commands
SSH: ssh kanin@104.248.145.189
Git pull: sudo bash -c "cd /home/botuser/crypto_bot && git pull"
Restart: sudo systemctl restart cryptobot cryptodashboard
Logs: sudo journalctl -u cryptobot -n 50 --no-pager
Checkpoint edit: sudo python3 -c "..."

## Recent Commits (as of Apr 17)
- OKX migration (exchange.py, data_fetcher.py, config.py)
- Startup sequence fix (circuit breaker guard on $0 equity)
- Rebalancer: protect open positions + 25% drift threshold
- CapGuard: exclude 0%-allocation from warnings
- LeverageGuard: correct Binance formula, hourly summary
- CorrCap proportional scaling post-Kelly
- DCA capital scaling (1% of slot capital)
- Minimum capital guard ($200 floor)
- MeanReversion regime allocation + DCA MACD filter
- ATR trailing stops (4 strategies)
- Graduated daily loss caps
- GridTrading SL circuit breaker
- Exchange reconciliation (Q2)

## Known Issues
1. Rebalancer conflates capital vs profit (needs redesign)
2. Kelly sizing rarely fires (low recommended_kelly)
3. Kelly not regime-aware
4. No backtesting infrastructure
5. OKX transfer functions raise NotImplementedError
6. Rolling Sharpe not tracked
