"""Finnhub API — economic calendar and news sentiment."""

from datetime import date, timedelta

import httpx
import structlog

from config.settings import settings

log = structlog.get_logger()

FINNHUB_BASE = "https://finnhub.io/api/v1"


class FinnhubClient:
    def __init__(self):
        self.api_key = settings.finnhub_api_key
        self.client = httpx.AsyncClient(timeout=15.0)

    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        if not self.api_key:
            return {}
        p = {"token": self.api_key}
        if params:
            p.update(params)
        resp = await self.client.get(f"{FINNHUB_BASE}{path}", params=p)
        resp.raise_for_status()
        return resp.json()

    async def get_economic_calendar(self, days_ahead: int = 7) -> list[dict]:
        """Get upcoming economic events (CPI, jobs, GDP, etc.)."""
        today = date.today()
        end = today + timedelta(days=days_ahead)
        try:
            data = await self._get("/calendar/economic", {
                "from": today.isoformat(),
                "to": end.isoformat(),
            })
            events = data.get("economicCalendar", []) if isinstance(data, dict) else data
            # Filter for US and high-impact events
            fed_relevant = []
            for e in events:
                country = e.get("country", "")
                impact = e.get("impact", "")
                event_name = e.get("event", "").lower()
                if country == "US" and (
                    impact in ("high", "medium") or
                    any(kw in event_name for kw in [
                        "cpi", "nonfarm", "unemployment", "gdp", "pce",
                        "fomc", "fed", "interest rate", "retail sales",
                        "consumer confidence", "ism",
                    ])
                ):
                    fed_relevant.append(e)

            log.info("economic_calendar", total=len(events), fed_relevant=len(fed_relevant))
            return fed_relevant
        except Exception as e:
            log.warning("finnhub_calendar_failed", error=str(e))
            return []

    async def get_news_sentiment(self, category: str = "general") -> dict:
        """Get market news and compute Fed-related sentiment."""
        try:
            news = await self._get("/news", {"category": category})
            if not isinstance(news, list):
                return {"score": 0.0, "articles": 0}

            fed_keywords = ["fed", "fomc", "rate cut", "rate hike", "powell",
                            "federal reserve", "monetary policy", "interest rate"]
            fed_articles = []
            for article in news:
                headline = (article.get("headline", "") + " " + article.get("summary", "")).lower()
                if any(kw in headline for kw in fed_keywords):
                    fed_articles.append(article)

            if not fed_articles:
                return {"score": 0.0, "articles": 0}

            # Simple sentiment from headlines
            dovish_words = ["cut", "ease", "dovish", "pause", "lower", "slow"]
            hawkish_words = ["hike", "hawk", "tighten", "raise", "hot", "inflation"]

            score = 0.0
            for a in fed_articles:
                text = (a.get("headline", "") + " " + a.get("summary", "")).lower()
                d = sum(1 for w in dovish_words if w in text)
                h = sum(1 for w in hawkish_words if w in text)
                if d + h > 0:
                    score += (d - h) / (d + h)

            avg_score = score / len(fed_articles) if fed_articles else 0.0

            log.info("finnhub_news_sentiment",
                     fed_articles=len(fed_articles),
                     score=round(avg_score, 3))

            return {
                "score": round(avg_score, 3),
                "articles": len(fed_articles),
                "headlines": [a.get("headline", "")[:100] for a in fed_articles[:5]],
            }
        except Exception as e:
            log.warning("finnhub_news_failed", error=str(e))
            return {"score": 0.0, "articles": 0}

    async def has_major_release_today(self) -> bool:
        """Check if there's a major economic release today (CPI, jobs, etc.)."""
        events = await self.get_economic_calendar(days_ahead=0)
        return len(events) > 0

    async def close(self):
        await self.client.aclose()


finnhub_client = FinnhubClient()
