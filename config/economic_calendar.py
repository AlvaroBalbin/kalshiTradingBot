"""Economic event calendar — tracks FOMC, CPI, NFP, Jobless Claims, GDP releases."""

from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import structlog

log = structlog.get_logger()

ET = ZoneInfo("America/New_York")


@dataclass
class EconomicEvent:
    name: str           # Human-readable name
    event_type: str     # Internal key: "fomc", "cpi", "nfp", "claims", "gdp"
    date: date          # Release date
    release_time: time  # Typical release time (ET)
    series_prefix: str  # Kalshi series/event ticker prefix
    blackout_pre_min: int = 5    # Minutes before release to stop trading
    blackout_post_min: int = 15  # Minutes after release to resume


# --- 2026 Economic Release Dates ---

# FOMC meetings (announcement at 2:00 PM ET)
FOMC_DATES_2026 = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
]

# CPI releases (~13th of each month, 8:30 AM ET)
CPI_DATES_2026 = [
    date(2026, 1, 14), date(2026, 2, 12), date(2026, 3, 11), date(2026, 4, 14),
    date(2026, 5, 13), date(2026, 6, 10), date(2026, 7, 14), date(2026, 8, 12),
    date(2026, 9, 11), date(2026, 10, 13), date(2026, 11, 12), date(2026, 12, 10),
]

# Nonfarm Payrolls (1st Friday of each month, 8:30 AM ET)
NFP_DATES_2026 = [
    date(2026, 1, 9), date(2026, 2, 6), date(2026, 3, 6), date(2026, 4, 3),
    date(2026, 5, 8), date(2026, 6, 5), date(2026, 7, 2), date(2026, 8, 7),
    date(2026, 9, 4), date(2026, 10, 2), date(2026, 11, 6), date(2026, 12, 4),
]

# Initial Jobless Claims (every Thursday, 8:30 AM ET)
# Generate all Thursdays in 2026
def _thursdays_in_year(year: int) -> list[date]:
    d = date(year, 1, 1)
    # Advance to first Thursday
    while d.weekday() != 3:  # 3 = Thursday
        d += timedelta(days=1)
    dates = []
    while d.year == year:
        dates.append(d)
        d += timedelta(days=7)
    return dates

CLAIMS_DATES_2026 = _thursdays_in_year(2026)

# GDP (advance estimate ~last week of month after quarter ends, 8:30 AM ET)
GDP_DATES_2026 = [
    date(2026, 1, 29),   # Q4 2025 advance
    date(2026, 4, 29),   # Q1 2026 advance
    date(2026, 7, 29),   # Q2 2026 advance
    date(2026, 10, 29),  # Q3 2026 advance
]


def _build_events() -> list[EconomicEvent]:
    """Build the full event list for all tracked economic releases."""
    events = []

    for d in FOMC_DATES_2026:
        events.append(EconomicEvent(
            name="FOMC Rate Decision",
            event_type="fomc",
            date=d,
            release_time=time(14, 0),  # 2:00 PM ET
            series_prefix="KXFED",
            blackout_pre_min=5,
            blackout_post_min=15,
        ))

    for d in CPI_DATES_2026:
        events.append(EconomicEvent(
            name="CPI Inflation",
            event_type="cpi",
            date=d,
            release_time=time(8, 30),
            series_prefix="KXCPI",
            blackout_pre_min=5,
            blackout_post_min=10,
        ))

    for d in NFP_DATES_2026:
        events.append(EconomicEvent(
            name="Nonfarm Payrolls",
            event_type="nfp",
            date=d,
            release_time=time(8, 30),
            series_prefix="KXNFP",
            blackout_pre_min=5,
            blackout_post_min=10,
        ))

    for d in CLAIMS_DATES_2026:
        events.append(EconomicEvent(
            name="Initial Jobless Claims",
            event_type="claims",
            date=d,
            release_time=time(8, 30),
            series_prefix="KXINITCLAIMS",
            blackout_pre_min=3,
            blackout_post_min=10,
        ))

    for d in GDP_DATES_2026:
        events.append(EconomicEvent(
            name="GDP Growth Rate",
            event_type="gdp",
            date=d,
            release_time=time(8, 30),
            series_prefix="KXGDP",
            blackout_pre_min=5,
            blackout_post_min=10,
        ))

    events.sort(key=lambda e: e.date)
    return events


ALL_EVENTS = _build_events()


def get_upcoming_events(within_days: int = 7,
                        from_date: date | None = None) -> list[EconomicEvent]:
    """Get events happening within the next N days."""
    today = from_date or date.today()
    cutoff = today + timedelta(days=within_days)
    return [e for e in ALL_EVENTS if today <= e.date <= cutoff]


def get_next_event(event_type: str | None = None,
                   from_date: date | None = None) -> EconomicEvent | None:
    """Get the next upcoming event, optionally filtered by type."""
    today = from_date or date.today()
    for e in ALL_EVENTS:
        if e.date >= today:
            if event_type is None or e.event_type == event_type:
                return e
    return None


def days_to_next_event(event_type: str | None = None,
                       from_date: date | None = None) -> int | None:
    """Days until the next event of given type (or any event)."""
    today = from_date or date.today()
    e = get_next_event(event_type, today)
    if e is None:
        return None
    return (e.date - today).days


def is_event_day(from_date: date | None = None) -> list[EconomicEvent]:
    """Return all events happening on the given date."""
    today = from_date or date.today()
    return [e for e in ALL_EVENTS if e.date == today]


def is_in_any_blackout(now: datetime | None = None) -> tuple[bool, str]:
    """Check if current time is in any event's blackout window.

    Returns (is_blacked_out, reason).
    """
    now = now or datetime.now(ET)
    today = now.date()
    for e in ALL_EVENTS:
        if e.date != today:
            continue
        release_dt = datetime.combine(e.date, e.release_time, tzinfo=ET)
        start = release_dt - timedelta(minutes=e.blackout_pre_min)
        end = release_dt + timedelta(minutes=e.blackout_post_min)
        if start <= now <= end:
            return True, f"{e.name} blackout ({e.release_time.strftime('%H:%M')} ET)"
    return False, ""


# --- Backward compatibility with fomc_calendar.py ---

def get_next_fomc_date(from_date: date | None = None) -> date | None:
    e = get_next_event("fomc", from_date)
    return e.date if e else None


def days_to_next_fomc(from_date: date | None = None) -> int | None:
    return days_to_next_event("fomc", from_date)


def is_fomc_week(from_date: date | None = None) -> bool:
    days = days_to_next_fomc(from_date)
    return days is not None and days <= 7


def is_fomc_day(from_date: date | None = None) -> bool:
    today = from_date or date.today()
    return today in [d for d in FOMC_DATES_2026]


def is_in_blackout(now: datetime | None = None) -> bool:
    blacked, _ = is_in_any_blackout(now)
    return blacked
