"""Interactive go-live checklist — validates everything before enabling live trading."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings, TIER_LIMITS


def check(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    mark = "  [+]" if passed else "  [X]"
    print(f"{mark} {name}: {detail}")
    return passed


async def run_checks():
    print("=" * 55)
    print("  KALSHI BOT — GO LIVE CHECKLIST")
    print("=" * 55)

    all_ok = True

    # 1. Settings validation
    print("\n--- Configuration ---")
    all_ok &= check("Trading mode", settings.trading_mode in ("cautious", "normal"),
                     f"{settings.trading_mode} (set TRADING_MODE in .env)")
    all_ok &= check("API key configured", bool(settings.kalshi_api_key_id),
                     settings.kalshi_api_key_id[:8] + "..." if settings.kalshi_api_key_id else "MISSING")
    all_ok &= check("Private key exists", Path(settings.kalshi_private_key_path).exists(),
                     settings.kalshi_private_key_path)
    all_ok &= check("FRED API key", bool(settings.fred_api_key),
                     "configured" if settings.fred_api_key else "MISSING (macro signals disabled)")

    # 2. Telegram
    print("\n--- Notifications ---")
    has_telegram = bool(settings.telegram_bot_token and settings.telegram_chat_id)
    all_ok &= check("Telegram bot token", has_telegram,
                     "configured" if has_telegram else "MISSING — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    if has_telegram:
        from monitoring.telegram import send_message
        sent = await send_message("Go-live checklist: Telegram test message")
        all_ok &= check("Telegram send test", sent, "sent" if sent else "FAILED")

    # 3. Kalshi API connectivity
    print("\n--- Kalshi API ---")
    from data.kalshi_client import KalshiClient
    kalshi = KalshiClient()

    try:
        balance = await kalshi.get_balance()
        all_ok &= check("API connectivity", True, f"balance: ${balance:.2f}")
        all_ok &= check("Sufficient balance", balance >= 10.0,
                         f"${balance:.2f}" + (" (low!)" if balance < 50 else ""))
    except Exception as e:
        all_ok &= check("API connectivity", False, str(e))
        balance = 0

    is_prod = "demo" not in settings.effective_base_url
    all_ok &= check("Production API", is_prod,
                     settings.effective_base_url[:50])

    try:
        positions = await kalshi.get_positions()
        check("Positions API", True, f"{len(positions)} open positions")
    except Exception as e:
        all_ok &= check("Positions API", False, str(e))

    # 4. Market discovery
    print("\n--- Market Discovery ---")
    from config.economic_calendar import get_upcoming_events, get_next_event
    upcoming = get_upcoming_events(within_days=14)
    check("Upcoming events", True, f"{len(upcoming)} events in next 14 days")
    for ev in upcoming[:5]:
        days = (ev.date - __import__('datetime').date.today()).days
        print(f"      {ev.name}: {ev.date} ({days}d)")

    next_ev = get_next_event()
    if next_ev:
        try:
            markets = await kalshi.get_economic_markets(next_ev.event_type, next_ev.series_prefix)
            check("Market discovery", len(markets) > 0,
                  f"{len(markets)} markets for {next_ev.name}")
        except Exception as e:
            check("Market discovery", False, str(e))

    # 5. Risk limits
    print("\n--- Risk Limits ---")
    tier = settings.trading_mode
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["paper"])
    print(f"      Mode: {tier}")
    print(f"      Max contracts/market: {limits[0]}")
    print(f"      Max daily loss: ${limits[1]}")
    print(f"      Max portfolio exposure: ${limits[2]}")

    # 6. Kill switch
    print("\n--- Safety ---")
    kill_active = Path(settings.kill_switch_path).exists()
    check("Kill switch", not kill_active,
          "ACTIVE (remove to proceed)" if kill_active else "not active")

    confirm_path = Path(".live_confirmed")
    check(".live_confirmed file", confirm_path.exists() or not settings.is_live,
          "exists" if confirm_path.exists() else "not yet created")

    await kalshi.close()

    # Summary
    print("\n" + "=" * 55)
    if all_ok:
        print("  ALL CHECKS PASSED")
        if not confirm_path.exists() and settings.is_live:
            print("\n  To enable live trading, run:")
            print("    touch .live_confirmed")
            print("    sudo systemctl restart kalshi-bot")
            response = input("\n  Create .live_confirmed now? (yes/no): ")
            if response.strip().lower() == "yes":
                confirm_path.touch()
                print("  .live_confirmed created. Restart the bot service to go live.")
    else:
        print("  SOME CHECKS FAILED — fix issues above before going live")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(run_checks())
