"""Human-like pacing for the browser: nothing the bot does happens at a fixed interval or instantly.

Every wait is drawn from a range (log-normal-ish: mostly short, sometimes noticeably longer), text is
typed key by key with varying delays and the odd hesitation, the mouse drifts along curves instead
of jumping, and pages get scrolled a little the way a person skims them. Cloudflare's bot scoring
looks at exactly these signals (timing regularity, absent pointer movement, instant form fills).
"""
from __future__ import annotations

import math
import random

from playwright.sync_api import Locator, Page


def jitter(lo_ms: float, hi_ms: float) -> int:
    """A wait between lo and hi ms, skewed towards the low end with a fat tail (occasionally ~1.6x hi)."""
    u = random.random()
    v = lo_ms + (hi_ms - lo_ms) * (u ** 1.6)
    if random.random() < 0.08:            # the occasional distraction
        v *= random.uniform(1.2, 1.6)
    return int(v)


def nap(page: Page, lo_ms: float, hi_ms: float) -> None:
    page.wait_for_timeout(jitter(lo_ms, hi_ms))


def _bezier(p0, p1, p2, p3, t):
    return ((1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t ** 2 * p2[0] + t ** 3 * p3[0],
            (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t ** 2 * p2[1] + t ** 3 * p3[1])


_last_pos: dict[int, tuple[float, float]] = {}


def move_to(page: Page, x: float, y: float) -> None:
    """Move the pointer to (x, y) along a curved path with variable speed."""
    key = id(page)
    sx, sy = _last_pos.get(key, (random.uniform(200, 900), random.uniform(150, 600)))
    dist = math.hypot(x - sx, y - sy)
    steps = max(6, min(40, int(dist / 18)))
    c1 = (sx + (x - sx) * random.uniform(0.2, 0.5) + random.uniform(-80, 80), sy + (y - sy) * random.uniform(0.0, 0.4) + random.uniform(-60, 60))
    c2 = (sx + (x - sx) * random.uniform(0.5, 0.9) + random.uniform(-60, 60), sy + (y - sy) * random.uniform(0.6, 1.0) + random.uniform(-40, 40))
    try:
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)                       # ease in/out
            px, py = _bezier((sx, sy), c1, c2, (x, y), t)
            page.mouse.move(px + random.uniform(-1.5, 1.5), py + random.uniform(-1.5, 1.5))
            page.wait_for_timeout(random.randint(4, 22))
    except Exception:  # noqa: BLE001
        pass
    _last_pos[key] = (x, y)


def wander(page: Page, moves: int | None = None) -> None:
    """A few aimless pointer drifts, like someone reading the page."""
    try:
        w = page.evaluate("() => window.innerWidth") or 1200
        h = page.evaluate("() => window.innerHeight") or 800
    except Exception:  # noqa: BLE001
        w, h = 1200, 800
    for _ in range(moves if moves is not None else random.randint(1, 3)):
        move_to(page, random.uniform(w * 0.15, w * 0.85), random.uniform(h * 0.15, h * 0.85))
        page.wait_for_timeout(jitter(120, 700))


def scroll_around(page: Page) -> None:
    """Scroll down a bit in a couple of wheel ticks, pause, sometimes scroll back up."""
    try:
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(120, 420))
            page.wait_for_timeout(jitter(150, 600))
        page.wait_for_timeout(jitter(300, 1400))
        if random.random() < 0.7:
            for _ in range(random.randint(1, 2)):
                page.mouse.wheel(0, -random.randint(150, 450))
                page.wait_for_timeout(jitter(120, 500))
    except Exception:  # noqa: BLE001
        pass


def click(page: Page, target: Locator, timeout: float = 8000) -> None:
    """Move to the element along a curve, hesitate, click slightly off-centre."""
    target.wait_for(state="visible", timeout=timeout)
    box = target.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        move_to(page, x, y)
        page.wait_for_timeout(jitter(60, 260))
        page.mouse.down(); page.wait_for_timeout(random.randint(40, 130)); page.mouse.up()
    else:
        target.click(timeout=timeout)


def type_into(page: Page, target: Locator, text: str, clear: bool = True) -> None:
    """Click the field, then type key by key with human rhythm (bursts, hesitations)."""
    click(page, target)
    page.wait_for_timeout(jitter(120, 500))
    if clear:
        try:
            target.fill("")
        except Exception:  # noqa: BLE001
            pass
    for i, ch in enumerate(text):
        delay = random.randint(45, 160)
        if ch in "@._-":
            delay += random.randint(60, 220)         # symbols take a beat
        if random.random() < 0.06 and i:
            delay += random.randint(250, 900)        # hesitation
        page.keyboard.type(ch, delay=0)
        page.wait_for_timeout(delay)
    page.wait_for_timeout(jitter(200, 700))


def dwell(page: Page, lo_ms: float = 900, hi_ms: float = 3200) -> None:
    """Look at a freshly loaded page for a moment: pause, maybe scroll, maybe move the pointer."""
    page.wait_for_timeout(jitter(lo_ms, hi_ms))
    r = random.random()
    if r < 0.45:
        wander(page)
    elif r < 0.75:
        scroll_around(page)
