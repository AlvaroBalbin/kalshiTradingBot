"""Combines ALL signals into final trade signals — FedWatch spread, macro, Twitter, Polymarket."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from signals.probability_spread import SpreadSignal, compute_spread_signals
from signals.macro_trend import MacroBias, compute_macro_bias
from data.kalshi_client import KalshiClient

log = structlog.get_logger()

# Signal weights — spread is king, everything else is confirmation
WEIGHT_SPREAD = 0.55
WEIGHT_MACRO = 0.15
WEIGHT_SENTIMENT = 0.15
WEIGHT_POLYMARKET = 0.15


@dataclass
class AggregatedSignal:
    market_ticker: str
    direction: str  # "BUY_YES" or "BUY_NO"
    confidence: float  # 0.0 to 1.0
    edge_estimate: float
    fedwatch_prob: float
    kalshi_price: float
    rate_range: str
    macro_bias: str
    sentiment_score: float = 0.0
    polymarket_agrees: bool = False
    data_sources: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)


def _macro_aligns(spread_signal: SpreadSignal, macro: MacroBias) -> float:
    """Returns 1.0 (aligns), 0.5 (neutral), 0.0 (contradicts).

    Kalshi "above X%" contracts:
    - BUY_YES = expecting rate >= threshold (higher rates)
    - BUY_NO = expecting rate < threshold (lower rates)

    So BUY_YES on a high threshold is hawkish, BUY_NO is dovish.
    """
    if macro.direction == "neutral":
        return 0.5
    # BUY_YES = expecting higher rates = hawkish
    if spread_signal.direction == "BUY_YES" and macro.direction == "hawkish":
        return 1.0
    if spread_signal.direction == "BUY_NO" and macro.direction == "dovish":
        return 1.0
    if spread_signal.direction == "BUY_YES" and macro.direction == "dovish":
        return 0.0
    if spread_signal.direction == "BUY_NO" and macro.direction == "hawkish":
        return 0.0
    return 0.5


def _sentiment_aligns(spread_signal: SpreadSignal, sentiment_score: float) -> float:
    """Check if Twitter sentiment aligns with spread signal.

    sentiment_score: positive = dovish (cuts), negative = hawkish (hikes)
    Returns 0.0 to 1.0
    """
    if abs(sentiment_score) < 0.05:
        return 0.5  # Neutral sentiment

    if spread_signal.direction == "BUY_YES" and sentiment_score > 0:
        return min(0.5 + abs(sentiment_score), 1.0)
    if spread_signal.direction == "BUY_NO" and sentiment_score < 0:
        return min(0.5 + abs(sentiment_score), 1.0)

    # Contradicts
    return max(0.5 - abs(sentiment_score), 0.0)


async def _get_twitter_sentiment() -> dict:
    """Safely fetch Twitter sentiment (returns empty if fails)."""
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

    Searches Polymarket data for Fed-related outcomes and checks
    if the implied direction matches our signal.
    Returns 0.0 to 1.0
    """
    if not poly_probs:
        return 0.5  # No data = neutral

    # Look for rate cut/hike probabilities in Polymarket
    cut_prob = 0.0
    hike_prob = 0.0
    hold_prob = 0.0

    for key, prob in poly_probs.items():
        key_lower = key.lower()
        if any(w in key_lower for w in ["cut", "lower", "decrease", "reduce"]):
            if "yes" in key_lower:
                cut_prob = max(cut_prob, prob)
        elif any(w in key_lower for w in ["hike", "raise", "increase", "higher"]):
            if "yes" in key_lower:
                hike_prob = max(hike_prob, prob)
        elif any(w in key_lower for w in ["hold", "unchanged", "maintain", "no change"]):
            if "yes" in key_lower:
                hold_prob = max(hold_prob, prob)

    if cut_prob == 0 and hike_prob == 0:
        return 0.5  # Can't determine alignment

    # BUY_YES on "above X%" = expecting higher rates = hawkish = hike_prob should be high
    # BUY_NO on "above X%" = expecting lower rates = dovish = cut_prob should be high
    if spread_signal.direction == "BUY_YES":
        return min(hike_prob + 0.3, 1.0) if hike_prob > cut_prob else 0.3
    else:
        return min(cut_prob + 0.3, 1.0) if cut_prob > hike_prob else 0.3


async def generate_signals(kalshi: KalshiClient) -> list[AggregatedSignal]:
    """Generate aggregated trading signals from ALL data sources.

    Data sources:
    1. FedWatch vs Kalshi probability spread (primary alpha)
    2. FRED macro indicators (directional confirmation)
    3. Twitter/X sentiment (crowd wisdom)
    4. Polymarket (cross-market validation)
    """

    # 1. Get spread signals (primary alpha — without this, no trade)
    spread_signals = await compute_spread_signals(kalshi)
    if not spread_signals:
        log.info("no_spread_signals")
        return []

    # 2. Get all confirmation signals in parallel
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
        sources = ["fedwatch", "kalshi"]

        # Macro alignment
        macro_align = _macro_aligns(ss, macro)
        if macro.confidence > 0:
            sources.append("fred")

        # Twitter alignment
        sent_align = _sentiment_aligns(ss, sentiment_score)
        if tweet_count > 0:
            sources.append("twitter")

        # Polymarket alignment
        poly_align = _polymarket_confirms(ss, poly_probs)
        if poly_probs:
            sources.append("polymarket")

        # Weighted confidence score
        spread_confidence = min(ss.edge_after_fees / 0.10, 1.0)
        macro_confidence = macro.confidence * macro_align
        sent_confidence = sent_align if tweet_count > 10 else 0.5
        poly_confidence = poly_align

        overall_confidence = (
            WEIGHT_SPREAD * spread_confidence +
            WEIGHT_MACRO * macro_confidence +
            WEIGHT_SENTIMENT * sent_confidence +
            WEIGHT_POLYMARKET * poly_confidence
        )

        # Count how many sources agree (consensus bonus)
        agreeing = sum([
            macro_align > 0.5,
            sent_align > 0.5,
            poly_align > 0.5,
        ])
        if agreeing == 3:
            overall_confidence *= 1.15  # 15% bonus for full consensus
        elif agreeing == 0 and ss.edge_after_fees < 0.05:
            log.info("signal_filtered_no_consensus",
                     ticker=ss.market_ticker, edge=ss.edge_after_fees)
            continue

        overall_confidence = min(overall_confidence, 1.0)

        signal = AggregatedSignal(
            market_ticker=ss.market_ticker,
            direction=ss.direction,
            confidence=round(overall_confidence, 3),
            edge_estimate=ss.edge_after_fees,
            fedwatch_prob=ss.fedwatch_prob,
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
                 direction=signal.direction,
                 confidence=signal.confidence,
                 edge=round(signal.edge_estimate, 3),
                 macro=macro.direction,
                 sentiment=sentiment_score,
                 polymarket=signal.polymarket_agrees,
                 sources=sources,
                 consensus=f"{agreeing}/3")

    # Sort by edge (best opportunities first)
    aggregated.sort(key=lambda s: s.edge_estimate, reverse=True)
    return aggregated
