from __future__ import annotations

import random
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .config import Config


def _nth_weekday_of_month(d: datetime, weekday: int, n: int) -> int:
    first = d.replace(day=1)
    offset = (weekday - first.weekday()) % 7
    return 1 + offset + (n - 1) * 7


def in_burst_window(cfg: Config, now: datetime | None = None) -> bool:
    b = cfg.burst
    if not b.enabled:
        return False
    tz = ZoneInfo(b.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    if now.weekday() != b.weekday or now.day != _nth_weekday_of_month(now, b.weekday, b.nth_week):
        return False
    return time.fromisoformat(b.start) <= now.time() <= time.fromisoformat(b.end)


def in_run_window(cfg: Config, now: datetime | None = None) -> tuple[bool, str]:
    """(allowed, reason). Checks date range, weekdays and hours from cfg.schedule."""
    sc = cfg.schedule
    tz = ZoneInfo(sc.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    if sc.date_from and now.date() < date.fromisoformat(sc.date_from):
        return False, f"starts {sc.date_from}"
    if sc.date_to and now.date() > date.fromisoformat(sc.date_to):
        return False, f"ended {sc.date_to}"
    if now.weekday() not in sc.days:
        return False, "not an active weekday"
    t0, t1 = time.fromisoformat(sc.hours_from or "00:00"), time.fromisoformat(sc.hours_to or "23:59")
    if not (t0 <= now.time() <= t1):
        return False, f"outside {sc.hours_from}-{sc.hours_to}"
    return True, "active"


def next_delay(cfg: Config) -> float:
    if in_burst_window(cfg):
        base, jitter = cfg.burst.interval_seconds, min(cfg.jitter_seconds, cfg.burst.interval_seconds // 3)
    else:
        base, jitter = cfg.interval_seconds, min(cfg.jitter_seconds, cfg.interval_seconds // 3)
    return max(10.0, base + random.uniform(-jitter, jitter))
