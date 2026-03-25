from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# 2026 FOMC meeting dates (announcement is day 2 at 2:00 PM ET)
FOMC_MEETINGS_2026 = [
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 9),
]

ANNOUNCEMENT_TIME = time(14, 0)  # 2:00 PM ET
PRESS_CONF_TIME = time(14, 30)   # 2:30 PM ET


def get_next_fomc_date(from_date: date | None = None) -> date | None:
    today = from_date or date.today()
    for d in FOMC_MEETINGS_2026:
        if d >= today:
            return d
    return None


def get_previous_fomc_date(from_date: date | None = None) -> date | None:
    today = from_date or date.today()
    prev = None
    for d in FOMC_MEETINGS_2026:
        if d < today:
            prev = d
        else:
            break
    return prev


def is_fomc_week(from_date: date | None = None) -> bool:
    today = from_date or date.today()
    next_fomc = get_next_fomc_date(today)
    if next_fomc is None:
        return False
    return (next_fomc - today).days <= 7


def is_fomc_day(from_date: date | None = None) -> bool:
    today = from_date or date.today()
    return today in FOMC_MEETINGS_2026


def get_announcement_datetime(meeting_date: date) -> datetime:
    return datetime.combine(meeting_date, ANNOUNCEMENT_TIME, tzinfo=ET)


def get_blackout_window(meeting_date: date, pre_minutes: int = 5, post_minutes: int = 15) -> tuple[datetime, datetime]:
    announcement = get_announcement_datetime(meeting_date)
    return (
        announcement - timedelta(minutes=pre_minutes),
        announcement + timedelta(minutes=post_minutes),
    )


def is_in_blackout(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    today = now.date()
    if not is_fomc_day(today):
        return False
    start, end = get_blackout_window(today)
    return start <= now <= end


def days_to_next_fomc(from_date: date | None = None) -> int | None:
    today = from_date or date.today()
    next_fomc = get_next_fomc_date(today)
    if next_fomc is None:
        return None
    return (next_fomc - today).days
