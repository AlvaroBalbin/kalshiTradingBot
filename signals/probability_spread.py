"""Core alpha signal: FedWatch vs Kalshi probability spread.

Kalshi Fed markets are binary "above X%" contracts:
- KXFED-26APR-T4.25 = "Will the fed funds upper bound be above 4.25%?"
- YES price = cumulative probability that rate >= the threshold

FedWatch gives us probabilities for specific rate ranges (e.g., 3.75-4.00 = 70%).
We convert FedWatch range probabilities to cumulative "above X%" probabilities
to compare against Kalshi prices.
"""

import re
from dataclasses import dataclass
from datetime import datetime

import structlog

from config.settings import settings
from config.fomc_calendar import get_next_fomc_date, FOMC_MEETINGS_2026
from data.kalshi_client import KalshiClient
from data.fedwatch import compute_fedwatch_probabilities, rate_range_to_bps

log = structlog.get_logger()


@dataclass
class SpreadSignal:
    market_ticker: str
    direction: str  # "BUY_YES" or "BUY_NO"
    kalshi_yes_price: float  # 0-1
    fedwatch_prob: float  # 0-1 (cumulative above threshold)
    raw_spread: float
    edge_after_fees: float
    threshold_rate: float  # The "above X%" threshold
    rate_range: str  # e.g., "4.25-4.50"
    timestamp: datetime


def _kalshi_fee(buy_price: float) -> float:
    potential_profit = max(0, 1.0 - buy_price)
    return potential_profit * settings.kalshi_fee_rate


def _extract_threshold(ticker: str) -> float | None:
    """Extract the rate threshold from a Kalshi ticker.

    KXFED-26APR-T4.25 → 4.25
    KXFED-26APR-T3.75 → 3.75
    """
    match = re.search(r'T(\d+\.\d+)', ticker)
    if match:
        return float(match.group(1))
    return None


def _fedwatch_to_cumulative(fedwatch_probs: dict[str, float]) -> dict[float, float]:
    """Convert FedWatch range probabilities to cumulative "above X%" probabilities.

    If FedWatch says:
      3.50-3.75: 10%, 3.75-4.00: 70%, 4.00-4.25: 15%, 4.25-4.50: 5%
    Then cumulative "above" probabilities are:
      above 3.50: 100% (all outcomes are >= 3.50)
      above 3.75: 90% (everything except 3.50-3.75)
      above 4.00: 20% (4.00-4.25 + 4.25-4.50)
      above 4.25: 5% (just 4.25-4.50)
    """
    # Parse ranges and sort by lower bound
    parsed = []
    for range_str, prob in fedwatch_probs.items():
        low_bps, high_bps = rate_range_to_bps(range_str)
        parsed.append((low_bps / 100, high_bps / 100, prob))
    parsed.sort(key=lambda x: x[0])

    # Build cumulative from top
    cumulative = {}
    running = 0.0
    for low, high, prob in reversed(parsed):
        running += prob
        cumulative[low] = round(running, 4)

    return cumulative


def _get_meeting_event_ticker(meeting_date) -> str:
    """Build the Kalshi event ticker for a meeting date.

    April 29, 2026 → KXFED-26APR
    """
    month_abbrevs = {
        1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
        7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
    }
    yr = str(meeting_date.year)[-2:]
    mon = month_abbrevs[meeting_date.month]
    return f"KXFED-{yr}{mon}"


async def compute_spread_signals(kalshi: KalshiClient) -> list[SpreadSignal]:
    """Compute probability spread signals for Fed markets.

    Compares FedWatch cumulative probabilities against Kalshi "above X%" prices.
    """
    next_meeting = get_next_fomc_date()
    if next_meeting is None:
        log.info("no_upcoming_fomc_meeting")
        return []

    # Get FedWatch probabilities
    fedwatch_probs = compute_fedwatch_probabilities(next_meeting)
    if not fedwatch_probs:
        log.warning("no_fedwatch_probabilities")
        return []

    # Convert to cumulative "above" probabilities
    cumulative = _fedwatch_to_cumulative(fedwatch_probs)
    log.info("fedwatch_cumulative", probs=cumulative)

    # Get Kalshi markets for this meeting
    event_ticker = _get_meeting_event_ticker(next_meeting)
    try:
        markets = await kalshi.get_markets(event_ticker=event_ticker)
    except Exception as e:
        log.warning("kalshi_markets_fetch_failed", event=event_ticker, error=str(e))
        return []

    if not markets:
        log.warning("no_kalshi_markets", event_ticker=event_ticker)
        return []

    signals = []
    for market in markets:
        ticker = market.get("ticker", "")
        threshold = _extract_threshold(ticker)
        if threshold is None:
            continue

        # Get Kalshi price
        yes_ask = market.get("yes_ask", 0)
        yes_bid = market.get("yes_bid", 0)

        if yes_ask <= 0 and yes_bid <= 0:
            continue  # No liquidity

        kalshi_mid = ((yes_bid + yes_ask) / 2) / 100 if (yes_bid > 0 and yes_ask > 0) else max(yes_ask, yes_bid) / 100

        if kalshi_mid <= 0:
            continue

        # Find matching FedWatch cumulative probability
        fw_prob = cumulative.get(threshold)
        if fw_prob is None:
            # Find closest threshold
            closest = min(cumulative.keys(), key=lambda x: abs(x - threshold), default=None)
            if closest is not None and abs(closest - threshold) <= 0.25:
                fw_prob = cumulative[closest]
            else:
                continue

        # Calculate spread
        raw_spread = fw_prob - kalshi_mid

        if abs(raw_spread) < settings.probability_threshold:
            continue

        # Determine direction and edge
        if raw_spread > 0:
            # Kalshi underpriced → BUY YES
            buy_price = yes_ask / 100 if yes_ask > 0 else kalshi_mid
            fee = _kalshi_fee(buy_price)
            edge = fw_prob - buy_price - fee
            direction = "BUY_YES"
        else:
            # Kalshi overpriced → BUY NO
            no_ask = market.get("no_ask", 0)
            buy_price = no_ask / 100 if no_ask > 0 else (1 - kalshi_mid)
            fee = _kalshi_fee(buy_price)
            edge = (1 - fw_prob) - buy_price - fee
            direction = "BUY_NO"

        if edge < settings.min_edge_after_fees:
            continue

        signal = SpreadSignal(
            market_ticker=ticker,
            direction=direction,
            kalshi_yes_price=kalshi_mid,
            fedwatch_prob=fw_prob,
            raw_spread=abs(raw_spread),
            edge_after_fees=edge,
            threshold_rate=threshold,
            rate_range=f"{threshold:.2f}-{threshold + 0.25:.2f}",
            timestamp=datetime.utcnow(),
        )
        signals.append(signal)
        log.info("spread_signal",
                 ticker=ticker, direction=direction,
                 threshold=threshold,
                 kalshi=round(kalshi_mid, 3), fedwatch=round(fw_prob, 3),
                 spread=round(abs(raw_spread), 3), edge=round(edge, 3))

    return signals
