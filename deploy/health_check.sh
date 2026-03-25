#!/bin/bash
# Health check script — run manually or via cron to monitor bot status
# Cron example: */30 * * * * bash /home/pi/tradingBotKalshi/deploy/health_check.sh

SERVICE="kalshi-bot"
BOT_DIR="$HOME/tradingBotKalshi"
LOG_FILE="$BOT_DIR/health.log"

timestamp=$(date '+%Y-%m-%d %H:%M:%S')

# Check if service is running
if systemctl is-active --quiet "$SERVICE"; then
    status="RUNNING"
else
    status="STOPPED"
    echo "$timestamp [ALERT] Bot is not running! Attempting restart..." >> "$LOG_FILE"
    sudo systemctl restart "$SERVICE"
    sleep 5
    if systemctl is-active --quiet "$SERVICE"; then
        echo "$timestamp [OK] Bot restarted successfully" >> "$LOG_FILE"
    else
        echo "$timestamp [CRITICAL] Bot failed to restart!" >> "$LOG_FILE"
    fi
fi

# Check database size
if [ -f "$BOT_DIR/bot.db" ]; then
    db_size=$(du -h "$BOT_DIR/bot.db" | cut -f1)
else
    db_size="MISSING"
fi

# Check disk space
disk_usage=$(df -h / | awk 'NR==2{print $5}')

# Check memory
mem_usage=$(free | awk 'NR==2{printf "%.0f%%", $3*100/$2}')

# Count today's trades
if [ -f "$BOT_DIR/bot.db" ]; then
    trades_today=$(sqlite3 "$BOT_DIR/bot.db" "SELECT COUNT(*) FROM trades WHERE date(timestamp)=date('now')" 2>/dev/null || echo "?")
    signals_today=$(sqlite3 "$BOT_DIR/bot.db" "SELECT COUNT(*) FROM signals WHERE date(timestamp)=date('now')" 2>/dev/null || echo "?")
else
    trades_today="?"
    signals_today="?"
fi

echo "$timestamp [HEALTH] Status=$status DB=$db_size Disk=$disk_usage Mem=$mem_usage Trades=$trades_today Signals=$signals_today" >> "$LOG_FILE"

# Print summary
echo "Bot Status: $status"
echo "Database:   $db_size"
echo "Disk:       $disk_usage"
echo "Memory:     $mem_usage"
echo "Trades today:  $trades_today"
echo "Signals today: $signals_today"
