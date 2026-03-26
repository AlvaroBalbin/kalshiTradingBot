"""CME FedWatch probability extraction — proper methodology using Fed funds futures."""

from datetime import date, timedelta

import yfinance as yf
import structlog

from config.economic_calendar import FOMC_DATES_2026 as FOMC_MEETINGS_2026, get_next_fomc_date

log = structlog.get_logger()

MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}

# Current Fed Funds target rate range (update as Fed changes it)
CURRENT_TARGET_LOW = 4.25
CURRENT_TARGET_HIGH = 4.50
RATE_INCREMENT = 0.25


def _futures_ticker(month: int, year: int) -> str:
    code = MONTH_CODES[month]
    yr = str(year)[-2:]
    return f"ZQ{code}{yr}.CBT"


def _get_futures_price(ticker: str) -> float | None:
    try:
        data = yf.Ticker(ticker)
        hist = data.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.warning("futures_fetch_failed", ticker=ticker, error=str(e))
        return None


def _implied_rate(futures_price: float) -> float:
    """Futures settle at 100 - monthly avg effective fed funds rate."""
    return 100.0 - futures_price


def compute_fedwatch_probabilities(meeting_date: date) -> dict[str, float]:
    """Compute rate probabilities using proper CME FedWatch methodology.

    The key insight: use the meeting month futures AND the prior month futures
    to isolate the probability of a rate change at THIS specific meeting.

    If the meeting is early in the month, the prior month's futures
    represent the pre-meeting rate. The meeting month's futures represent
    a weighted average of pre-meeting days at old rate + post-meeting days at new rate.

    For simplicity (and because most FOMC meetings are mid-to-late month),
    we use: P(cut) = (current_target_mid - implied_rate) / 0.25
    """
    meeting_month = meeting_date.month
    meeting_year = meeting_date.year

    # Get meeting month futures
    meeting_ticker = _futures_ticker(meeting_month, meeting_year)
    meeting_price = _get_futures_price(meeting_ticker)

    if meeting_price is None:
        log.error("cannot_get_meeting_futures", ticker=meeting_ticker)
        return {}

    implied = _implied_rate(meeting_price)

    # Also try prior month for better isolation
    if meeting_month == 1:
        prior_month, prior_year = 12, meeting_year - 1
    else:
        prior_month, prior_year = meeting_month - 1, meeting_year

    prior_ticker = _futures_ticker(prior_month, prior_year)
    prior_price = _get_futures_price(prior_ticker)
    prior_implied = _implied_rate(prior_price) if prior_price else None

    # Use prior month to establish "starting rate" if available
    starting_rate = prior_implied if prior_implied else (CURRENT_TARGET_LOW + CURRENT_TARGET_HIGH) / 2

    log.info("fedwatch_data",
             meeting=str(meeting_date),
             meeting_ticker=meeting_ticker,
             meeting_price=meeting_price,
             implied_rate=round(implied, 4),
             prior_implied=round(prior_implied, 4) if prior_implied else None,
             starting_rate=round(starting_rate, 4))

    # CME FedWatch formula:
    # The implied rate from futures tells us the market's expected rate after the meeting.
    # P(no change) = 1 - |implied - starting_rate| / 0.25
    # P(25bp cut) = max(0, (starting_rate - implied) / 0.25)
    # P(25bp hike) = max(0, (implied - starting_rate) / 0.25)

    rate_diff = implied - starting_rate
    probabilities = {}

    # Generate possible outcomes: -50bp, -25bp, hold, +25bp, +50bp
    outcomes = [
        (-0.50, "double_cut"),
        (-0.25, "cut_25"),
        (0.00, "hold"),
        (+0.25, "hike_25"),
        (+0.50, "double_hike"),
    ]

    for change, label in outcomes:
        target_low = CURRENT_TARGET_LOW + change
        target_high = CURRENT_TARGET_HIGH + change
        target_mid = (target_low + target_high) / 2

        # Probability based on distance from implied rate
        distance = abs(implied - target_mid)

        if distance < RATE_INCREMENT:
            # Linear interpolation between this outcome and adjacent
            prob = 1.0 - (distance / RATE_INCREMENT)
        else:
            prob = 0.0

        if prob > 0.005:
            key = f"{target_low:.2f}-{target_high:.2f}"
            probabilities[key] = round(prob, 4)

    # Normalize to sum to 1.0
    total = sum(probabilities.values())
    if total > 0:
        probabilities = {k: round(v / total, 4) for k, v in probabilities.items()}

    log.info("fedwatch_probabilities", meeting=str(meeting_date),
             implied_rate=round(implied, 4), probs=probabilities)
    return probabilities


def get_next_meeting_probabilities() -> tuple[date | None, dict[str, float]]:
    next_meeting = get_next_fomc_date()
    if next_meeting is None:
        return None, {}
    probs = compute_fedwatch_probabilities(next_meeting)
    return next_meeting, probs


def rate_range_to_bps(rate_range: str) -> tuple[int, int]:
    parts = rate_range.split("-")
    return (int(float(parts[0]) * 100), int(float(parts[1]) * 100))


def bps_to_rate_range(low_bps: int, high_bps: int) -> str:
    return f"{low_bps / 100:.2f}-{high_bps / 100:.2f}"
