#!/bin/bash
# deploy/deploy.sh
# Upload your latest code to Digital Ocean and restart the bot.
# Run this from your LOCAL MACHINE (Mac/PC), not on the server.
#
# Usage:
#   bash deploy/deploy.sh YOUR_DROPLET_IP
#   bash deploy/deploy.sh 123.45.67.89

set -e

if [ -z "$1" ]; then
    echo "Usage: bash deploy/deploy.sh YOUR_DROPLET_IP"
    exit 1
fi

SERVER_IP=$1
BOT_USER="botuser"
REMOTE_DIR="/home/$BOT_USER/crypto_bot"
LOCAL_DIR="$(pwd)"   # Must be run from crypto_bot/ folder

echo "=== Deploying Crypto Bot to $SERVER_IP ==="
echo ""

# ── 1. Sync code ──────────────────────────────────────────────────────────────
echo "[1/3] Uploading code…"
rsync -avz --progress \
    --exclude='.venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='logs/' \
    --exclude='data/cache/' \
    --exclude='.env' \
    --exclude='dashboard/data/trades.db' \
    --exclude='dashboard/data/paper_state.json' \
    --exclude='dashboard/data/portfolio_checkpoint.json' \
    --exclude='dashboard/data/backtest_results.json' \
    "$LOCAL_DIR/" \
    "$BOT_USER@$SERVER_IP:$REMOTE_DIR/"
echo "Code uploaded."

# ── 2. Install/update packages ─────────────────────────────────────────────────
echo "[2/3] Updating Python packages…"
ssh "$BOT_USER@$SERVER_IP" \
    "$REMOTE_DIR/.venv/bin/pip install -r $REMOTE_DIR/requirements.txt -q"
echo "Packages updated."

# ── 3. Restart service ────────────────────────────────────────────────────────
echo "[3/3] Restarting bot service…"
ssh root@$SERVER_IP "systemctl restart cryptobot && sleep 2 && systemctl status cryptobot --no-pager"

echo ""
echo "=== Deploy complete! ==="
echo ""
echo "Monitor logs:  ssh $BOT_USER@$SERVER_IP 'journalctl -u cryptobot -f'"
echo "Check status:  ssh root@$SERVER_IP 'systemctl status cryptobot'"
echo "View equity:   ssh $BOT_USER@$SERVER_IP 'tail -50 $REMOTE_DIR/logs/bot.log'"
