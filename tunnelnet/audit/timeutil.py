"""UTC time helpers shared by the live journal and the archiver."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DAY_FORMAT = "%Y-%m-%d"


def utc_now() -> str:
    """Current UTC time in the compact form used inside audit records."""

    return time.strftime(STAMP_FORMAT, time.gmtime())


def day_of(stamp: str) -> str:
    """UTC calendar day (``YYYY-MM-DD``) an audit stamp belongs to."""

    return stamp[:10]


def today(clock=utc_now) -> str:
    return day_of(clock())


def parse_day(day: str) -> datetime:
    return datetime.strptime(day, DAY_FORMAT).replace(tzinfo=timezone.utc)


def shift_day(day: str, days: int) -> str:
    return (parse_day(day) + timedelta(days=days)).strftime(DAY_FORMAT)


def days_between(older: str, newer: str) -> int:
    """Whole UTC day boundary count from ``older`` to ``newer``."""

    return (parse_day(newer) - parse_day(older)).days
