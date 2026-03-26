"""Core alpha signal: consensus probability vs Kalshi market price spread.

Works for ALL economic event types:
- FOMC: FedWatch cumulative probs vs KXFED "above X%" contracts
- CPI: Cleveland Fed Nowcast → probability distribution vs KXCPI contracts
- NFP: FRED payroll trend → probability distribution vs KXNFP contracts
- Claims: FRED claims trend → probability distribution vs KXINITCLAIMS contracts
- GDP: Atlanta Fed GDPNow → probability distribution vs KXGDP contracts

The pattern is always the same:
1. Get consensus estimate for the indicator
2. Convert to a probability for each Kalshi threshold
3. Compare consensus probability against Kalshi market price
4. If spread > threshold, generate a signal
"""

import re
from dataclasses import dataclass
from datetime import datetime

import structlog

from config.settings import settings
from config.economic_calendar import EconomicEvent, get_next_fomc_date, FOMC_DATES_2026
from data.kalshi_client import KalshiClient
from data.consensus_client import (
    get_consensus, consensus_to_probability, ConsensusEstimate,
)

log = structlog.get_logger()


@dataclass
class SpreadSignal:
    market_ticker: str
    event_type: str     # "fomc", "cpi", "nfp", "claims", "gdp"
    direction: str      # "BUY_YES" or "BUY_NO"
    kalshi_yes_price: float  # 0-1
    consensus_prob: float    # 0-1 (probability above threshold)
    raw_spread: float
    edge_after_fees: float
    threshold: float    # The "above X" threshold value
    rate_range: str     # e.g., "4.25-4.50" or ">220K"
    timestamp: datetime

    # Backward compat alias
    @property
    def fedwatch_prob(self) -> float:
        return self.consensus_prob


def _kalshi_fee(buy_price: float) -> float:
    potential_profit = max(0, 1.0 - buy_price)
    return potential_profit * settings.kalshi_fee_rate


def _extract_threshold(ticker: str) -> float | None:
    """Extract numeric threshold from a Kalshi ticker.

    KXFED-26APR-T4.25 → 4.25
    KXCPI-26MAR-T3.0 → 3.0
    KXNFP-26MAR-T200 → 200
    """
    match = re.search(r'T(\d+\.?\d*)', ticker)
    if match:
        return float(match.group(1))
    # Try B prefix (below)
    match = re.search(r'B(\d+\.?\d*)', ticker)
    if match:
        return float(match.group(1))
    return None


# --- FOMC-specific logic (existing, preserved) ---

def _fedwatch_to_cumulative(fedwatch_probs: dict[str, float]) -> dict[float, float]:
    """Convert FedWatch range probabilities to cumulative "above X%" probabilities."""
    from data.fedwatch import rate_range_to_bps

    parsed = []
    for range_str, prob in fedwatch_probs.items():
        low_bps, high_bps = rate_range_to_bps(range_str)
        parsed.append((low_bps / 100, high_bps / 100, prob))
    parsed.sort(key=lambda x: x[0])

    cumulative = {}
    running = 0.0
    for low, high, prob in reversed(parsed):
        running += prob
        cumulative[low] = round(running, 4)

    return cumulative


def _get_meeting_event_ticker(meeting_date) -> str:
    month_abbrevs = {
        1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
        7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
    }
    yr = str(meeting_date.year)[-2:]
    mon = month_abbrevs[meeting_date.month]
    return f"KXFED-{yr}{mon}"


async def _compute_fomc_spread_signals(kalshi: KalshiClient) -> list[SpreadSignal]:
    """Original FOMC spread signal logic using FedWatch."""
    from data.fedwatch import compute_fedwatch_probabilities

    next_meeting = get_next_fomc_date()
    if next_meeting is None:
        return []

    fedwatch_probs = compute_fedwatch_probabilities(next_meeting)
    if not fedwatch_probs:
        log.warning("no_fedwatch_probabilities")
        return []

    cumulative = _fedwatch_to_cumulative(fedwatch_probs)
    log.info("fedwatch_cumulative", probs=cumulative)

    event_ticker = _get_meeting_event_ticker(next_meeting)
    try:
        markets = await kalshi.get_markets(event_ticker=event_ticker)
    except Exception as e:
        log.warning("kalshi_markets_fetch_failed", event=event_ticker, error=str(e))
        return []

    if not markets:
        return []

    return _compute_signals_from_markets(
        markets=markets,
        prob_lookup=cumulative,
        event_type="fomc",
    )


# --- Generic logic for non-FOMC events ---

async def _compute_generic_spread_signals(
    kalshi: KalshiClient,
    event: EconomicEvent,
) -> list[SpreadSignal]:
    """Compute spread signals for CPI, NFP, Claims, GDP using consensus estimates."""
    consensus = await get_consensus(event.event_type)
    if consensus is None:
        log.warning("no_consensus_data", event_type=event.event_type)
        return []

    # Discover Kalshi markets for this event
    markets = await kalshi.get_economic_markets(event.event_type, event.series_prefix)
    if not markets:
        log.info("no_markets_for_event", event_type=event.event_type, prefix=event.series_prefix)
        return []

    # Build probability lookup: for each threshold, compute P(above threshold)
    prob_lookup = {}
    for market in markets:
        ticker = market.get("ticker", "")
        threshold = _extract_threshold(ticker)
        if threshold is not None:
            prob = consensus_to_probability(consensus, threshold, above=True)
            prob_lookup[threshold] = prob

    if not prob_lookup:
        return []

    log.info("consensus_probabilities",
             event_type=event.event_type,
             consensus=consensus.point_estimate,
             source=consensus.source,
             thresholds=prob_lookup)

    return _compute_signals_from_markets(
        markets=markets,
        prob_lookup=prob_lookup,
        event_type=event.event_type,
    )


def _compute_signals_from_markets(
    markets: list[dict],
    prob_lookup: dict[float, float],
    event_type: str,
) -> list[SpreadSignal]:
    """Generic signal computation: compare probability lookup against Kalshi prices."""
    signals = []

    for market in markets:
        ticker = market.get("ticker", "")
        threshold = _extract_threshold(ticker)
        if threshold is None:
            continue

        yes_ask = market.get("yes_ask", 0)
        yes_bid = market.get("yes_bid", 0)

        if yes_ask <= 0 and yes_bid <= 0:
            continue

        if yes_bid > 0 and yes_ask > 0:
            kalshi_mid = (yes_bid + yes_ask) / 2 / 100
        else:
            kalshi_mid = max(yes_ask, yes_bid) / 100

        if kalshi_mid <= 0:
            continue

        # Find matching probability
        prob = prob_lookup.get(threshold)
        if prob is None:
            closest = min(prob_lookup.keys(), key=lambda x: abs(x - threshold), default=None)
            if closest is not None and abs(closest - threshold) <= _threshold_tolerance(event_type):
                prob = prob_lookup[closest]
            else:
                continue

        raw_spread = prob - kalshi_mid

        if abs(raw_spread) < settings.probability_threshold:
            continue

        if raw_spread > 0:
            buy_price = yes_ask / 100 if yes_ask > 0 else kalshi_mid
            fee = _kalshi_fee(buy_price)
            edge = prob - buy_price - fee
            direction = "BUY_YES"
        else:
            no_ask = market.get("no_ask", 0)
            buy_price = no_ask / 100 if no_ask > 0 else (1 - kalshi_mid)
            fee = _kalshi_fee(buy_price)
            edge = (1 - prob) - buy_price - fee
            direction = "BUY_NO"

        if edge < settings.min_edge_after_fees:
            continue

        signal = SpreadSignal(
            market_ticker=ticker,
            event_type=event_type,
            direction=direction,
            kalshi_yes_price=kalshi_mid,
            consensus_prob=prob,
            raw_spread=abs(raw_spread),
            edge_after_fees=edge,
            threshold=threshold,
            rate_range=_format_range(event_type, threshold),
            timestamp=datetime.utcnow(),
        )
        signals.append(signal)
        log.info("spread_signal",
                 ticker=ticker, event_type=event_type, direction=direction,
                 threshold=threshold,
                 kalshi=round(kalshi_mid, 3), consensus=round(prob, 3),
                 spread=round(abs(raw_spread), 3), edge=round(edge, 3))

    return signals


def _threshold_tolerance(event_type: str) -> float:
    """How close a threshold must be to match (event-type dependent)."""
    return {
        "fomc": 0.25,     # 25bps rate buckets
        "cpi": 0.2,       # 0.2% CPI buckets
        "nfp": 25.0,      # 25K jobs buckets
        "claims": 10.0,   # 10K claims buckets
        "gdp": 0.5,       # 0.5% GDP buckets
    }.get(event_type, 0.25)


def _format_range(event_type: str, threshold: float) -> str:
    """Format a human-readable range string."""
    if event_type == "fomc":
        return f"{threshold:.2f}-{threshold + 0.25:.2f}"
    elif event_type in ("cpi", "gdp"):
        return f">{threshold:.1f}%"
    elif event_type in ("nfp", "claims"):
        return f">{threshold:.0f}K"
    return f">{threshold}"


# --- Public API ---

async def compute_spread_signals(
    kalshi: KalshiClient,
    events: list[EconomicEvent] | None = None,
) -> list[SpreadSignal]:
    """Compute spread signals for all active economic events.

    If events is None, computes FOMC signals only (backward compatible).
    If events is provided, computes signals for each event type.
    """
    all_signals = []

    if events is None:
        # Backward compatible: FOMC only
        return await _compute_fomc_spread_signals(kalshi)

    for event in events:
        try:
            if event.event_type == "fomc":
                signals = await _compute_fomc_spread_signals(kalshi)
            else:
                signals = await _compute_generic_spread_signals(kalshi, event)
            all_signals.extend(signals)
        except Exception as e:
            log.error("spread_signal_error",
                      event_type=event.event_type, error=str(e))

    return all_signals
