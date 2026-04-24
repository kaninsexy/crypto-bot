# Deployment Security — OKX + DigitalOcean

Current server: `kanin@104.248.145.189` (DigitalOcean Singapore).
Current exchange: OKX (migrated from Binance).
Current mode: paper trading.

## Exchange API Key Handling

### Never do

- Never `scp` or `rsync` `.env` to the server
- Never commit `.env` to git (`.gitignore` blocks this — verify periodically)
- Never paste keys in a terminal with shell history enabled

### Do — systemd environment injection

The bot runs under systemd (`cryptobot.service`). Secrets injected via `Environment=` directives in the service file, which is readable only by root. The bot reads from `os.environ`; no `.env` file should exist on the server.

Service file location: `/etc/systemd/system/cryptobot.service`
Ensure `chmod 600` — only root can read.

Required environment variables:

- `OKX_API_KEY`
- `OKX_API_SECRET`
- `OKX_API_PASSPHRASE` (OKX requires this, Binance did not)
- `TRADING_MODE=paper` (must be `paper` until Phase 5 live decision)
- Other config values from `.env.example`

### Current server hardening status (from old MASTER_PLAN completed features)

- Non-root user (`kanin`) for bot execution
- SSH access locked down
- Dashboard port 8080 IP-restricted
- Dashboard runs under gunicorn (not Flask dev server)

## OKX API Key Hardening

1. **IP whitelist.** In OKX API Management, restrict the key to the DigitalOcean server's IP (`104.248.145.189`). A stolen key is useless without that IP.
2. **Permissions.**
   - Paper trading: `Read` only — NEVER enable Trade or Withdraw for paper keys
   - Live trading (future): `Read` + `Trade`, NEVER `Withdraw`
   - Withdrawal permission is never needed by the bot for any mode
3. **Separate keys per mode.** Different API key for paper vs live when the time comes. Never use live keys for testing.
4. **Passphrase.** Unlike Binance, OKX requires a passphrase at API key creation. Store it as `OKX_API_PASSPHRASE` environment variable alongside key/secret. Same rules apply — do not commit, do not `scp`.

## Local Development (Mac)

`.env` stays local. Verify `.gitignore` is working:

```bash
cd ~/Documents/crypto-bot
git check-ignore -v .env
# Should output: .gitignore:<line>:.env    .env
```

If the above prints nothing, `.env` is NOT ignored. Fix `.gitignore` before continuing.

## Deploy Procedure

Standard deploy flow:

```bash
# On Mac
git push origin main

# SSH to server
ssh kanin@104.248.145.189

# On server
sudo bash -c "cd /home/botuser/crypto_bot && git pull"
sudo systemctl restart cryptobot
sudo systemctl restart cryptodashboard

# Verify both services healthy
sudo systemctl status cryptobot
sudo systemctl status cryptodashboard

# Tail logs for at least 2 minutes to confirm startup is clean
sudo journalctl -u cryptobot -f
```

Deploy gates:

- Nothing deploys to paper until it has passed CPCV + DSR on holdout (per `validation_framework.md`)
- Paper mode is the guard — `TRADING_MODE=live` requires explicit Phase 5 decision with human sign-off
- Any code path that would trigger real OKX execution calls must be gated by `paper_mode=True` checks

## Key Rotation

Rotate OKX API keys:

- After any suspected leak
- When moving from paper to live mode (new key, new IP whitelist)
- Periodically (recommended: every 6 months as hygiene)

## Summary checklist (before any deploy)

- [ ] `.env` not present on server (verify: `ssh server 'ls /home/botuser/crypto_bot/.env'` returns "No such file")
- [ ] Environment variables present in systemd service file (`chmod 600`)
- [ ] OKX API key IP-whitelisted to server's IP
- [ ] API key has correct permissions (paper: Read only; live: Read+Trade, never Withdraw)
- [ ] `TRADING_MODE=paper` until Phase 5 decision
- [ ] Dashboard port still IP-restricted
- [ ] `.gitignore` still blocks `.env` (verify with `git check-ignore`)
