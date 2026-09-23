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


def public_delay(cfg: Config, age_minutes: float | None) -> float:
    """VFS republishes the earliest-date data about once an hour and tells us how old the copy is.
    Polling every 5 minutes therefore re-reads the same numbers ~11 times for nothing while adding
    ~280 requests a day to this IP's tally — which is what gets the IP rate-limited (429201).
    Instead: wait until that copy is about to be replaced, then look.
    """
    base = max(60, cfg.public_interval_seconds)
    if age_minutes is None or age_minutes < 0 or age_minutes > 300:
        return base + random.uniform(-cfg.public_jitter_seconds, cfg.public_jitter_seconds)
    if age_minutes >= 57:                       # due or late — check again soon
        return random.uniform(300, 480)
    wait = (60 - age_minutes + 2) * 60          # just after VFS's next refresh
    return max(base, wait) + random.uniform(0, cfg.public_jitter_seconds)


def next_delay(cfg: Config) -> float:
    """Public mode polls a plain API often; login mode runs one account per round and then lets
    that account rest, so the gap between rounds is simply `rotation.account_rest_hours`."""
    if cfg.mode == "login" and cfg.rotation.enabled and not in_burst_window(cfg):
        # one round per gap; whether an account is actually free is decided by account_rest_hours,
        # and the loop waits longer by itself when every account is still resting
        return max(60.0, cfg.rotation.round_gap_minutes * 60.0)
    if in_burst_window(cfg):
        base, jitter = cfg.burst.interval_seconds, min(cfg.jitter_seconds, cfg.burst.interval_seconds // 3)
    elif cfg.mode == "public":
        base, jitter = cfg.public_interval_seconds, min(cfg.public_jitter_seconds, cfg.public_interval_seconds // 3)
    else:
        base, jitter = cfg.interval_seconds, min(cfg.jitter_seconds, cfg.interval_seconds // 3)
    return max(10.0, base + random.uniform(-jitter, jitter))
