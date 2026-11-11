"""Time zones and bank holidays."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import holidays as _holidays
except ImportError:  # optional
    _holidays = None


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now(tz: str) -> datetime:
    return datetime.now(zone(tz))


def local_today(tz: str) -> date:
    return local_now(tz).date()


def to_utc_naive(d: date, hour: int, minute: int, tz: str) -> datetime:
    """A local wall-clock time in `tz` -> naive UTC for storage."""
    local = datetime.combine(d, time(hour, minute), tzinfo=zone(tz))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


@lru_cache(maxsize=32)
def _hol(country: str, year: int):
    if not _holidays or not country:
        return {}
    try:
        return _holidays.country_holidays(country.upper(), years=[year])
    except Exception:
        return {}


def holiday_name(d: date, country: str) -> str | None:
    return _hol(country, d.year).get(d)


def is_workday(d: date, country: str, weekends_ok: bool, holidays_on: bool) -> bool:
    if not weekends_ok and d.weekday() >= 5:
        return False
    if holidays_on and holiday_name(d, country):
        return False
    return True


def upcoming_holidays(start: date, days: int, country: str) -> list[tuple[date, str]]:
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        n = holiday_name(d, country)
        if n:
            out.append((d, n))
    return out


def hhmm(s: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = str(s).split(":")[:2]
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return default
