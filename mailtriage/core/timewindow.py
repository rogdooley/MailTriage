from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Window:
    label_date: str  # YYYY-MM-DD in local calendar
    start_utc: str  # ISO Z
    end_utc: str  # ISO Z


def compute_windows(
    *,
    timezone: str,
    workday_start: str,  # HH:MM
    days: int | None,
    date: str | None,
) -> list[Window]:
    tz = ZoneInfo(timezone)
    hh, mm = _parse_hhmm(workday_start)

    if date is not None:
        local_day = datetime.strptime(date, "%Y-%m-%d").date()
        return [_window_for_day(local_day, tz, hh, mm)]

    n = 1 if days is None else days
    if n <= 0:
        raise ValueError("--days must be >= 1")

    today_local = datetime.now(tz).date()
    days_list = [today_local - timedelta(days=i) for i in range(n)]
    # oldest -> newest for deterministic processing
    days_list.reverse()

    return [_window_for_day(d, tz, hh, mm) for d in days_list]


def _window_for_day(d, tz: ZoneInfo, hh: int, mm: int) -> Window:
    start_local = datetime.combine(d, time(hh, mm), tzinfo=tz)
    end_local = start_local + timedelta(days=1)

    start_utc = start_local.astimezone(ZoneInfo("UTC"))
    end_utc = end_local.astimezone(ZoneInfo("UTC"))

    return Window(
        label_date=d.isoformat(),
        start_utc=start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc=end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError("workday_start must be HH:MM")
    hh = int(parts[0])
    mm = int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("workday_start out of range")
    return hh, mm
