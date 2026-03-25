"""Polymarket API client — free, no auth needed. Cross-reference Fed probabilities."""

import json
import httpx
import structlog

log = structlog.get_logger()

POLYMARKET_API = "https://gamma-api.polymarket.com"

FED_KEYWORDS = [
    "fed", "fomc", "federal reserve", "interest rate", "rate cut", "rate hike",
    "powell", "monetary policy", "basis point", "fed funds",
]


async def get_fed_markets() -> list[dict]:
    """Search Polymarket for Fed/FOMC related markets."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        results = []

        # Search with text queries
        for query in ["fed rate", "FOMC", "federal reserve", "interest rate"]:
            try:
                resp = await client.get(
                    f"{POLYMARKET_API}/markets",
                    params={"closed": "false", "limit": 100, "active": "true",
                            "ascending": "false", "order": "volume"},
                )
                resp.raise_for_status()
                all_markets = resp.json()
                if isinstance(all_markets, list):
                    for m in all_markets:
                        text = (
                            m.get("question", "") + " " +
                            m.get("description", "") + " " +
                            " ".join(m.get("tags", []) if isinstance(m.get("tags"), list) else [])
                        ).lower()
                        if any(kw in text for kw in FED_KEYWORDS):
                            results.append(m)
            except Exception as e:
                log.warning("polymarket_fetch_failed", query=query, error=str(e))
                break  # Don't spam if API is down

        # Deduplicate
        seen = set()
        unique = []
        for m in results:
            mid = m.get("id") or m.get("condition_id", "")
            if mid and mid not in seen:
                seen.add(mid)
                unique.append(m)

        log.info("polymarket_fed_markets", count=len(unique))
        for m in unique[:5]:
            log.info("polymarket_market", question=m.get("question", "")[:80])
        return unique


async def get_polymarket_probabilities() -> dict[str, float]:
    """Get Polymarket implied probabilities for Fed-related outcomes."""
    markets = await get_fed_markets()
    probs = {}

    for m in markets:
        question = m.get("question", "Unknown")
        prices_str = m.get("outcomePrices", "")
        outcomes = m.get("outcomes", "")

        if prices_str and outcomes:
            try:
                prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
                outcomes_list = json.loads(outcomes) if isinstance(outcomes, str) else outcomes

                for outcome, price in zip(outcomes_list, prices):
                    key = f"{question} | {outcome}"
                    probs[key] = float(price)
                    log.info("polymarket_prob", question=question[:60],
                             outcome=outcome, prob=float(price))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue

    return probs
