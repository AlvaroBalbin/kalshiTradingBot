"""Risk management — circuit breakers, limits, and safety checks."""

from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from config.settings import settings
from config.economic_calendar import is_in_any_blackout, ET
from db.database import get_todays_trades, get_open_positions

log = structlog.get_logger()


def check_kill_switch() -> tuple[bool, str]:
    """Check if the emergency kill switch file exists.

    Returns (is_ok, reason). is_ok=False means STOP ALL TRADING.
    """
    kill_path = Path(settings.kill_switch_path)
    if kill_path.exists():
        return False, f"KILL SWITCH ACTIVE — remove {kill_path} to resume"
    return True, "Kill switch not active"


async def check_daily_loss_limit() -> tuple[bool, str]:
    """Check if daily loss limit has been hit.

    Uses effective (tier-aware) limit.
    """
    trades = await get_todays_trades()
    realized_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
    fees = sum(t.get("fees", 0) or 0 for t in trades)
    net_pnl = realized_pnl - fees

    limit = settings.effective_max_daily_loss
    if net_pnl <= -limit:
        msg = f"Daily loss limit hit: ${net_pnl:.2f} (limit: -${limit})"
        log.warning("circuit_breaker_daily_loss", pnl=net_pnl, limit=limit)
        return False, msg

    return True, f"Daily P&L: ${net_pnl:.2f} (limit: -${limit})"


async def check_exposure_limit() -> tuple[bool, str]:
    """Check if total portfolio exposure is within tier-aware limits."""
    positions = await get_open_positions()
    total_exposure = sum(
        (p.get("quantity", 0) or 0) * (p.get("price", 0) or 0)
        for p in positions
    )

    limit = settings.effective_max_portfolio_exposure
    if total_exposure >= limit:
        msg = f"Exposure limit: ${total_exposure:.2f} (limit: ${limit})"
        log.warning("exposure_limit_reached", exposure=total_exposure, limit=limit)
        return False, msg

    remaining = limit - total_exposure
    return True, f"Exposure: ${total_exposure:.2f}, remaining: ${remaining:.2f}"


def check_blackout() -> tuple[bool, str]:
    """Check if we're in any economic event blackout window."""
    blacked, reason = is_in_any_blackout()
    if blacked:
        return False, f"In blackout: {reason}"
    return True, "Not in blackout"


def check_trading_mode() -> tuple[bool, str]:
    """Check and log the current trading mode."""
    mode = settings.trading_mode
    if mode == "paper":
        return True, "Paper trading mode — no real money at risk"
    elif mode == "cautious":
        return True, (f"CAUTIOUS mode — real money | "
                      f"Max {settings.effective_max_position_per_market} contracts, "
                      f"${settings.effective_max_portfolio_exposure} exposure, "
                      f"${settings.effective_max_daily_loss} daily loss")
    elif mode == "normal":
        log.warning("normal_mode_active")
        return True, (f"NORMAL mode — real money | "
                      f"Max {settings.effective_max_position_per_market} contracts, "
                      f"${settings.effective_max_portfolio_exposure} exposure, "
                      f"${settings.effective_max_daily_loss} daily loss")
    else:
        return False, f"Unknown trading mode: {mode}"


async def check_balance(kalshi) -> tuple[bool, str]:
    """Verify Kalshi account has sufficient balance for trading."""
    if not settings.is_live:
        return True, "Paper mode — balance check skipped"

    try:
        balance = await kalshi.get_balance()
        if balance < 1.0:
            return False, f"Insufficient balance: ${balance:.2f} (min: $1.00)"
        return True, f"Balance: ${balance:.2f}"
    except Exception as e:
        log.error("balance_check_failed", error=str(e))
        return False, f"Balance check failed: {e}"


def check_orderbook_liquidity(orderbook: dict, max_spread: float = 0.10) -> tuple[bool, str]:
    """Check if orderbook has sufficient liquidity."""
    yes_bids = orderbook.get("yes", [])
    no_bids = orderbook.get("no", [])

    if not yes_bids and not no_bids:
        return False, "Empty orderbook — no liquidity"

    best_yes_bid = 0
    best_yes_ask = 100

    if yes_bids:
        best_yes_bid = max(int(b[0]) for b in yes_bids) if yes_bids else 0
    if no_bids:
        best_yes_ask = min(100 - int(b[0]) for b in no_bids) if no_bids else 100

    spread = (best_yes_ask - best_yes_bid) / 100
    if spread > max_spread:
        return False, f"Spread too wide: {spread:.1%} (max: {max_spread:.1%})"

    return True, f"Spread: {spread:.1%}"


async def pre_trade_checks(kalshi=None, orderbook: dict | None = None) -> tuple[bool, list[str]]:
    """Run all pre-trade risk checks.

    Returns (all_passed, list_of_messages).
    """
    results = []
    all_ok = True

    # 1. Kill switch (FIRST — overrides everything)
    ok, msg = check_kill_switch()
    if not ok:
        all_ok = False
    results.append(msg)
    if not ok:
        return all_ok, results  # Abort immediately

    # 2. Trading mode check
    ok, msg = check_trading_mode()
    if not ok:
        all_ok = False
    results.append(msg)

    # 3. Blackout check
    ok, msg = check_blackout()
    if not ok:
        all_ok = False
    results.append(msg)

    # 4. Daily loss limit
    ok, msg = await check_daily_loss_limit()
    if not ok:
        all_ok = False
    results.append(msg)

    # 5. Exposure limit
    ok, msg = await check_exposure_limit()
    if not ok:
        all_ok = False
    results.append(msg)

    # 6. Balance check (live mode only)
    if kalshi is not None and settings.is_live:
        ok, msg = await check_balance(kalshi)
        if not ok:
            all_ok = False
        results.append(msg)

    # 7. Liquidity (if orderbook provided)
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
