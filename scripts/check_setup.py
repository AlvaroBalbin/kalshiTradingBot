"""Validate API keys and connectivity before running the bot."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings


def check(name: str, ok: bool, detail: str = ""):
    status = "PASS" if ok else "FAIL"
    symbol = "+" if ok else "X"
    msg = f"  [{symbol}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


async def main():
    print("\n=== Kalshi FOMC Bot — Setup Check ===\n")
    all_ok = True

    # 1. Kalshi API Key
    has_key = bool(settings.kalshi_api_key_id)
    all_ok &= check("Kalshi API Key", has_key,
                     settings.kalshi_api_key_id[:8] + "..." if has_key else "Not set - get from kalshi.com/account/profile")

    # 2. Kalshi Private Key
    from pathlib import Path
    key_path = Path(settings.kalshi_private_key_path)
    has_pem = key_path.exists()
    all_ok &= check("Kalshi Private Key", has_pem,
                     str(key_path) if has_pem else f"File not found: {key_path}")

    # 3. Kalshi API Connectivity
    if has_key and has_pem:
        try:
            from data.kalshi_client import KalshiClient
            kalshi = KalshiClient()
            balance = await kalshi.get_balance()
            check("Kalshi API Connection", True, f"Balance: ${balance:.2f}")
            await kalshi.close()
        except Exception as e:
            all_ok &= check("Kalshi API Connection", False, str(e))
    else:
        check("Kalshi API Connection", False, "Skipped — missing credentials")
        all_ok = False

    # 4. FRED API Key
    has_fred = bool(settings.fred_api_key)
    all_ok &= check("FRED API Key", has_fred,
                     "Set" if has_fred else "Not set - get from fred.stlouisfed.org (free)")

    # 5. FRED Connectivity
    if has_fred:
        try:
            from data.fred_client import fred_client
            rate = fred_client.get_current_fed_rate()
            check("FRED API Connection", rate is not None,
                  f"Current Fed Rate: {rate:.2f}%" if rate else "No data")
        except Exception as e:
            check("FRED API Connection", False, str(e))
    else:
        check("FRED API Connection", False, "Skipped — no API key")

    # 6. yfinance (FedWatch data)
    try:
        import yfinance as yf
        ticker = yf.Ticker("ZQJ26.CBT")
        hist = ticker.history(period="5d")
        has_data = not hist.empty
        check("yfinance (Fed Futures)", has_data,
              f"Latest price: {hist['Close'].iloc[-1]:.4f}" if has_data else "No data (market may be closed)")
    except Exception as e:
        check("yfinance (Fed Futures)", False, str(e))

    # 7. Database
    try:
        from db.database import init_db
        init_db()
        check("SQLite Database", True, "Initialized")
    except Exception as e:
        all_ok &= check("SQLite Database", False, str(e))

    # 8. Economic Calendar
    from config.economic_calendar import get_upcoming_events, get_next_event
    upcoming = get_upcoming_events(within_days=14)
    next_ev = get_next_event()
    check("Economic Calendar", len(upcoming) > 0,
          f"{len(upcoming)} events in 14 days, next: {next_ev.name} ({next_ev.date})" if next_ev else "No upcoming events")

    # 9. Trading mode
    check("Trading Mode", True,
          f"{settings.trading_mode.upper()} — {'no real orders' if not settings.is_live else 'LIVE TRADING'}")

    print(f"\n{'All checks passed!' if all_ok else 'Some checks failed — fix issues above.'}\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
