"""Order execution — places and tracks orders on Kalshi, or simulates in paper mode."""

import asyncio
from pathlib import Path

import structlog

from config.settings import settings
from data.kalshi_client import KalshiClient
from strategy.trade_decision import TradeOrder
from db.database import insert_signal, insert_trade, update_trade_status
from monitoring.alerts import alert_error

log = structlog.get_logger()

ORDER_TIMEOUT_SECONDS = 300


async def execute_order(kalshi: KalshiClient, order: TradeOrder) -> dict | None:
    """Execute a single trade order — real or paper."""
    signal = order.signal

    # Log signal to DB
    signal_id = await insert_signal(
        market_ticker=signal.market_ticker,
        direction=signal.direction,
        confidence=signal.confidence,
        edge_estimate=signal.edge_estimate,
        fedwatch_prob=signal.fedwatch_prob,
        kalshi_price=signal.kalshi_price,
    )

    # Paper trading mode — log but don't place
    if settings.paper_trading:
        log.info("PAPER_TRADE",
                 ticker=order.market_ticker, side=order.side,
                 count=order.count, price=order.price,
                 edge=round(signal.edge_estimate, 3),
                 confidence=signal.confidence,
                 sources=getattr(signal, 'data_sources', []))

        trade_id = await insert_trade(
            signal_id=signal_id,
            market_ticker=order.market_ticker,
            side=order.side,
            action=order.action,
            quantity=order.count,
            price=order.price / 100,
            order_id=f"PAPER-{signal_id}",
        )
        await update_trade_status(trade_id, "paper_filled", fill_price=order.price / 100)

        return {"order_id": f"PAPER-{signal_id}", "status": "paper_filled",
                "ticker": order.market_ticker, "side": order.side,
                "count": order.count, "price": order.price}

    # Real order placement
    try:
        if order.side == "yes":
            result = await kalshi.create_order(
                ticker=order.market_ticker,
                side=order.side,
                action=order.action,
                count=order.count,
                type="limit",
                yes_price=order.price,
            )
        else:
            result = await kalshi.create_order(
                ticker=order.market_ticker,
                side=order.side,
                action=order.action,
                count=order.count,
                type="limit",
                no_price=order.price,
            )
    except Exception as e:
        log.error("order_placement_failed", ticker=order.market_ticker, error=str(e))
        alert_error("order_placement", f"{order.market_ticker}: {e}")
        # Auto-kill on auth failures (401/403) to prevent repeated failures
        if "401" in str(e) or "403" in str(e):
            Path(settings.kill_switch_path).touch()
            log.error("auth_failure_kill_switch_activated")
        return None

    order_id = result.get("order_id", "")
    status = result.get("status", "unknown")

    log.info("order_placed", order_id=order_id, ticker=order.market_ticker,
             side=order.side, count=order.count, price=order.price, status=status)

    trade_id = await insert_trade(
        signal_id=signal_id,
        market_ticker=order.market_ticker,
        side=order.side,
        action=order.action,
        quantity=order.count,
        price=order.price / 100,
        order_id=order_id,
    )

    if status == "filled":
        fill_price = result.get("avg_fill_price", order.price) / 100
        await update_trade_status(trade_id, "filled", fill_price=fill_price)
    elif status != "filled":
        filled = await _wait_for_fill(kalshi, order_id, timeout=ORDER_TIMEOUT_SECONDS)
        if filled:
            fill_price = filled.get("avg_fill_price", order.price) / 100
            await update_trade_status(trade_id, "filled", fill_price=fill_price)
        else:
            try:
                await kalshi.cancel_order(order_id)
                log.info("order_cancelled_timeout", order_id=order_id)
            except Exception:
                pass
            await update_trade_status(trade_id, "cancelled")
            return None

    return result


async def _wait_for_fill(kalshi: KalshiClient, order_id: str,
                         timeout: int = ORDER_TIMEOUT_SECONDS) -> dict | None:
    elapsed = 0
    interval = 5
    while elapsed < timeout:
        try:
            order = await kalshi.get_order(order_id)
            status = order.get("status", "")
            if status == "filled":
                return order
            elif status in ("cancelled", "expired"):
                return None
        except Exception as e:
            log.warning("order_poll_error", order_id=order_id, error=str(e))
        await asyncio.sleep(interval)
        elapsed += interval
    return None


async def execute_all_orders(kalshi: KalshiClient, orders: list[TradeOrder]) -> list[dict]:
    """Execute orders sequentially with rate limiting."""
    results = []
    for i, order in enumerate(orders):
        result = await execute_order(kalshi, order)
        if result:
            results.append(result)
        # Rate limit: wait 1 second between orders
        if i < len(orders) - 1:
            await asyncio.sleep(1)
    return results
