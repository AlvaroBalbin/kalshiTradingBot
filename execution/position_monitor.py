"""Position exit monitoring — watches open positions for profit targets and stop losses."""

import structlog

from config.settings import settings
from data.kalshi_client import KalshiClient
from db.database import get_open_positions, update_trade_status
from monitoring.alerts import alert_exit, alert_error

log = structlog.get_logger()


async def check_exits(kalshi: KalshiClient) -> list[dict]:
    """Check all open positions against profit target and stop loss.

    Returns list of exit results.
    """
    open_trades = await get_open_positions()
    if not open_trades:
        return []

    exits = []
    for trade in open_trades:
        ticker = trade.get("market_ticker", "")
        side = trade.get("side", "yes")
        entry_price = trade.get("fill_price") or trade.get("price", 0)
        quantity = trade.get("quantity", 0)
        trade_id = trade.get("id")

        if not ticker or not entry_price or not trade_id:
            continue

        # Get current market price
        try:
            market = await kalshi.get_market(ticker)
        except Exception as e:
            log.warning("exit_check_market_failed", ticker=ticker, error=str(e))
            continue

        status = market.get("status", "")

        # Handle settled markets
        if status in ("settled", "finalized"):
            result_str = market.get("result", "")
            if result_str == "yes":
                settle_price = 1.0
            elif result_str == "no":
                settle_price = 0.0
            else:
                continue

            if side == "yes":
                pnl_per = settle_price - entry_price
            else:
                pnl_per = (1.0 - settle_price) - entry_price

            gross_pnl = pnl_per * quantity
            fees = max(0, gross_pnl) * settings.kalshi_fee_rate
            net_pnl = gross_pnl - fees

            await update_trade_status(trade_id, "settled",
                                      fill_price=entry_price,
                                      pnl=net_pnl, fees=fees)
            log.info("position_settled", ticker=ticker, side=side,
                     pnl=round(net_pnl, 2), result=result_str)
            alert_exit(ticker, f"settled ({result_str})", net_pnl)
            exits.append({"ticker": ticker, "action": "settled",
                          "pnl": net_pnl, "result": result_str})
            continue

        # Get current price for open markets
        if side == "yes":
            current_price = market.get("yes_bid", 0) / 100
        else:
            current_price = market.get("no_bid", 0) / 100

        if current_price <= 0:
            continue

        # Calculate unrealized P&L in cents
        unrealized_cents = (current_price - entry_price) * 100

        # Check profit target
        if unrealized_cents >= settings.profit_target_cents:
            log.info("profit_target_hit", ticker=ticker, side=side,
                     entry=entry_price, current=current_price,
                     unrealized_cents=round(unrealized_cents, 1))

            result = await _exit_position(kalshi, trade, current_price)
            if result:
                exits.append(result)
            continue

        # Check stop loss
        if unrealized_cents <= -settings.stop_loss_cents:
            log.warning("stop_loss_hit", ticker=ticker, side=side,
                        entry=entry_price, current=current_price,
                        unrealized_cents=round(unrealized_cents, 1))

            result = await _exit_position(kalshi, trade, current_price)
            if result:
                exits.append(result)
            continue

        log.debug("position_open", ticker=ticker, side=side,
                  entry=round(entry_price, 2), current=round(current_price, 2),
                  unrealized_cents=round(unrealized_cents, 1))

    return exits


async def _exit_position(kalshi: KalshiClient, trade: dict,
                         exit_price: float) -> dict | None:
    """Exit a position by selling."""
    ticker = trade.get("market_ticker", "")
    side = trade.get("side", "yes")
    quantity = trade.get("quantity", 0)
    entry_price = trade.get("fill_price") or trade.get("price", 0)
    trade_id = trade.get("id")

    if settings.paper_trading:
        pnl_per = exit_price - entry_price
        gross_pnl = pnl_per * quantity
        fees = max(0, gross_pnl) * settings.kalshi_fee_rate
        net_pnl = gross_pnl - fees

        await update_trade_status(trade_id, "closed",
                                  fill_price=entry_price,
                                  pnl=net_pnl, fees=fees)
        action = "profit_target" if pnl_per > 0 else "stop_loss"
        log.info("PAPER_EXIT", ticker=ticker, side=side,
                 action=action, pnl=round(net_pnl, 2))
        alert_exit(ticker, f"paper_{action}", net_pnl)
        return {"ticker": ticker, "action": action, "pnl": net_pnl}

    # Real exit: sell the position
    try:
        exit_side = side  # sell same side
        price_cents = int(exit_price * 100)
        result = await kalshi.create_order(
            ticker=ticker,
            side=exit_side,
            action="sell",
            count=quantity,
            type="limit",
            yes_price=price_cents if side == "yes" else None,
            no_price=price_cents if side == "no" else None,
        )

        pnl_per = exit_price - entry_price
        gross_pnl = pnl_per * quantity
        fees = max(0, gross_pnl) * settings.kalshi_fee_rate
        net_pnl = gross_pnl - fees

        await update_trade_status(trade_id, "closed",
                                  fill_price=entry_price,
                                  pnl=net_pnl, fees=fees)

        action = "profit_target" if pnl_per > 0 else "stop_loss"
        log.info("EXIT_EXECUTED", ticker=ticker, side=side,
                 action=action, pnl=round(net_pnl, 2),
                 order_id=result.get("order_id", ""))
        alert_exit(ticker, action, net_pnl)
        return {"ticker": ticker, "action": action, "pnl": net_pnl}

    except Exception as e:
        log.error("exit_failed", ticker=ticker, error=str(e))
        alert_error("exit_position", f"{ticker}: {e}")
        return None
