# Deployment Security Guide — DigitalOcean

## ⚠️  First: Rotate Your Binance API Keys

Your current `.env` file contains real API keys. Before doing anything else,
go to Binance → API Management and **delete and regenerate** your keys.
Then put the new keys in `.env` (locally only — see below).

---

## The Right Way to Handle Keys on DigitalOcean

### What NOT to do
- ❌ Never `scp` or `rsync` your `.env` file to the server
- ❌ Never commit `.env` to git (`.gitignore` already blocks this — keep it that way)
- ❌ Never paste keys in a terminal with shell history enabled

---

## Option A: DigitalOcean Droplet (most common)

The cleanest approach is to set environment variables in the **systemd service file**,
which is only readable by root and your service user.

### Step 1 — Create a service user (never run as root)
```bash
sudo adduser --system --no-create-home --group cryptobot
```

### Step 2 — Create a systemd service with keys as env vars
```bash
sudo nano /etc/systemd/system/cryptobot.service
```

Paste:
```ini
[Unit]
Description=Crypto Trading Bot
After=network.target

[Service]
Type=simple
User=cryptobot
WorkingDirectory=/home/your_user/crypto_bot
ExecStart=/home/your_user/crypto_bot/.venv/bin/python main.py --portfolio --loop
Restart=on-failure
RestartSec=30s

# ── Secrets injected here — file is root-readable only ──
Environment="BINANCE_API_KEY=your_new_key_here"
Environment="BINANCE_API_SECRET=your_new_secret_here"
Environment="TRADING_MODE=paper"
Environment="TRADING_PAIR=BTC/USDT"
Environment="TIMEFRAME=1h"
Environment="CANDLE_LIMIT=300"
Environment="PAPER_BALANCE=10000.0"
Environment="LOG_LEVEL=INFO"

[Install]
WantedBy=multi-user.target
```

Then lock it down:
```bash
sudo chmod 600 /etc/systemd/system/cryptobot.service   # only root can read
sudo systemctl daemon-reload
sudo systemctl enable cryptobot
sudo systemctl start cryptobot
```

### Step 3 — Verify it works (NO .env file on server)
```bash
sudo systemctl status cryptobot
sudo journalctl -u cryptobot -f   # live logs
```

The bot reads from `os.environ` (via `python-dotenv`'s `load_dotenv()`).
When no `.env` file exists, it reads the variables set by systemd — the keys
never touch the filesystem as plaintext outside that service file.

---

## Option B: DigitalOcean App Platform (if you use their PaaS)

1. In the DO dashboard → your app → Settings → Environment Variables
2. Add `BINANCE_API_KEY` and `BINANCE_API_SECRET` as **Encrypted** secrets
3. DO injects them at runtime — they are never stored on disk

---

## Option C: pm2 (if you use pm2 instead of systemd)

```bash
# Create ecosystem.config.js — chmod 600 it after
nano ecosystem.config.js
```

```js
module.exports = {
  apps: [{
    name: 'cryptobot',
    script: '.venv/bin/python',
    args: 'main.py --portfolio --loop',
    env: {
      BINANCE_API_KEY: 'your_new_key_here',
      BINANCE_API_SECRET: 'your_new_secret_here',
      TRADING_MODE: 'paper',
    }
  }]
}
```

```bash
chmod 600 ecosystem.config.js   # IMPORTANT
pm2 start ecosystem.config.js
pm2 save
pm2 startup   # run the command it prints to survive reboot
```

---

## Binance API Key Hardening (Most Important Step)

Even with keys stored securely, if they leak you want blast radius to be minimal.

1. **IP whitelist** — In Binance API Management, restrict the key to your
   server's IP address. A stolen key is useless without that IP.
   ```
   Binance → API Management → Edit → Restrict access to trusted IPs only
   Add your Droplet's IP: xxx.xxx.xxx.xxx
   ```

2. **Permissions** — For paper trading, enable **Read Info only**.
   For live trading, enable **Spot & Margin Trading**. NEVER enable Withdrawals.

3. **Separate keys** — Use a different API key for paper vs live. Never use
   your live key for testing.

---

## Local Development (Mac)

Keep using `.env` locally — it's `.gitignore`'d so it's safe as long as you
don't accidentally `git add -f .env` or use a shared machine.

Check that it's ignored:
```bash
cd crypto_bot
git check-ignore -v .env   # should print: .gitignore:2:.env    .env
```

---

## Summary Checklist

- [ ] Rotate Binance API keys (old ones were exposed in a session)
- [ ] IP-restrict the new key to your server's IP on Binance
- [ ] Use systemd `Environment=` or pm2 with `chmod 600` — NOT `.env` on server
- [ ] Never `scp .env` to the server
- [ ] Keep paper and live API keys separate
- [ ] Read-only permissions for paper trading keys
