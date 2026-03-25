"""FRED API client for macro indicators."""

from datetime import date, timedelta

import structlog

from config.settings import settings

log = structlog.get_logger()

# Key FRED series for rate decision context
SERIES = {
    "DFF": "Federal Funds Effective Rate (daily)",
    "FEDFUNDS": "Federal Funds Rate (monthly)",
    "T10Y2Y": "10-Year Treasury Minus 2-Year (yield curve)",
    "UNRATE": "Unemployment Rate",
    "CPIAUCSL": "Consumer Price Index (all urban)",
}


class FredClient:
    def __init__(self):
        self._fred = None

    @property
    def fred(self):
        if self._fred is None:
            from fredapi import Fred
            self._fred = Fred(api_key=settings.fred_api_key)
        return self._fred

    def get_current_fed_rate(self) -> float | None:
        """Get the most recent effective federal funds rate."""
        try:
            series = self.fred.get_series("DFF", observation_start=date.today() - timedelta(days=14))
            if series.empty:
                return None
            return float(series.dropna().iloc[-1])
        except Exception as e:
            log.error("fred_fetch_failed", series="DFF", error=str(e))
            return None

    def get_yield_curve_spread(self) -> float | None:
        """Get the 10Y-2Y Treasury spread (negative = inverted curve)."""
        try:
            series = self.fred.get_series("T10Y2Y", observation_start=date.today() - timedelta(days=14))
            if series.empty:
                return None
            return float(series.dropna().iloc[-1])
        except Exception as e:
            log.error("fred_fetch_failed", series="T10Y2Y", error=str(e))
            return None

    def get_unemployment_rate(self) -> float | None:
        """Get latest unemployment rate."""
        try:
            series = self.fred.get_series("UNRATE", observation_start=date.today() - timedelta(days=90))
            if series.empty:
                return None
            return float(series.dropna().iloc[-1])
        except Exception as e:
            log.error("fred_fetch_failed", series="UNRATE", error=str(e))
            return None

    def get_cpi_yoy_change(self) -> float | None:
        """Get year-over-year CPI change (inflation proxy)."""
        try:
            series = self.fred.get_series("CPIAUCSL", observation_start=date.today() - timedelta(days=400))
            if series.empty or len(series) < 13:
                return None
            latest = float(series.dropna().iloc[-1])
            year_ago = float(series.dropna().iloc[-13])  # ~12 months ago
            return round((latest - year_ago) / year_ago * 100, 2)
        except Exception as e:
            log.error("fred_fetch_failed", series="CPIAUCSL", error=str(e))
            return None

    def get_macro_snapshot(self) -> dict:
        """Get all macro indicators in one call."""
        return {
            "fed_rate": self.get_current_fed_rate(),
            "yield_curve_spread": self.get_yield_curve_spread(),
            "unemployment": self.get_unemployment_rate(),
            "cpi_yoy": self.get_cpi_yoy_change(),
        }


fred_client = FredClient()
