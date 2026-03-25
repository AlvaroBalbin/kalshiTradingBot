#!/bin/bash
# Raspberry Pi 5 Setup Script for Kalshi FOMC Trading Bot
# Run: bash deploy/rpi_setup.sh
set -e

BOT_DIR="$HOME/tradingBotKalshi"
SERVICE_NAME="kalshi-bot"

echo "════════════════════════════════════════════════"
echo "  Kalshi Trading Bot — Raspberry Pi 5 Setup"
echo "════════════════════════════════════════════════"

# 1. System dependencies
echo "[1/7] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv git libffi-dev libssl-dev

# Check Python version
PY_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "  Python version: $PY_VERSION"

# 2. Clone or update repo
echo "[2/7] Setting up project directory..."
if [ -d "$BOT_DIR" ]; then
    echo "  Directory exists, pulling latest..."
    cd "$BOT_DIR"
    git pull 2>/dev/null || echo "  Not a git repo, skipping pull"
else
    echo "  Creating directory..."
    mkdir -p "$BOT_DIR"
    echo "  Copy your project files to $BOT_DIR"
    echo "  Or: git clone <your-repo-url> $BOT_DIR"
fi

cd "$BOT_DIR"

# 3. Virtual environment
echo "[3/7] Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -e . -q
echo "  Dependencies installed"

# 4. Environment file
echo "[4/7] Checking .env configuration..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "  Created .env from .env.example"
        echo "  !! IMPORTANT: Edit .env with your API keys !!"
        echo "     nano $BOT_DIR/.env"
    else
        echo "  WARNING: No .env file found!"
    fi
else
    echo "  .env exists"
fi

# 5. Private key check
echo "[5/7] Checking RSA private key..."
KEY_PATH=$(grep KALSHI_PRIVATE_KEY_PATH .env 2>/dev/null | cut -d= -f2 | tr -d ' "')
KEY_PATH=${KEY_PATH:-./kalshi_private_key.pem}
if [ -f "$KEY_PATH" ]; then
    echo "  Private key found: $KEY_PATH"
    chmod 600 "$KEY_PATH"
else
    echo "  WARNING: Private key not found at $KEY_PATH"
    echo "  Download from Kalshi and place at: $BOT_DIR/$KEY_PATH"
fi

# 6. Database init
echo "[6/7] Initializing database..."
.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from db.database import init_db
init_db()
print('  Database initialized: bot.db')
"

# 7. Systemd service
echo "[7/7] Setting up systemd service..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=Kalshi FOMC Trading Bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$USER
WorkingDirectory=$BOT_DIR
Environment="PATH=$BOT_DIR/.venv/bin:/usr/bin:/bin"
Environment="PYTHONPATH=$BOT_DIR"
ExecStart=$BOT_DIR/.venv/bin/python -m scripts.run_bot
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

# Watchdog: restart if bot stops responding
WatchdogSec=600

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$BOT_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
echo "  Service file created: $SERVICE_FILE"

# Setup daily database backup cron
BACKUP_DIR="$HOME/kalshi-backups"
mkdir -p "$BACKUP_DIR"
CRON_LINE="0 2 * * * cp $BOT_DIR/bot.db $BACKUP_DIR/bot_\$(date +\\%Y\\%m\\%d).db"
(crontab -l 2>/dev/null | grep -v "kalshi.*bot.db" ; echo "$CRON_LINE") | crontab -
echo "  Daily backup cron added (2 AM)"

# Setup log rotation
sudo tee /etc/logrotate.d/kalshi-bot > /dev/null << LOGEOF
/var/log/journal/*kalshi-bot* {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
LOGEOF

echo ""
echo "════════════════════════════════════════════════"
echo "  Setup Complete!"
echo "════════════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "  1. Edit your API keys:  nano $BOT_DIR/.env"
echo "  2. Place your RSA key:  $BOT_DIR/kalshi_private_key.pem"
echo "  3. Run paper test:      cd $BOT_DIR && .venv/bin/python scripts/paper_test.py"
echo "  4. Start the bot:       sudo systemctl start $SERVICE_NAME"
echo "  5. Enable on boot:      sudo systemctl enable $SERVICE_NAME"
echo "  6. View logs:           sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "  Bot control:"
echo "    sudo systemctl start $SERVICE_NAME    # Start"
echo "    sudo systemctl stop $SERVICE_NAME     # Stop"
echo "    sudo systemctl restart $SERVICE_NAME  # Restart"
echo "    sudo systemctl status $SERVICE_NAME   # Status"
echo ""
echo "  !! Bot starts in PAPER TRADING mode by default !!"
echo "  !! Set PAPER_TRADING=false in .env for real trading !!"
