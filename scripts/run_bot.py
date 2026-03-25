"""Main entry point — starts the FOMC trading bot."""

import asyncio
import signal
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings
from config.fomc_calendar import get_next_fomc_date, days_to_next_fomc
from data.kalshi_client import KalshiClient
from db.database import init_db
from monitoring.logger import setup_logging
from monitoring.alerts import alert_bot_started, alert_fomc_approaching
from scheduler.job_scheduler import BotScheduler

import structlog

log = structlog.get_logger()


async def run():
    setup_logging()

    mode = "DEMO" if settings.use_demo else "PRODUCTION"
    alert_bot_started(mode)

    print(f"""
╔══════════════════════════════════════════════════╗
║       KALSHI FOMC TRADING BOT v0.1.0             ║
║                                                  ║
║  Mode: {mode:<10s}                              ║
║  API:  {settings.kalshi_base_url[:40]:<40s}  ║
╚══════════════════════════════════════════════════╝
    """)

    # Check configuration
    if not settings.kalshi_api_key_id:
        print("ERROR: KALSHI_API_KEY_ID not set in .env")
        print("Get your API key from: kalshi.com > Account > Profile > API Keys")
        sys.exit(1)

    if not settings.fred_api_key:
        print("WARNING: FRED_API_KEY not set — macro signals disabled")

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

    # Show FOMC info
    next_fomc = get_next_fomc_date()
    days = days_to_next_fomc()
    if next_fomc:
        print(f"  Next FOMC: {next_fomc} ({days} days away)")
        alert_fomc_approaching(days, str(next_fomc))

    # Check for Fed markets
    try:
        markets = await kalshi.get_fed_markets()
        print(f"  Fed markets found: {len(markets)}")
        for m in markets[:5]:
            ticker = m.get("ticker", "?")
            yes_bid = m.get("yes_bid", 0)
            yes_ask = m.get("yes_ask", 0)
            print(f"    {ticker}: bid={yes_bid}c ask={yes_ask}c")
    except Exception as e:
        print(f"  Warning: Could not fetch Fed markets: {e}")

    print("\n  Starting scheduler...\n")

    # Start scheduler
    bot = BotScheduler(kalshi)
    bot.start()

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
