"""The slot detector that needs no account.

    GET lift-api.vfsglobal.com/master/centerwithslots/{mission}/{country}/{category}/{culture}

No JWT, no cookies, no clientsource — just a browser-shaped TLS handshake (curl_cffi). Because
nothing is authenticated, polling it cannot get an account restricted, and it names the centres that
have availability instead of the lagging "earliest date" figure.

Reading the answer (VFS is not helpful about this):

    "centerName": null   /  []  /  [{}]        -> nothing open
    an item with a real centerName and no error.code -> that centre HAS slots
    error.code 4100 "Internal Server Error"    -> also just "nothing open", not a fault

Failures are classified the way a long-running VFS watcher learned to: a 403 carrying `403204` is the
WAF blocking the IP, a plain 403 is the Cloudflare interstitial, 429 (or `429001`/`429201` in the
body) is rate limiting, and five of either in a row means back off hard.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field

from curl_cffi import requests as cr

log = logging.getLogger("vfsbot.slots")

ENDPOINT = "https://lift-api.vfsglobal.com/master/centerwithslots/{mission}/{country}/{category}/{culture}"
IMPERSONATE = ("chrome", "chrome124", "chrome120")

# what each answer means — the whole point of this module
NO_SLOTS, SLOT, BLOCK_WAF, BLOCK_CF, RATE_LIMIT, SERVER_ERR, BAD = (
    "no-slots", "SLOT", "block-waf", "block-cloudflare", "rate-limited", "server-error", "unreadable")


@dataclass
class Probe:
    state: str
    centres: list[str] = field(default_factory=list)
    status: int = 0
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.state in (NO_SLOTS, SLOT)


def classify(body: str, status: int) -> Probe:
    if status == 403:
        return Probe(BLOCK_WAF if "403204" in body else BLOCK_CF, status=status, detail=body[:120])
    if status == 429 or '"429201"' in body or '"429001"' in body:
        return Probe(RATE_LIMIT, status=status, detail=body[:120])
    if status >= 500:
        return Probe(SERVER_ERR, status=status, detail=body[:120])
    if status != 200 or not body:
        return Probe(BAD, status=status, detail=body[:120])
    if '"centerName":null' in body.replace(" ", "") or body.strip() in ("[]", "[{}]", "{}"):
        return Probe(NO_SLOTS, status=status)
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return Probe(BAD, status=status, detail=body[:120])
    if isinstance(data, list):
        open_centres = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("centerName")
            err = item.get("error") or {}
            if name and not (isinstance(err, dict) and err.get("code")):
                open_centres.append(str(name).strip())
        if open_centres:
            return Probe(SLOT, centres=open_centres, status=status)
    return Probe(NO_SLOTS, status=status)


class SlotProbe:
    """One target: a mission/country/visa category. Keeps its own failure streaks."""

    def __init__(self, cfg, category_code: str = "", proxy: str = ""):
        self.cfg = cfg
        self.category_code = category_code or cfg.category_code
        self.proxy = proxy
        self.waf_streak = 0
        self.rate_streak = 0
        self.last_probe: Probe | None = None

    @property
    def url(self) -> str:
        return ENDPOINT.format(mission=self.cfg.mission_code, country=self.cfg.country_code,
                               category=self.category_code, culture=self.cfg.culture_code)

    def _headers(self) -> dict:
        return {"accept": "application/json, text/plain, */*",
                "origin": "https://visa.vfsglobal.com",
                "referer": "https://visa.vfsglobal.com/",
                "route": f"{self.cfg.country_code}/en/{self.cfg.mission_code}",
                "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/140.0.0.0 Safari/537.36")}

    def probe(self, timeout: float = 20) -> Probe:
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        last = Probe(BAD, detail="no attempt")
        for imp in IMPERSONATE:
            try:
                r = cr.get(self.url, headers=self._headers(), impersonate=imp,
                           proxies=proxies, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                last = Probe(BAD, detail=f"{type(e).__name__}: {e}"[:120])
                continue
            last = classify(r.text or "", r.status_code)
            if last.usable:
                break
        self.last_probe = last
        self.waf_streak = self.waf_streak + 1 if last.state in (BLOCK_WAF, BLOCK_CF) else 0
        self.rate_streak = self.rate_streak + 1 if last.state == RATE_LIMIT else 0
        return last

    def confirm(self, hits: int = 2, gap: float = 1.5) -> Probe | None:
        """A hit is only real if it repeats — VFS occasionally returns a half-built list."""
        seen = []
        for i in range(hits):
            p = self.probe()
            seen.append(p)
            if p.state != SLOT:
                return None
            if i + 1 < hits:
                time.sleep(gap)
        merged: list[str] = []
        for p in seen:
            for c in p.centres:
                if c not in merged:
                    merged.append(c)
        return Probe(SLOT, centres=merged, status=200)

    def next_delay(self) -> float:
        """How long to wait before the next probe, given what just happened."""
        base = self.cfg.slot_probe_seconds
        j = self.cfg.slot_probe_jitter
        if self.rate_streak >= 5 or self.waf_streak >= 5:
            return self.cfg.slot_backoff_seconds          # hard back-off, they want us gone
        if self.rate_streak or self.waf_streak:
            return base * 4
        return max(10.0, base * random.uniform(1 - j, 1 + j))
