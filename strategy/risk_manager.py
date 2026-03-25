"""Risk management — circuit breakers, limits, and safety checks."""

from datetime import datetime, date
from zoneinfo import ZoneInfo

import structlog

from config.settings import settings
from config.fomc_calendar import is_in_blackout, is_fomc_day, ET
from db.database import get_todays_trades, get_open_positions

log = structlog.get_logger()


async def check_daily_loss_limit() -> tuple[bool, str]:
    """Check if daily loss limit has been hit.

    Returns (is_ok, reason).
    """
    trades = await get_todays_trades()
    realized_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
    fees = sum(t.get("fees", 0) or 0 for t in trades)
    net_pnl = realized_pnl - fees

    if net_pnl <= -settings.max_daily_loss:
        msg = f"Daily loss limit hit: ${net_pnl:.2f} (limit: -${settings.max_daily_loss})"
        log.warning("circuit_breaker_daily_loss", pnl=net_pnl)
        return False, msg

    return True, f"Daily P&L: ${net_pnl:.2f}"


async def check_exposure_limit() -> tuple[bool, str]:
    """Check if total portfolio exposure is within limits."""
    positions = await get_open_positions()
    total_exposure = sum(
        (p.get("quantity", 0) or 0) * (p.get("price", 0) or 0)
        for p in positions
    )

    if total_exposure >= settings.max_portfolio_exposure:
        msg = f"Exposure limit: ${total_exposure:.2f} (limit: ${settings.max_portfolio_exposure})"
        log.warning("exposure_limit_reached", exposure=total_exposure)
        return False, msg

    remaining = settings.max_portfolio_exposure - total_exposure
    return True, f"Exposure: ${total_exposure:.2f}, remaining: ${remaining:.2f}"


def check_blackout() -> tuple[bool, str]:
    """Check if we're in FOMC announcement blackout window."""
    if is_in_blackout():
        return False, "In FOMC announcement blackout window — no trading"
    return True, "Not in blackout"


def check_demo_mode() -> tuple[bool, str]:
    """Warn if trading on production (not demo)."""
    if not settings.use_demo:
        log.warning("production_mode_active")
        return True, "WARNING: Production mode — real money at risk"
    return True, "Demo mode — safe"


def check_orderbook_liquidity(orderbook: dict, max_spread: float = 0.10) -> tuple[bool, str]:
    """Check if orderbook has sufficient liquidity.

    Args:
        orderbook: Kalshi orderbook response
        max_spread: Maximum acceptable bid-ask spread (0-1 scale)
    """
    yes_bids = orderbook.get("yes", [])
    no_bids = orderbook.get("no", [])

    if not yes_bids and not no_bids:
        return False, "Empty orderbook — no liquidity"

    # Get best bid/ask from orderbook
    # Orderbook format may vary; handle common formats
    best_yes_bid = 0
    best_yes_ask = 100

    if yes_bids:
        best_yes_bid = max(int(b[0]) for b in yes_bids) if yes_bids else 0
    if no_bids:
        # no_bid at X means yes_ask at (100-X)
        best_yes_ask = min(100 - int(b[0]) for b in no_bids) if no_bids else 100

    spread = (best_yes_ask - best_yes_bid) / 100
    if spread > max_spread:
        return False, f"Spread too wide: {spread:.1%} (max: {max_spread:.1%})"

    return True, f"Spread: {spread:.1%}"


async def pre_trade_checks(orderbook: dict | None = None) -> tuple[bool, list[str]]:
    """Run all pre-trade risk checks.

    Returns (all_passed, list_of_messages).
    """
    results = []
    all_ok = True

    # 1. Demo mode check
    ok, msg = check_demo_mode()
    results.append(msg)

    # 2. Blackout check
    ok, msg = check_blackout()
    if not ok:
        all_ok = False
    results.append(msg)

    # 3. Daily loss limit
    ok, msg = await check_daily_loss_limit()
    if not ok:
        all_ok = False
    results.append(msg)

    # 4. Exposure limit
    ok, msg = await check_exposure_limit()
    if not ok:
        all_ok = False
    results.append(msg)

    # 5. Liquidity (if orderbook provided)
    if orderbook is not None:
        ok, msg = check_orderbook_liquidity(orderbook)
        if not ok:
            all_ok = False
        results.append(msg)

    if not all_ok:
        log.warning("pre_trade_checks_failed", reasons=results)
    else:
        log.info("pre_trade_checks_passed")

    return all_ok, results
