"""X/Twitter sentiment analysis for Fed-related tweets."""

import base64
from datetime import datetime, timedelta

import httpx
import structlog

from config.settings import settings

log = structlog.get_logger()

X_API_BASE = "https://api.twitter.com/2"
X_OAUTH2_TOKEN_URL = "https://api.twitter.com/oauth2/token"

# Fed-related search terms — keep queries short to stay within free tier limits
FED_QUERIES = [
    '"federal reserve" OR FOMC OR "rate cut" OR "rate hike" -is:retweet lang:en',
]

# Simple keyword-based sentiment (fast, no ML dependency needed)
BULLISH_WORDS = [
    "cut", "cuts", "dovish", "easing", "pause", "pivot", "accommodation",
    "lower rates", "rate cut", "slash", "stimul", "boost", "rally",
]
BEARISH_WORDS = [
    "hike", "hikes", "hawkish", "tightening", "restrictive", "higher rates",
    "rate hike", "inflation", "overheating", "no cut", "hold steady",
]


def _score_tweet(text: str) -> float:
    """Score a tweet from -1.0 (bearish/hawkish) to +1.0 (bullish/dovish)."""
    text_lower = text.lower()
    bull_count = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear_count = sum(1 for w in BEARISH_WORDS if w in text_lower)

    total = bull_count + bear_count
    if total == 0:
        return 0.0
    return (bull_count - bear_count) / total


class TwitterSentiment:
    def __init__(self):
        self._bearer_token = None
        self._client = None

    async def _get_bearer_token(self) -> str:
        """Get bearer token. Tries multiple auth methods in order."""
        if self._bearer_token:
            return self._bearer_token

        # Method 1: OAuth 2.0 client credentials with OAuth2 Client ID/Secret
        if settings.twitter_oauth2_client_id and settings.twitter_oauth2_client_secret:
            try:
                credentials = base64.b64encode(
                    f"{settings.twitter_oauth2_client_id}:{settings.twitter_oauth2_client_secret}".encode()
                ).decode()

                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://api.twitter.com/2/oauth2/token",
                        headers={
                            "Authorization": f"Basic {credentials}",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        data={"grant_type": "client_credentials"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    self._bearer_token = data["access_token"]
                    log.info("twitter_token_obtained", method="oauth2_client_credentials")
                    return self._bearer_token
            except Exception as e:
                log.warning("twitter_oauth2_cc_failed", error=str(e))

        # Method 2: OAuth 1.0a consumer key/secret -> bearer token
        if settings.twitter_consumer_key and settings.twitter_secret_key:
            try:
                credentials = base64.b64encode(
                    f"{settings.twitter_consumer_key}:{settings.twitter_secret_key}".encode()
                ).decode()

                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        X_OAUTH2_TOKEN_URL,
                        headers={
                            "Authorization": f"Basic {credentials}",
                            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                        },
                        data={"grant_type": "client_credentials"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    self._bearer_token = data["access_token"]
                    log.info("twitter_token_obtained", method="oauth1_to_bearer")
                    return self._bearer_token
            except Exception as e:
                log.warning("twitter_oauth1_bearer_failed", error=str(e))

        # Method 3: Direct bearer token from .env
        if settings.twitter_bearer_token:
            self._bearer_token = settings.twitter_bearer_token
            log.info("twitter_token_obtained", method="env_bearer_token")
            return self._bearer_token

        return ""

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            token = await self._get_bearer_token()
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={"Authorization": f"Bearer {token}"},
            )
        return self._client

    async def search_recent(self, query: str, max_results: int = 10) -> list[dict]:
        """Search recent tweets using X API v2.

        Free tier: ~100 reads/month, so we keep max_results low.
        """
        token = await self._get_bearer_token()
        if not token:
            log.warning("no_twitter_token_available")
            return []

        client = await self._get_client()
        try:
            resp = await client.get(
                f"{X_API_BASE}/tweets/search/recent",
                params={
                    "query": query,
                    "max_results": min(max_results, 10),  # Keep low for free tier
                    "tweet.fields": "created_at,public_metrics,text",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            tweets = data.get("data", [])
            log.info("twitter_search_success", query=query[:50], count=len(tweets))
            return tweets
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                log.warning("twitter_rate_limited")
            elif e.response.status_code == 403:
                log.error("twitter_forbidden", error=e.response.text[:200],
                          hint="Search may not be available on your API tier")
            else:
                log.error("twitter_search_failed", status=e.response.status_code,
                          error=e.response.text[:200])
            return []
        except Exception as e:
            log.error("twitter_search_error", error=str(e))
            return []

    async def get_fed_sentiment(self) -> dict:
        """Get aggregate Fed sentiment from recent tweets."""
        all_tweets = []
        for query in FED_QUERIES:
            tweets = await self.search_recent(query, max_results=10)
            all_tweets.extend(tweets)

        if not all_tweets:
            log.info("no_fed_tweets_found")
            return {
                "score": 0.0, "tweet_count": 0,
                "bullish_pct": 0, "bearish_pct": 0, "neutral_pct": 0,
                "sample_tweets": [],
            }

        # Score each tweet, weight by engagement
        scores = []
        for tweet in all_tweets:
            text = tweet.get("text", "")
            score = _score_tweet(text)
            metrics = tweet.get("public_metrics", {})
            engagement = (metrics.get("like_count", 0) + metrics.get("retweet_count", 0) * 2 + 1)
            weight = min(engagement, 100)
            scores.append((score, weight))

        total_weight = sum(w for _, w in scores)
        avg_score = sum(s * w for s, w in scores) / total_weight if total_weight else 0.0

        bullish = sum(1 for s, _ in scores if s > 0.1) / len(scores)
        bearish = sum(1 for s, _ in scores if s < -0.1) / len(scores)
        neutral = 1.0 - bullish - bearish

        sample = [t.get("text", "")[:140] for t in all_tweets[:5]]

        result = {
            "score": round(avg_score, 3),
            "tweet_count": len(all_tweets),
            "bullish_pct": round(bullish * 100, 1),
            "bearish_pct": round(bearish * 100, 1),
            "neutral_pct": round(neutral * 100, 1),
            "sample_tweets": sample,
        }

        log.info("fed_sentiment",
                 score=result["score"], tweets=result["tweet_count"],
                 bullish=f"{result['bullish_pct']}%", bearish=f"{result['bearish_pct']}%")

        return result

    async def close(self):
        if self._client:
            await self._client.aclose()


twitter_client = TwitterSentiment()
