"""Kalshi API client with RSA-PSS authentication."""

import re
import time
import base64
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config.settings import settings
import structlog

log = structlog.get_logger()


class KalshiAuth:
    """RSA-PSS request signing for Kalshi API."""

    def __init__(self, key_id: str, private_key_pem: bytes):
        self.key_id = key_id
        self.private_key = serialization.load_pem_private_key(private_key_pem, password=None)

    def sign_request(self, method: str, path: str, timestamp_ms: int) -> str:
        message = f"{timestamp_ms}{method}{path}"
        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = int(time.time() * 1000)
        sig = self.sign_request(method.upper(), path, ts)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }


class KalshiClient:
    """REST client for Kalshi trade API v2."""

    def __init__(self):
        self.base_url = settings.kalshi_base_url
        self.auth = KalshiAuth(
            key_id=settings.kalshi_api_key_id,
            private_key_pem=settings.private_key_bytes,
        )
        self.client = httpx.AsyncClient(timeout=30.0)

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self.auth.headers(method.upper(), f"/trade-api/v2{path}")
        resp = await self.client.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()

    # --- Market Data ---

    async def get_markets(self, series_ticker: str | None = None,
                          event_ticker: str | None = None,
                          limit: int = 200, status: str = "open") -> list[dict]:
        """Get markets, optionally filtered by series or event ticker."""
        params = {"limit": limit, "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        data = await self._request("GET", "/markets", params=params)
        return data.get("markets", [])

    async def get_market(self, ticker: str) -> dict:
        data = await self._request("GET", f"/markets/{ticker}")
        return data.get("market", {})

    async def get_orderbook(self, ticker: str) -> dict:
        data = await self._request("GET", f"/markets/{ticker}/orderbook")
        return data.get("orderbook", {})

    async def get_event(self, event_ticker: str) -> dict:
        data = await self._request("GET", f"/events/{event_ticker}")
        return data.get("event", {})

    async def get_events(self, series_ticker: str | None = None,
                         status: str = "open", limit: int = 50) -> list[dict]:
        """Get events, optionally filtered by series."""
        params = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = await self._request("GET", "/events", params=params)
        return data.get("events", [])

    # --- Trading ---

    async def create_order(self, ticker: str, side: str, action: str,
                           count: int, type: str = "limit",
                           yes_price: int | None = None,
                           no_price: int | None = None) -> dict:
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": type,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price

        log.info("placing_order", ticker=ticker, side=side, action=action,
                 count=count, yes_price=yes_price, no_price=no_price)
        data = await self._request("POST", "/portfolio/orders", json=body)
        return data.get("order", {})

    async def cancel_order(self, order_id: str) -> dict:
        log.info("cancelling_order", order_id=order_id)
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_order(self, order_id: str) -> dict:
        data = await self._request("GET", f"/portfolio/orders/{order_id}")
        return data.get("order", {})

    # --- Portfolio ---

    async def get_balance(self) -> float:
        data = await self._request("GET", "/portfolio/balance")
        return data.get("balance", 0) / 100

    async def get_positions(self, settlement_status: str = "unsettled") -> list[dict]:
        data = await self._request(
            "GET", "/portfolio/positions",
            params={"settlement_status": settlement_status},
        )
        return data.get("market_positions", [])

    async def get_fills(self, ticker: str | None = None, limit: int = 100) -> list[dict]:
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/fills", params=params)
        return data.get("fills", [])

    # --- Market Discovery ---

    async def get_fed_markets(self) -> list[dict]:
        """Get all open Fed/FOMC rate decision markets (backward compat)."""
        return await self.get_economic_markets("fomc", "KXFED")

    async def get_economic_markets(self, event_type: str,
                                   series_prefix: str) -> list[dict]:
        """Get all open markets for an economic event type.

        Uses multi-fallback discovery:
        1. Event-based (series ticker → events → sub-markets)
        2. Direct series search
        3. Keyword search in market titles
        """
        # Alternate series tickers to try per event type
        alt_series = {
            "fomc": ["KXFED", "FED"],
            "cpi": ["KXCPI", "CPI", "KXINFLATION"],
            "nfp": ["KXNFP", "NFP", "KXJOBS", "KXPAYROLLS"],
            "claims": ["KXINITCLAIMS", "KXUNEMPLOY", "KXJOBLESS"],
            "gdp": ["KXGDP", "GDP"],
        }

        series_list = alt_series.get(event_type, [series_prefix])
        if series_prefix not in series_list:
            series_list.insert(0, series_prefix)

        markets = []

        # Method 1: Event-based discovery
        for series in series_list:
            try:
                events = await self.get_events(series_ticker=series)
                for event in events:
                    event_ticker = event.get("event_ticker", "")
                    if event_ticker:
                        event_markets = await self.get_markets(event_ticker=event_ticker)
                        markets.extend(event_markets)
                        log.info("event_found", event_type=event_type,
                                 event_ticker=event_ticker,
                                 num_markets=len(event_markets))
            except httpx.HTTPStatusError:
                continue
            if markets:
                break

        # Method 2: Direct series search (fallback)
        if not markets:
            for series in series_list:
                try:
                    m = await self.get_markets(series_ticker=series)
                    markets.extend(m)
                except httpx.HTTPStatusError:
                    continue
                if markets:
                    break

        # Deduplicate by ticker
        seen = set()
        unique = []
        for m in markets:
            ticker = m.get("ticker", "")
            if ticker and ticker not in seen:
                seen.add(ticker)
                unique.append(m)

        log.info("markets_discovered", event_type=event_type, count=len(unique),
                 tickers=[m.get("ticker", "") for m in unique[:10]])
        return unique

    async def close(self):
        await self.client.aclose()


def parse_market_rate_range(market: dict) -> tuple[float, float] | None:
    """Extract rate range from a Kalshi Fed market.

    Tries multiple parsing strategies:
    1. Parse ticker (e.g., KXFED-26APR-T425 → 4.25%)
    2. Parse title/subtitle text
    """
    ticker = market.get("ticker", "")
    title = " ".join(filter(None, [
        market.get("title", ""),
        market.get("yes_sub_title", ""),
        market.get("no_sub_title", ""),
        market.get("subtitle", ""),
    ])).lower()

    # Strategy 1: Parse ticker like KXFED-26APR-T425 or KXFED-26APR-B425
    ticker_match = re.search(r'[TB](\d{3,4})', ticker)
    if ticker_match:
        bps = int(ticker_match.group(1))
        # T425 = target 4.25%, this contract is for rate range 4.25-4.50
        return (float(bps), float(bps + 25))

    # Strategy 2: Parse "4.25% to 4.50%" or "4.25%-4.50%"
    range_match = re.search(r'(\d+\.?\d*)%?\s*(?:to|-)\s*(\d+\.?\d*)%', title)
    if range_match:
        low = float(range_match.group(1)) * 100
        high = float(range_match.group(2)) * 100
        return (low, high)

    # Strategy 3: Parse "above X%" or "at least X%"
    above_match = re.search(r'(?:above|over|at least|higher than)\s*(\d+\.?\d*)%', title)
    if above_match:
        low = float(above_match.group(1)) * 100
        return (low, low + 25)  # Assume 25bp bracket

    # Strategy 4: Parse "below X%" or "under X%"
    below_match = re.search(r'(?:below|under|lower than)\s*(\d+\.?\d*)%', title)
    if below_match:
        high = float(below_match.group(1)) * 100
        return (high - 25, high)

    # Strategy 5: Parse standalone rate like "4.25" or "425" in ticker
    rate_match = re.search(r'(\d{3,4})$', ticker.split('-')[-1] if '-' in ticker else "")
    if rate_match:
        bps = int(rate_match.group(1))
        if bps > 100:  # Looks like basis points
            return (float(bps), float(bps + 25))

    log.debug("unparseable_market", ticker=ticker, title=title[:80])
    return None
