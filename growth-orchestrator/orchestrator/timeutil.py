from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(FMT)


def valid_iso(s) -> bool:
    try:
        parse(s)
        return True
    except (TypeError, ValueError):
        return False


__all__ = ["UTC", "FMT", "parse", "iso", "valid_iso", "timedelta", "datetime"]
