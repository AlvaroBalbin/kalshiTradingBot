"""Consensus estimate fetching for economic indicators.

Sources:
- FRED: CPI, unemployment, GDP (lagging but reliable)
- Atlanta Fed GDPNow: Real-time GDP nowcast
- Cleveland Fed: CPI nowcast
- BLS/prior releases: Used as baseline for jobless claims, NFP

The key insight: Kalshi markets are binary ("will X be above/below Y?").
We need to estimate the probability distribution around the consensus to
determine if Kalshi prices are mispriced.
"""

from dataclasses import dataclass
from datetime import date

import httpx
import structlog

from data.fred_client import fred_client

log = structlog.get_logger()

_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=15.0)
    return _http


@dataclass
class ConsensusEstimate:
    """Consensus estimate for an economic indicator."""
    event_type: str
    point_estimate: float     # Central estimate
    unit: str                 # "%", "K" (thousands), etc.
    low_range: float | None = None   # Low end of range
    high_range: float | None = None  # High end of range
    source: str = ""          # Where the estimate came from
    confidence: float = 0.5   # How confident we are in this estimate (0-1)


async def get_consensus(event_type: str) -> ConsensusEstimate | None:
    """Get consensus estimate for an economic event type."""
    fetchers = {
        "fomc": _get_fomc_consensus,
        "cpi": _get_cpi_consensus,
        "nfp": _get_nfp_consensus,
        "claims": _get_claims_consensus,
        "gdp": _get_gdp_consensus,
    }
    fetcher = fetchers.get(event_type)
    if fetcher is None:
        log.warning("no_consensus_fetcher", event_type=event_type)
        return None

    try:
        return await fetcher()
    except Exception as e:
        log.error("consensus_fetch_failed", event_type=event_type, error=str(e))
        return None


async def _get_fomc_consensus() -> ConsensusEstimate:
    """FOMC consensus comes from FedWatch (handled separately in fedwatch.py).

    Here we provide the current rate as baseline context.
    """
    fed_rate = fred_client.get_current_fed_rate()
    rate = fed_rate if fed_rate is not None else 4.33  # fallback

    return ConsensusEstimate(
        event_type="fomc",
        point_estimate=rate,
        unit="%",
        low_range=rate - 0.25,
        high_range=rate + 0.25,
        source="FRED/DFF",
        confidence=0.7,
    )


async def _get_cpi_consensus() -> ConsensusEstimate:
    """CPI consensus from Cleveland Fed Nowcast + FRED historical."""
    # Try Cleveland Fed CPI Nowcast
    nowcast = await _fetch_cleveland_cpi_nowcast()
    if nowcast is not None:
        return ConsensusEstimate(
            event_type="cpi",
            point_estimate=nowcast,
            unit="%",
            low_range=nowcast - 0.2,
            high_range=nowcast + 0.2,
            source="Cleveland Fed Nowcast",
            confidence=0.7,
        )

    # Fallback: use FRED historical CPI YoY
    cpi_yoy = fred_client.get_cpi_yoy_change()
    if cpi_yoy is not None:
        return ConsensusEstimate(
            event_type="cpi",
            point_estimate=cpi_yoy,
            unit="%",
            low_range=cpi_yoy - 0.3,
            high_range=cpi_yoy + 0.3,
            source="FRED/CPI (lagged)",
            confidence=0.5,
        )

    return ConsensusEstimate(
        event_type="cpi",
        point_estimate=3.0,  # reasonable default
        unit="%",
        source="default",
        confidence=0.3,
    )


async def _get_nfp_consensus() -> ConsensusEstimate:
    """NFP consensus from FRED recent data + trend."""
    # FRED PAYEMS series gives monthly total nonfarm payrolls
    # The MoM change is the NFP number
    try:
        from fredapi import Fred
        from config.settings import settings
        fred = Fred(api_key=settings.fred_api_key)
        series = fred.get_series("PAYEMS", observation_start=date.today().replace(day=1).replace(
            month=max(1, date.today().month - 6)))
        if series is not None and len(series) >= 3:
            # Average of last 3 months' changes (in thousands)
            changes = series.diff().dropna().tail(3)
            avg_change = float(changes.mean())
            return ConsensusEstimate(
                event_type="nfp",
                point_estimate=avg_change,
                unit="K",
                low_range=avg_change - 50,
                high_range=avg_change + 50,
                source="FRED/PAYEMS (3mo avg)",
                confidence=0.5,
            )
    except Exception as e:
        log.warning("nfp_fred_failed", error=str(e))

    return ConsensusEstimate(
        event_type="nfp",
        point_estimate=180.0,  # reasonable default
        unit="K",
        source="default",
        confidence=0.3,
    )


async def _get_claims_consensus() -> ConsensusEstimate:
    """Jobless claims consensus from FRED recent trend."""
    try:
        from fredapi import Fred
        from config.settings import settings
        fred = Fred(api_key=settings.fred_api_key)
        series = fred.get_series("ICSA", observation_start=date.today().replace(
            month=max(1, date.today().month - 2), day=1))
        if series is not None and len(series) >= 4:
            # 4-week moving average (in thousands)
            recent = series.tail(4)
            avg = float(recent.mean()) / 1000  # Convert to thousands
            last = float(series.iloc[-1]) / 1000
            return ConsensusEstimate(
                event_type="claims",
                point_estimate=last,
                unit="K",
                low_range=avg - 15,
                high_range=avg + 15,
                source="FRED/ICSA (4wk avg)",
                confidence=0.6,
            )
    except Exception as e:
        log.warning("claims_fred_failed", error=str(e))

    return ConsensusEstimate(
        event_type="claims",
        point_estimate=220.0,  # reasonable default
        unit="K",
        source="default",
        confidence=0.3,
    )


async def _get_gdp_consensus() -> ConsensusEstimate:
    """GDP consensus from Atlanta Fed GDPNow."""
    gdpnow = await _fetch_atlanta_gdpnow()
    if gdpnow is not None:
        return ConsensusEstimate(
            event_type="gdp",
            point_estimate=gdpnow,
            unit="%",
            low_range=gdpnow - 0.5,
            high_range=gdpnow + 0.5,
            source="Atlanta Fed GDPNow",
            confidence=0.7,
        )

    # Fallback: FRED GDP
    try:
        from fredapi import Fred
        from config.settings import settings
        fred = Fred(api_key=settings.fred_api_key)
        series = fred.get_series("GDP", observation_start=date.today().replace(
            year=date.today().year - 1))
        if series is not None and len(series) >= 2:
            latest = float(series.iloc[-1])
            prev = float(series.iloc[-2])
            growth = ((latest - prev) / prev) * 400  # annualized quarterly
            return ConsensusEstimate(
                event_type="gdp",
                point_estimate=round(growth, 1),
                unit="%",
                low_range=growth - 1.0,
                high_range=growth + 1.0,
                source="FRED/GDP (lagged)",
                confidence=0.4,
            )
    except Exception as e:
        log.warning("gdp_fred_failed", error=str(e))

    return ConsensusEstimate(
        event_type="gdp",
        point_estimate=2.0,
        unit="%",
        source="default",
        confidence=0.3,
    )


# --- External Nowcast Fetchers ---

async def _fetch_cleveland_cpi_nowcast() -> float | None:
    """Fetch Cleveland Fed Inflation Nowcast.

    The Cleveland Fed publishes daily CPI nowcasts.
    URL format: https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting
    We scrape the JSON API endpoint.
    """
    try:
        url = "https://www.clevelandfed.org/api/inflation-nowcasting/data"
        resp = await _get_http().get(url)
        if resp.status_code == 200:
            data = resp.json()
            # Extract latest CPI nowcast value
            if isinstance(data, dict):
                # Try common response structures
                for key in ("cpiNowcast", "nowcast", "cpi"):
                    if key in data:
                        val = data[key]
                        if isinstance(val, (int, float)):
                            log.info("cleveland_cpi_nowcast", value=val)
                            return float(val)
                        if isinstance(val, list) and val:
                            log.info("cleveland_cpi_nowcast", value=val[-1])
                            return float(val[-1])
    except Exception as e:
        log.warning("cleveland_nowcast_failed", error=str(e))
    return None


async def _fetch_atlanta_gdpnow() -> float | None:
    """Fetch Atlanta Fed GDPNow estimate.

    The Atlanta Fed publishes real-time GDP tracking estimates.
    """
    try:
        url = "https://www.atlantafed.org/-/media/documents/cqer/researchcq/gdpnow/RealGDPTrackingSlides.pdf"
        # Try the JSON data endpoint first
        json_url = "https://www.atlantafed.org/api/cqer/gdpnow"
        resp = await _get_http().get(json_url)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                for key in ("gdpNow", "estimate", "gdp"):
                    if key in data:
                        val = data[key]
                        if isinstance(val, (int, float)):
                            log.info("atlanta_gdpnow", value=val)
                            return float(val)
                        if isinstance(val, list) and val:
                            log.info("atlanta_gdpnow", value=val[-1])
                            return float(val[-1])
    except Exception as e:
        log.warning("atlanta_gdpnow_failed", error=str(e))
    return None


def consensus_to_probability(consensus: ConsensusEstimate,
                             threshold: float,
                             above: bool = True) -> float:
    """Convert a consensus estimate to a probability that the value will be above/below a threshold.

    Uses a simple normal distribution approximation centered on the point estimate.
    The range (low_range, high_range) is treated as ~1.5 standard deviations.

    Args:
        consensus: The consensus estimate
        threshold: The binary threshold (e.g., "above 3.0%")
        above: If True, return P(value >= threshold). If False, P(value < threshold).

    Returns:
        Probability between 0.01 and 0.99
    """
    import math

    mu = consensus.point_estimate

    # Estimate std dev from range
    if consensus.high_range is not None and consensus.low_range is not None:
        range_width = consensus.high_range - consensus.low_range
        sigma = range_width / 3.0  # ~1.5 std devs each side
    else:
        # Default: assume moderate uncertainty
        sigma = abs(mu) * 0.1 if mu != 0 else 1.0

    if sigma <= 0:
        sigma = 0.01

    # Standard normal CDF approximation
    z = (threshold - mu) / sigma
    # Use error function for CDF
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    prob = 1.0 - cdf if above else cdf
    # Clamp to reasonable range
    return max(0.01, min(0.99, prob))
