#!/bin/bash
# deploy/setup_server.sh
# Run this ONCE on a fresh Digital Ocean Ubuntu 22.04 droplet.
# Usage: bash setup_server.sh

set -e

echo "=== Crypto Bot — Server Setup ==="
echo "Running as: $(whoami) on $(hostname)"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/7] Updating system packages…"
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    python3-pip git curl htop screen ufw fail2ban \
    build-essential libssl-dev libffi-dev

# ── 2. Firewall ───────────────────────────────────────────────────────────────
echo "[2/7] Configuring firewall…"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
echo "Firewall: SSH allowed, all other inbound blocked."

# ── 3. Create bot user ────────────────────────────────────────────────────────
echo "[3/7] Creating bot user…"
if ! id "botuser" &>/dev/null; then
    useradd -m -s /bin/bash botuser
    echo "Created user: botuser"
else
    echo "User botuser already exists."
fi

# ── 4. Create directory structure ─────────────────────────────────────────────
echo "[4/7] Creating directory structure…"
mkdir -p /home/botuser/crypto_bot
mkdir -p /home/botuser/crypto_bot/logs
mkdir -p /home/botuser/crypto_bot/data/cache
chown -R botuser:botuser /home/botuser/crypto_bot
echo "Directories created at /home/botuser/crypto_bot"

# ── 5. Python virtual environment ─────────────────────────────────────────────
echo "[5/7] Creating Python virtual environment…"
sudo -u botuser python3.11 -m venv /home/botuser/crypto_bot/.venv
echo "Virtual environment created."

# ── 6. Install Python packages ────────────────────────────────────────────────
echo "[6/7] Installing Python packages (this takes 1-2 min)…"
sudo -u botuser /home/botuser/crypto_bot/.venv/bin/pip install --upgrade pip -q
sudo -u botuser /home/botuser/crypto_bot/.venv/bin/pip install \
    ccxt \
    pandas \
    numpy \
    ta \
    loguru \
    python-dotenv \
    pyarrow \
    fastparquet \
    requests \
    -q
echo "Python packages installed."

# ── 7. Systemd service ────────────────────────────────────────────────────────
echo "[7/7] Installing systemd service…"
cp /home/botuser/crypto_bot/deploy/cryptobot.service /etc/systemd/system/cryptobot.service
systemctl daemon-reload
systemctl enable cryptobot
echo "Service installed and enabled (not started yet)."

echo ""
echo "=== Setup complete! ==="
echo ""
echo "NEXT STEPS:"
echo "  1. Upload your code:  rsync -avz ./crypto_bot/ botuser@YOUR_IP:/home/botuser/crypto_bot/"
echo "  2. Create .env file:  cp /home/botuser/crypto_bot/.env.example /home/botuser/crypto_bot/.env"
echo "  3. Edit .env:         nano /home/botuser/crypto_bot/.env"
echo "  4. Download data:     sudo -u botuser /home/botuser/crypto_bot/.venv/bin/python -m data.historical_fetcher BTC/USDT 1h 3"
echo "  5. Start bot:         systemctl start cryptobot"
echo "  6. Check logs:        journalctl -u cryptobot -f"
echo "  7. Check status:      systemctl status cryptobot"
