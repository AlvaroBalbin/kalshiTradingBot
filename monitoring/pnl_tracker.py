"""P&L tracking and position monitoring."""

from datetime import date

import structlog

from data.kalshi_client import KalshiClient
from db.database import get_open_positions, get_todays_trades

log = structlog.get_logger()


async def get_portfolio_summary(kalshi: KalshiClient) -> dict:
    """Get current portfolio summary."""
    balance = await kalshi.get_balance()
    positions = await kalshi.get_positions()
    todays_trades = await get_todays_trades()

    realized_pnl = sum(t.get("pnl", 0) or 0 for t in todays_trades)
    fees_today = sum(t.get("fees", 0) or 0 for t in todays_trades)

    summary = {
        "balance": balance,
        "num_positions": len(positions),
        "positions": positions,
        "todays_trades": len(todays_trades),
        "realized_pnl_today": realized_pnl,
        "fees_today": fees_today,
        "net_pnl_today": realized_pnl - fees_today,
    }

    log.info("portfolio_summary",
             balance=f"${balance:.2f}",
             positions=len(positions),
             trades_today=len(todays_trades),
             pnl_today=f"${realized_pnl - fees_today:.2f}")

    return summary


async def log_position_details(kalshi: KalshiClient):
    """Log detailed position information."""
    positions = await kalshi.get_positions()
    if not positions:
        log.info("no_open_positions")
        return

    for pos in positions:
        ticker = pos.get("ticker", "unknown")
        quantity = pos.get("total_traded", 0)
        market_exposure = pos.get("market_exposure", 0)
        realized = pos.get("realized_pnl", 0)

        log.info("position",
                 ticker=ticker,
                 quantity=quantity,
                 exposure=f"${market_exposure / 100:.2f}",
                 realized_pnl=f"${realized / 100:.2f}")
