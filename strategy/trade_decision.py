"""Final trade decision — combines signal + sizing + risk checks."""

from dataclasses import dataclass

import structlog

from signals.signal_aggregator import AggregatedSignal
from strategy.position_sizer import kelly_size
from strategy.risk_manager import pre_trade_checks
from data.kalshi_client import KalshiClient

log = structlog.get_logger()


@dataclass
class TradeOrder:
    market_ticker: str
    side: str          # "yes" or "no"
    action: str        # "buy"
    count: int         # Number of contracts
    price: int         # Limit price in cents (1-99)
    signal: AggregatedSignal


async def decide_trades(signals: list[AggregatedSignal],
                        kalshi: KalshiClient) -> list[TradeOrder]:
    """Convert signals into executable trade orders.

    Steps:
    1. Run pre-trade risk checks
    2. Get current balance for position sizing
    3. Size each position
    4. Get orderbook for limit price
    5. Build trade orders
    """
    if not signals:
        return []

    # Pre-trade checks (no orderbook yet — we'll check per-market)
    ok, reasons = await pre_trade_checks(kalshi=kalshi)
    if not ok:
        log.warning("trades_blocked_by_risk", reasons=reasons)
        return []

    # Get current balance and existing positions
    balance = await kalshi.get_balance()
    log.info("current_balance", balance=balance)

    # Check existing positions to avoid duplicates
    existing_tickers = set()
    try:
        positions = await kalshi.get_positions()
        existing_tickers = {p.get("ticker", "") for p in positions if p.get("ticker")}
        if existing_tickers:
            log.info("existing_positions", tickers=list(existing_tickers))
    except Exception:
        pass

    orders = []
    remaining_exposure = balance

    for signal in signals:
        if remaining_exposure <= 0:
            log.info("no_remaining_exposure")
            break

        # Skip if we already have a position in this market
        if signal.market_ticker in existing_tickers:
            log.info("skipping_existing_position", ticker=signal.market_ticker)
            continue

        # Get orderbook for liquidity check
        try:
            orderbook = await kalshi.get_orderbook(signal.market_ticker)
        except Exception as e:
            log.warning("orderbook_fetch_failed", ticker=signal.market_ticker, error=str(e))
            continue

        # Per-market liquidity check
        ok, reasons = await pre_trade_checks(kalshi=kalshi, orderbook=orderbook)
        if not ok:
            continue

        # Determine side and price
        if signal.direction == "BUY_YES":
            side = "yes"
            # Place limit slightly below FedWatch prob to ensure edge
            # Use the ask price from Kalshi as a baseline
            limit_price = int(signal.kalshi_price * 100)  # Current market price in cents
            # Don't overpay — cap at a price that preserves minimum edge
            max_price = int((signal.fedwatch_prob - 0.02) * 100)  # At least 2 cents edge
            limit_price = min(limit_price + 1, max_price)  # Improve by 1 cent
        else:
            side = "no"
            no_price = 1.0 - signal.kalshi_price
            limit_price = int(no_price * 100)
            max_price = int(((1 - signal.fedwatch_prob) - 0.02) * 100)
            limit_price = min(limit_price + 1, max_price)

        limit_price = max(1, min(99, limit_price))

        # Size the position
        buy_price_frac = limit_price / 100
        count = kelly_size(
            edge=signal.edge_estimate,
            price=buy_price_frac,
            bankroll=remaining_exposure,
        )

        if count <= 0:
            continue

        order = TradeOrder(
            market_ticker=signal.market_ticker,
            side=side,
            action="buy",
            count=count,
            price=limit_price,
            signal=signal,
        )
        orders.append(order)

        # Deduct from remaining exposure
        remaining_exposure -= count * buy_price_frac

        log.info("trade_decision",
                 ticker=signal.market_ticker, side=side,
                 count=count, price=limit_price,
                 edge=round(signal.edge_estimate, 3),
                 confidence=signal.confidence)

    return orders
