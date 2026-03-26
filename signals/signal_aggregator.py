"""Combines ALL signals into final trade signals — spread, macro, Twitter, Polymarket."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from signals.probability_spread import SpreadSignal, compute_spread_signals
from signals.macro_trend import MacroBias, compute_macro_bias
from data.kalshi_client import KalshiClient
from config.economic_calendar import EconomicEvent, get_upcoming_events

log = structlog.get_logger()

# Signal weights by event type
# FOMC: Polymarket useful (rate predictions). Others: not useful for econ data.
WEIGHTS = {
    "fomc": {"spread": 0.55, "macro": 0.15, "sentiment": 0.15, "polymarket": 0.15},
    "cpi":  {"spread": 0.60, "macro": 0.20, "sentiment": 0.20, "polymarket": 0.0},
    "nfp":  {"spread": 0.60, "macro": 0.20, "sentiment": 0.20, "polymarket": 0.0},
    "claims": {"spread": 0.65, "macro": 0.20, "sentiment": 0.15, "polymarket": 0.0},
    "gdp":  {"spread": 0.60, "macro": 0.20, "sentiment": 0.20, "polymarket": 0.0},
}
DEFAULT_WEIGHTS = {"spread": 0.60, "macro": 0.20, "sentiment": 0.20, "polymarket": 0.0}


@dataclass
class AggregatedSignal:
    market_ticker: str
    event_type: str     # "fomc", "cpi", "nfp", "claims", "gdp"
    direction: str      # "BUY_YES" or "BUY_NO"
    confidence: float   # 0.0 to 1.0
    edge_estimate: float
    fedwatch_prob: float  # backward compat — really "consensus_prob"
    kalshi_price: float
    rate_range: str
    macro_bias: str
    sentiment_score: float = 0.0
    polymarket_agrees: bool = False
    data_sources: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)


def _macro_aligns(spread_signal: SpreadSignal, macro: MacroBias) -> float:
    """Returns 1.0 (aligns), 0.5 (neutral), 0.0 (contradicts)."""
    if macro.direction == "neutral":
        return 0.5

    event_type = spread_signal.event_type

    if event_type == "fomc":
        # BUY_YES = expecting higher rates = hawkish
        if spread_signal.direction == "BUY_YES" and macro.direction == "hawkish":
            return 1.0
        if spread_signal.direction == "BUY_NO" and macro.direction == "dovish":
            return 1.0
        if spread_signal.direction == "BUY_YES" and macro.direction == "dovish":
            return 0.0
        if spread_signal.direction == "BUY_NO" and macro.direction == "hawkish":
            return 0.0
    elif event_type in ("cpi",):
        # BUY_YES on CPI "above X%" = expecting higher inflation = hawkish
        if spread_signal.direction == "BUY_YES" and macro.direction == "hawkish":
            return 1.0
        if spread_signal.direction == "BUY_NO" and macro.direction == "dovish":
            return 1.0
        return 0.3
    elif event_type in ("nfp",):
        # BUY_YES on NFP "above X" = strong labor market = hawkish
        if spread_signal.direction == "BUY_YES" and macro.direction == "hawkish":
            return 0.8
        if spread_signal.direction == "BUY_NO" and macro.direction == "dovish":
            return 0.8
        return 0.4
    elif event_type in ("claims",):
        # BUY_YES on claims "above X" = more claims = weaker labor = dovish
        if spread_signal.direction == "BUY_YES" and macro.direction == "dovish":
            return 0.8
        if spread_signal.direction == "BUY_NO" and macro.direction == "hawkish":
            return 0.8
        return 0.4
    elif event_type in ("gdp",):
        # BUY_YES on GDP "above X%" = strong growth = hawkish
        if spread_signal.direction == "BUY_YES" and macro.direction == "hawkish":
            return 0.8
        if spread_signal.direction == "BUY_NO" and macro.direction == "dovish":
            return 0.8
        return 0.4

    return 0.5


def _sentiment_aligns(spread_signal: SpreadSignal, sentiment_score: float) -> float:
    """Check if Twitter sentiment aligns with spread signal."""
    if abs(sentiment_score) < 0.05:
        return 0.5

    # For all event types, positive sentiment ≈ dovish, negative ≈ hawkish
    if spread_signal.direction == "BUY_YES" and sentiment_score > 0:
        return min(0.5 + abs(sentiment_score), 1.0)
    if spread_signal.direction == "BUY_NO" and sentiment_score < 0:
        return min(0.5 + abs(sentiment_score), 1.0)

    return max(0.5 - abs(sentiment_score), 0.0)


async def _get_twitter_sentiment() -> dict:
    """Safely fetch Twitter sentiment."""
    try:
        from data.twitter_sentiment import twitter_client
        from config.settings import settings
        if not settings.twitter_bearer_token:
            return {"score": 0.0, "tweet_count": 0}
        return await twitter_client.get_fed_sentiment()
    except Exception as e:
        log.warning("twitter_sentiment_failed", error=str(e))
        return {"score": 0.0, "tweet_count": 0}


async def _get_polymarket_probs() -> dict[str, float]:
    """Safely fetch Polymarket probabilities."""
    try:
        from data.polymarket import get_polymarket_probabilities
        return await get_polymarket_probabilities()
    except Exception as e:
        log.warning("polymarket_fetch_failed", error=str(e))
        return {}


def _polymarket_confirms(spread_signal: SpreadSignal, poly_probs: dict[str, float]) -> float:
    """Check if Polymarket agrees with our spread signal direction.

    Only meaningful for FOMC (rate cut/hike). Returns 0.5 for other event types.
    """
    if spread_signal.event_type != "fomc" or not poly_probs:
        return 0.5

    cut_prob = 0.0
    hike_prob = 0.0

    for key, prob in poly_probs.items():
        key_lower = key.lower()
        if any(w in key_lower for w in ["cut", "lower", "decrease", "reduce"]):
            if "yes" in key_lower:
                cut_prob = max(cut_prob, prob)
        elif any(w in key_lower for w in ["hike", "raise", "increase", "higher"]):
            if "yes" in key_lower:
                hike_prob = max(hike_prob, prob)

    if cut_prob == 0 and hike_prob == 0:
        return 0.5

    if spread_signal.direction == "BUY_YES":
        return min(hike_prob + 0.3, 1.0) if hike_prob > cut_prob else 0.3
    else:
        return min(cut_prob + 0.3, 1.0) if cut_prob > hike_prob else 0.3


async def generate_signals(
    kalshi: KalshiClient,
    events: list[EconomicEvent] | None = None,
) -> list[AggregatedSignal]:
    """Generate aggregated trading signals from ALL data sources.

    Args:
        kalshi: Kalshi API client
        events: Economic events to generate signals for.
                If None, uses get_upcoming_events() to find active events.
    """
    # Determine which events to process
    if events is None:
        events = get_upcoming_events(within_days=7)

    if not events:
        log.info("no_upcoming_events")
        # Backward compat: try FOMC anyway
        events = []

    # 1. Get spread signals for all events
    spread_signals = await compute_spread_signals(kalshi, events if events else None)
    if not spread_signals:
        log.info("no_spread_signals")
        return []

    # 2. Get confirmation signals in parallel
    async def _wrap_macro():
        return compute_macro_bias()

    macro_task, sentiment_task, poly_task = await asyncio.gather(
        _wrap_macro(),
        _get_twitter_sentiment(),
        _get_polymarket_probs(),
    )
    macro = macro_task
    sentiment = sentiment_task
    poly_probs = poly_task

    sentiment_score = sentiment.get("score", 0.0)
    tweet_count = sentiment.get("tweet_count", 0)

    log.info("signal_sources_loaded",
             spread_signals=len(spread_signals),
             macro=macro.direction,
             sentiment_score=sentiment_score,
             tweets=tweet_count,
             polymarket_outcomes=len(poly_probs))

    aggregated = []
    for ss in spread_signals:
        event_type = ss.event_type
        weights = WEIGHTS.get(event_type, DEFAULT_WEIGHTS)
        sources = ["consensus", "kalshi"]

        # Macro alignment
        macro_align = _macro_aligns(ss, macro)
        if macro.confidence > 0:
            sources.append("fred")

        # Twitter alignment
        sent_align = _sentiment_aligns(ss, sentiment_score)
        if tweet_count > 0:
            sources.append("twitter")

        # Polymarket alignment (FOMC only)
        poly_align = _polymarket_confirms(ss, poly_probs)
        if poly_probs and event_type == "fomc":
            sources.append("polymarket")

        # Weighted confidence score
        spread_confidence = min(ss.edge_after_fees / 0.10, 1.0)
        macro_confidence = macro.confidence * macro_align
        sent_confidence = sent_align if tweet_count > 10 else 0.5
        poly_confidence = poly_align

        overall_confidence = (
            weights["spread"] * spread_confidence +
            weights["macro"] * macro_confidence +
            weights["sentiment"] * sent_confidence +
            weights["polymarket"] * poly_confidence
        )

        # Consensus bonus: count how many non-spread sources agree
        active_sources = []
        if weights["macro"] > 0:
            active_sources.append(macro_align > 0.5)
        if weights["sentiment"] > 0:
            active_sources.append(sent_align > 0.5)
        if weights["polymarket"] > 0:
            active_sources.append(poly_align > 0.5)

        agreeing = sum(active_sources)
        total_sources = len(active_sources)

        if total_sources > 0 and agreeing == total_sources:
            overall_confidence *= 1.15  # 15% bonus for full consensus
        elif agreeing == 0 and ss.edge_after_fees < 0.05:
            log.info("signal_filtered_no_consensus",
                     ticker=ss.market_ticker, edge=ss.edge_after_fees)
            continue

        overall_confidence = min(overall_confidence, 1.0)

        signal = AggregatedSignal(
            market_ticker=ss.market_ticker,
            event_type=event_type,
            direction=ss.direction,
            confidence=round(overall_confidence, 3),
            edge_estimate=ss.edge_after_fees,
            fedwatch_prob=ss.consensus_prob,
            kalshi_price=ss.kalshi_yes_price,
            rate_range=ss.rate_range,
            macro_bias=macro.direction,
            sentiment_score=sentiment_score,
            polymarket_agrees=poly_align > 0.5,
            data_sources=sources,
            timestamp=datetime.utcnow(),
        )
        aggregated.append(signal)

        log.info("aggregated_signal",
                 ticker=signal.market_ticker,
                 event_type=event_type,
                 direction=signal.direction,
                 confidence=signal.confidence,
                 edge=round(signal.edge_estimate, 3),
                 macro=macro.direction,
                 sentiment=sentiment_score,
                 polymarket=signal.polymarket_agrees,
                 sources=sources,
                 consensus=f"{agreeing}/{total_sources}")

    # Sort by edge (best opportunities first)
    aggregated.sort(key=lambda s: s.edge_estimate, reverse=True)
    return aggregated
