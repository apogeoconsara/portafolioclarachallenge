"""Send window: Mon-Fri 09:00-18:00 recipient local time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .policy import Policy

WINDOW_DAYS = (0, 1, 2, 3, 4)
START, END = 9, 18


def next_send_time(now: datetime, country: str, policy: Policy) -> datetime:
    """Earliest instant >= now inside the recipient's window."""
    off = timedelta(hours=policy.utc_offset(country))
    local = now.astimezone(timezone.utc) + off
    for _ in range(10):
        start = local.replace(hour=START, minute=0, second=0, microsecond=0)
        end = local.replace(hour=END, minute=0, second=0, microsecond=0)
        if local.weekday() in WINDOW_DAYS:
            if start <= local < end:
                return (local - off).replace(tzinfo=timezone.utc)
            if local < start:
                return (start - off).replace(tzinfo=timezone.utc)
        local = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    raise RuntimeError("no send window found")


def next_day_window(now: datetime, country: str, policy: Policy) -> datetime:
    """Start of the next window day (used when the daily cap is reached)."""
    off = timedelta(hours=policy.utc_offset(country))
    local = now.astimezone(timezone.utc) + off
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - off
    return next_send_time(tomorrow.replace(tzinfo=timezone.utc), country, policy)
