"""Main entry point — starts the economic event trading bot."""

import asyncio
import signal
import sys
import os
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings
from config.economic_calendar import get_upcoming_events, get_next_event
from data.kalshi_client import KalshiClient
from db.database import init_db
from monitoring.logger import setup_logging
from monitoring.alerts import alert_bot_started, alert_event_approaching
from scheduler.job_scheduler import BotScheduler

import structlog

log = structlog.get_logger()


async def run():
    setup_logging()

    mode = settings.trading_mode.upper()

    print(f"""
╔══════════════════════════════════════════════════╗
║       KALSHI ECONOMIC TRADING BOT v0.2.0         ║
║                                                  ║
║  Mode: {mode:<10s}                              ║
║  API:  {settings.effective_base_url[:40]:<40s}  ║
║  Max contracts/market: {settings.effective_max_position_per_market:<3d}                      ║
║  Max daily loss: ${settings.effective_max_daily_loss:<6.0f}                        ║
║  Max exposure: ${settings.effective_max_portfolio_exposure:<8.0f}                      ║
╚══════════════════════════════════════════════════╝
    """)

    # Check configuration
    if not settings.kalshi_api_key_id:
        print("ERROR: KALSHI_API_KEY_ID not set in .env")
        print("Get your API key from: kalshi.com > Account > Profile > API Keys")
        sys.exit(1)

    if not settings.fred_api_key:
        print("WARNING: FRED_API_KEY not set — macro signals disabled")

    if not settings.telegram_bot_token:
        print("WARNING: TELEGRAM_BOT_TOKEN not set — no Telegram alerts")

    # Validate trading mode
    if settings.trading_mode not in ("paper", "cautious", "normal"):
        print(f"ERROR: Invalid TRADING_MODE={settings.trading_mode}")
        print("Valid modes: paper, cautious, normal")
        sys.exit(1)

    # Init database
    init_db()
    log.info("database_initialized")

    # Init Kalshi client
    kalshi = KalshiClient()

    # Check connectivity
    try:
        balance = await kalshi.get_balance()
        log.info("kalshi_connected", balance=f"${balance:.2f}")
        print(f"  Kalshi balance: ${balance:.2f}")
    except Exception as e:
        print(f"ERROR: Cannot connect to Kalshi API: {e}")
        print("Check your API key and private key path in .env")
        sys.exit(1)

    # --- LIVE MODE SAFETY GATES ---
    if settings.is_live:
        # Gate 1: Must be pointing at production API
        if "demo" in settings.effective_base_url:
            print("FATAL: Live mode configured but pointing at demo API!")
            sys.exit(1)

        # Gate 2: Balance check
        if balance < 10.0:
            print(f"WARNING: Low balance for live trading: ${balance:.2f}")
            print("Consider depositing more funds before going live.")

        # Gate 3: Production API validation (read-only test)
        try:
            positions = await kalshi.get_positions()
            log.info("production_api_validated", positions=len(positions))
            print(f"  Production API validated ({len(positions)} open positions)")
        except Exception as e:
            print(f"FATAL: Production API validation failed: {e}")
            sys.exit(1)

        # Gate 4: Require explicit .live_confirmed file
        confirm_path = Path(".live_confirmed")
        if not confirm_path.exists():
            print()
            print("  ╔══════════════════════════════════════════╗")
            print(f"  ║  LIVE TRADING MODE: {mode:<22s}  ║")
            print(f"  ║  Balance: ${balance:<8.2f}                     ║")
            print(f"  ║  Max daily loss: ${settings.effective_max_daily_loss:<6.0f}               ║")
            print(f"  ║  Max exposure: ${settings.effective_max_portfolio_exposure:<8.0f}             ║")
            print(f"  ║  Max contracts/market: {settings.effective_max_position_per_market:<3d}              ║")
            print("  ╠══════════════════════════════════════════╣")
            print("  ║  To confirm live trading, run:           ║")
            print("  ║  touch .live_confirmed                   ║")
            print("  ╚══════════════════════════════════════════╝")
            sys.exit(1)

        print(f"  Live trading confirmed (mode: {mode})")

    # Show upcoming economic events
    upcoming = get_upcoming_events(within_days=14)
    if upcoming:
        print(f"\n  Upcoming events ({len(upcoming)}):")
        for event in upcoming[:8]:
            days = (event.date - __import__('datetime').date.today()).days
            print(f"    {event.name}: {event.date} ({days} days)")
            alert_event_approaching(event.name, days, str(event.date))
    else:
        print("  No economic events in next 14 days")

    # Check for available markets
    next_event = get_next_event()
    if next_event:
        try:
            markets = await kalshi.get_economic_markets(
                next_event.event_type, next_event.series_prefix)
            print(f"\n  Markets for {next_event.name}: {len(markets)}")
            for m in markets[:5]:
                ticker = m.get("ticker", "?")
                yes_bid = m.get("yes_bid", 0)
                yes_ask = m.get("yes_ask", 0)
                print(f"    {ticker}: bid={yes_bid}c ask={yes_ask}c")
        except Exception as e:
            print(f"  Warning: Could not fetch markets: {e}")

    print("\n  Starting scheduler...\n")

    # Start scheduler
    bot = BotScheduler(kalshi)
    bot.start()

    # Send startup alert
    alert_bot_started(mode, balance)

    # Handle graceful shutdown
    stop_event = asyncio.Event()

    def _shutdown(signum, frame):
        log.info("shutdown_requested")
        print("\nShutting down...")
        bot.stop()
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Keep running
    await stop_event.wait()
    await kalshi.close()
    print("Bot stopped.")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
