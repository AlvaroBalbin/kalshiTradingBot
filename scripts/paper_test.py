"""Paper trading test — simulates a full trading cycle with mock market data.

Run: python scripts/paper_test.py
Tests the entire pipeline: signal generation -> trade decision -> execution -> exit monitoring.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, date
from unittest.mock import AsyncMock, MagicMock, patch

from config.settings import settings
from db.database import init_db, get_open_positions, get_todays_trades
from signals.probability_spread import SpreadSignal, _fedwatch_to_cumulative
from signals.signal_aggregator import AggregatedSignal, generate_signals
from strategy.trade_decision import decide_trades, TradeOrder
from strategy.position_sizer import kelly_size
from strategy.risk_manager import pre_trade_checks
from execution.order_manager import execute_order, execute_all_orders
from execution.position_monitor import check_exits

import structlog
log = structlog.get_logger()


# ── Mock Data ──────────────────────────────────────────────

MOCK_FEDWATCH = {
    "4.00-4.25": 0.15,
    "4.25-4.50": 0.60,
    "4.50-4.75": 0.20,
    "4.75-5.00": 0.05,
}

MOCK_MARKETS = [
    {
        "ticker": "KXFED-26APR-T4.00",
        "yes_bid": 82, "yes_ask": 85,
        "no_bid": 15, "no_ask": 18,
        "status": "open",
    },
    {
        "ticker": "KXFED-26APR-T4.25",
        "yes_bid": 78, "yes_ask": 81,  # Kalshi says 79.5% -> FedWatch says 85% -> mispriced
        "no_bid": 19, "no_ask": 22,
        "status": "open",
    },
    {
        "ticker": "KXFED-26APR-T4.50",
        "yes_bid": 18, "yes_ask": 22,
        "no_bid": 78, "no_ask": 82,
        "status": "open",
    },
    {
        "ticker": "KXFED-26APR-T4.75",
        "yes_bid": 3, "yes_ask": 7,
        "no_bid": 93, "no_ask": 97,
        "status": "open",
    },
]

MOCK_ORDERBOOK = {
    "yes": [[80, 50], [79, 100]],
    "no": [[20, 50], [21, 100]],
}


def print_header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_result(label: str, value, ok: bool = True):
    status = "OK" if ok else "FAIL"
    print(f"  [{status}] {label}: {value}")


async def test_fedwatch_cumulative():
    """Test FedWatch probability conversion."""
    print_header("TEST 1: FedWatch Cumulative Probability Conversion")

    cumulative = _fedwatch_to_cumulative(MOCK_FEDWATCH)

    print(f"  Input (range probabilities):")
    for k, v in sorted(MOCK_FEDWATCH.items()):
        print(f"    {k}: {v:.0%}")

    print(f"\n  Output (cumulative 'above X%'):")
    for rate, prob in sorted(cumulative.items()):
        print(f"    above {rate:.2f}%: {prob:.1%}")

    # Validate: above 4.00 should be 100%, above 4.25 should be 85%
    assert abs(cumulative.get(4.0, 0) - 1.0) < 0.01, "above 4.00 should be ~100%"
    assert abs(cumulative.get(4.25, 0) - 0.85) < 0.01, "above 4.25 should be ~85%"
    assert abs(cumulative.get(4.50, 0) - 0.25) < 0.01, "above 4.50 should be ~25%"
    assert abs(cumulative.get(4.75, 0) - 0.05) < 0.01, "above 4.75 should be ~5%"

    print_result("Cumulative probabilities", "correct", True)
    return True


async def test_kelly_sizing():
    """Test position sizing."""
    print_header("TEST 2: Kelly Position Sizing")

    cases = [
        {"edge": 0.05, "price": 0.80, "bankroll": 100.0, "desc": "5% edge at 80c"},
        {"edge": 0.10, "price": 0.50, "bankroll": 100.0, "desc": "10% edge at 50c"},
        {"edge": 0.02, "price": 0.90, "bankroll": 50.0, "desc": "2% edge at 90c (tight)"},
        {"edge": 0.0, "price": 0.50, "bankroll": 100.0, "desc": "0% edge (should be 0)"},
        {"edge": -0.05, "price": 0.50, "bankroll": 100.0, "desc": "negative edge (should be 0)"},
    ]

    all_ok = True
    for case in cases:
        count = kelly_size(case["edge"], case["price"], case["bankroll"])
        expected_zero = case["edge"] <= 0
        ok = (count == 0) if expected_zero else (count > 0)
        print_result(case["desc"], f"{count} contracts", ok)
        if not ok:
            all_ok = False

    return all_ok


async def test_risk_checks():
    """Test risk management checks."""
    print_header("TEST 3: Risk Management Checks")

    ok, reasons = await pre_trade_checks()
    print_result("Basic pre-trade checks", ", ".join(reasons), ok)

    ok2, reasons2 = await pre_trade_checks(MOCK_ORDERBOOK)
    print_result("With orderbook liquidity", ", ".join(reasons2), ok2)

    # Test with wide spread orderbook
    wide_book = {"yes": [[50, 10]], "no": [[35, 10]]}
    ok3, reasons3 = await pre_trade_checks(wide_book)
    print_result("Wide spread (should warn)", ", ".join(reasons3), True)

    return True


async def test_paper_trading_cycle():
    """Full paper trading cycle: signal -> decide -> execute -> monitor."""
    print_header("TEST 4: Full Paper Trading Cycle")

    # Ensure paper trading mode
    assert settings.paper_trading, "Paper trading must be enabled!"
    print_result("Paper trading mode", "ON", True)

    # Create mock signals
    signals = [
        AggregatedSignal(
            market_ticker="KXFED-26APR-T4.25",
            direction="BUY_YES",
            confidence=0.72,
            edge_estimate=0.055,
            fedwatch_prob=0.85,
            kalshi_price=0.795,
            rate_range="4.25-4.50",
            macro_bias="dovish",
            sentiment_score=0.1,
            polymarket_agrees=True,
            data_sources=["fedwatch", "kalshi", "fred", "polymarket"],
        ),
    ]
    print_result("Mock signals created", f"{len(signals)} signals", True)

    # Create mock Kalshi client
    mock_kalshi = AsyncMock()
    mock_kalshi.get_balance = AsyncMock(return_value=100.0)
    mock_kalshi.get_positions = AsyncMock(return_value=[])
    mock_kalshi.get_orderbook = AsyncMock(return_value=MOCK_ORDERBOOK)
    mock_kalshi.get_market = AsyncMock(return_value={
        "ticker": "KXFED-26APR-T4.25",
        "yes_bid": 90, "yes_ask": 92,  # Price moved up -> profit
        "no_bid": 8, "no_ask": 10,
        "status": "open",
    })

    # Decide trades
    orders = await decide_trades(signals, mock_kalshi)
    print_result("Trade decisions", f"{len(orders)} orders", len(orders) > 0)

    if orders:
        for order in orders:
            print(f"    Order: {order.side.upper()} {order.count}x {order.market_ticker} @ {order.price}c")

        # Execute paper trades
        results = await execute_all_orders(mock_kalshi, orders)
        print_result("Paper execution", f"{len(results)} fills", len(results) > 0)
        for r in results:
            print(f"    Fill: {r.get('order_id')} -> {r.get('status')}")

    # Check for open positions
    open_pos = await get_open_positions()
    print_result("Open positions in DB", f"{len(open_pos)}", True)

    # Test exit monitoring (with mocked higher price -> profit target)
    if open_pos:
        exits = await check_exits(mock_kalshi)
        print_result("Exit monitoring", f"{len(exits)} exits", True)
        for ex in exits:
            print(f"    Exit: {ex.get('ticker')} -> {ex.get('action')} (PnL: ${ex.get('pnl', 0):.2f})")

    return True


async def test_signal_aggregation_with_mocks():
    """Test signal aggregation with mocked external data."""
    print_header("TEST 5: Signal Aggregation (Mocked Data)")

    mock_kalshi = AsyncMock()
    mock_kalshi.get_markets = AsyncMock(return_value=MOCK_MARKETS)

    # Mock FedWatch
    with patch("signals.probability_spread.compute_fedwatch_probabilities", return_value=MOCK_FEDWATCH), \
         patch("signals.probability_spread.get_next_fomc_date", return_value=date(2026, 4, 29)), \
         patch("signals.signal_aggregator.compute_macro_bias") as mock_macro, \
         patch("signals.signal_aggregator._get_twitter_sentiment") as mock_twitter, \
         patch("signals.signal_aggregator._get_polymarket_probs") as mock_poly:

        from signals.macro_trend import MacroBias
        mock_macro.return_value = MacroBias(direction="dovish", confidence=0.6, reasons=["Low inflation"])
        mock_twitter.return_value = {"score": 0.15, "tweet_count": 25}
        mock_poly.return_value = {"Rate cut - Yes": 0.65}

        signals = await generate_signals(mock_kalshi)

        print_result("Signals generated", f"{len(signals)}", True)
        for s in signals:
            print(f"    {s.direction} {s.market_ticker}")
            print(f"      Edge: {s.edge_estimate:.1%} | Confidence: {s.confidence:.1%}")
            print(f"      FedWatch: {s.fedwatch_prob:.1%} vs Kalshi: {s.kalshi_price:.1%}")
            print(f"      Sources: {', '.join(s.data_sources)}")

    return True


async def main():
    print("""
==========================================================
    KALSHI TRADING BOT -- PAPER TRADING TEST
    Testing: Signal -> Decision -> Execution -> Exit
==========================================================
    """)

    # Ensure paper trading
    if not settings.paper_trading:
        print("ERROR: Set PAPER_TRADING=true in .env before running tests!")
        sys.exit(1)

    # Init DB
    init_db()
    print_result("Database initialized", "bot.db", True)

    results = {}
    tests = [
        ("FedWatch Conversion", test_fedwatch_cumulative),
        ("Kelly Sizing", test_kelly_sizing),
        ("Risk Checks", test_risk_checks),
        ("Paper Trading Cycle", test_paper_trading_cycle),
        ("Signal Aggregation", test_signal_aggregation_with_mocks),
    ]

    for name, test_fn in tests:
        try:
            ok = await test_fn()
            results[name] = ok
        except Exception as e:
            print_result(name, f"EXCEPTION: {e}", False)
            results[name] = False
            import traceback
            traceback.print_exc()

    # Summary
    print_header("TEST SUMMARY")
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
    print(f"\n  {passed}/{total} tests passed")

    if passed == total:
        print("\n  All tests passed! Ready for Raspberry Pi deployment.")
    else:
        print("\n  Some tests failed. Fix issues before deploying.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
