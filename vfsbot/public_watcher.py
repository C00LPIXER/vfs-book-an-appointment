"""Watch VFS slot availability via the PUBLIC 'earliest available date' endpoint — no login,
no OTP, no passport. One POST returns every centre x every visa category.

    POST https://lift-api.vfsglobal.com/appointment/centerwithearliestslot
    body {"missionCode":"bgr","countryCode":"ind","cultureCode":"en-US"}

The endpoint sits behind Cloudflare, so we load the public page once in a real browser to get a
cf_clearance cookie + the x-auth-token, then call the endpoint from inside that page each cycle.
This is an *indicative* figure VFS refreshes periodically (the response carries lastUpdatedOn);
it's a zero-risk early signal — a human then logs in to grab the actual slot.
"""
from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import Config, Secrets
from .events import log_event
from .watcher import Blocked, SlotResult, _fmt_date, find_browser, short_centre

log = logging.getLogger("vfsbot.public")

ENDPOINT = "https://lift-api.vfsglobal.com/appointment/centerwithearliestslot"
PUBLIC_PAGE = "{base}/"
SHOTS = Path("state/screenshots")

_FETCH_JS = """async ({url, tok, payload}) => {
  try {
    const r = await fetch(url, {method:'POST',
      headers:{'content-type':'application/json','x-auth-token':tok},
      body: JSON.stringify(payload)});
    return {status:r.status, text: await r.text()};
  } catch (e) { return {status:0, text:String(e)}; }
}"""


class PublicWatcher:
    def __init__(self, cfg: Config, secrets: Secrets | None = None):
        self.cfg = cfg
        self.secrets = secrets
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self._token: str | None = None
        self.last_updated: str = ""

    # ---- lifecycle ----------------------------------------------------------------

    def __enter__(self) -> "PublicWatcher":
        self._pw = sync_playwright().start()
        profile = Path("public-profile").resolve()
        profile.mkdir(parents=True, exist_ok=True)
        exe = find_browser(self.cfg.browser.executable)
        log.info("using browser: %s", exe or "(playwright chromium — may be blocked)")
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(profile), executable_path=exe, headless=self.cfg.browser.headless,
            no_viewport=True, args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(30_000)
        self.page.on("request", self._grab_token)
        self._close_extra_tabs()
        return self

    def _close_extra_tabs(self) -> None:
        """Brave can open a spare about:blank / new-tab page; keep only our working page."""
        try:
            for pg in list(self.ctx.pages):
                if pg is not self.page and not pg.is_closed():
                    pg.close()
        except Exception:  # noqa: BLE001
            pass

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _grab_token(self, req) -> None:
        if "centerwithearliestslot" in req.url.lower():
            t = req.headers.get("x-auth-token")
            if t:
                self._token = t

    def _ensure_page(self) -> None:
        if self.page is None or self.page.is_closed():
            self.page = self.ctx.new_page()
            self.page.set_default_timeout(30_000)
            self.page.on("request", self._grab_token)

    def screenshot(self, tag: str) -> str | None:
        try:
            self._ensure_page()
            SHOTS.mkdir(parents=True, exist_ok=True)
            name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{re.sub(r'[^a-z0-9]+','_',tag.lower())}.png"
            self.page.screenshot(path=str(SHOTS / name), full_page=True)
            return name
        except Exception:  # noqa: BLE001
            return None

    def load_page(self) -> None:
        """Load the public page to refresh the Cloudflare clearance + auth token."""
        self._ensure_page()
        self.page.goto(PUBLIC_PAGE.format(base=self.cfg.base_url), wait_until="domcontentloaded")
        # wait for the page's own EAD call so we capture the token and pass Cloudflare
        for _ in range(15):
            self.page.wait_for_timeout(1000)
            if self._token:
                break
        self._close_extra_tabs()
        body = self.page.locator("body").inner_text()[:200]
        if '"code": "403' in body or "Attention Required" in body:
            raise Blocked(body[:120])

    # ---- compatibility shims (the watch loop was written for the login watcher) ----
    on_otp_required = staticmethod(lambda: None)
    on_human_needed = staticmethod(lambda what: None)

    def ensure_logged_in(self, wait_minutes: int = 0) -> None:
        if not self._token:
            self.load_page()

    def _on_login_page(self) -> bool:
        return False

    def _is_logged_in(self) -> bool:
        return bool(self._token)

    def keepalive(self) -> None:
        try:
            self._ensure_page()
            self.page.mouse.move(random.randint(200, 600), 300)
        except Exception:  # noqa: BLE001
            pass

    # ---- the check ----------------------------------------------------------------

    def _fetch(self) -> dict:
        self._ensure_page()
        if not self._token:
            self.load_page()
        payload = {"missionCode": "bgr", "countryCode": "ind", "cultureCode": "en-US"}
        res = self.page.evaluate(_FETCH_JS, {"url": ENDPOINT, "tok": self._token, "payload": payload})
        if res["status"] != 200 or not res["text"].startswith("{"):
            # token/cookie stale or blocked → reload the page once and retry
            if '"code": "403' in res["text"] or res["status"] in (401, 403, 0):
                self.load_page()
                res = self.page.evaluate(_FETCH_JS, {"url": ENDPOINT, "tok": self._token, "payload": payload})
        data = json.loads(res["text"])
        if not isinstance(data, dict) or "vacList" not in data:
            raise RuntimeError(f"unexpected response: {res['text'][:120]}")
        self.last_updated = data.get("lastUpdatedOn", "")
        return data

    def raw(self) -> dict:
        """The full endpoint response (all centres x all categories)."""
        return self._fetch()

    def request_spec(self) -> dict:
        """Everything needed to reproduce the call outside the browser: url, headers, cookies, body.
        (Cloudflare still fingerprints the client, so this 403s from curl/Postman — see README.)"""
        if not self._token:
            self.load_page()
        ua = self.page.evaluate("() => navigator.userAgent")
        cookies = {c["name"]: c["value"] for c in self.ctx.cookies() if "cf" in c["name"].lower()}
        return {
            "method": "POST",
            "url": ENDPOINT,
            "headers": {
                "content-type": "application/json",
                "accept": "application/json, text/plain, */*",
                "x-auth-token": self._token,
                "user-agent": ua,
                "origin": "https://visa.vfsglobal.com",
                "referer": "https://visa.vfsglobal.com/",
            },
            "cookies": cookies,
            "body": {"missionCode": "bgr", "countryCode": "ind", "cultureCode": "en-US"},
        }

    last_raw: dict = {}

    def check_all(self) -> list[SlotResult]:
        """One request → a SlotResult per configured centre, for the configured category."""
        data = self._fetch()
        self.last_raw = data
        # index the response by short centre name
        by_centre: dict[str, list[dict]] = {}
        for vac in data.get("vacList", []):
            by_centre[short_centre(vac.get("vacName", "")).lower()] = vac.get("visaGroupList", [])
        cat = (self.cfg.category or "D visa").lower()
        results: list[SlotResult] = []
        for centre in self.cfg.centre_list:
            groups = by_centre.get(short_centre(centre).lower(), [])
            group = next((g for g in groups if cat in g.get("displayName", "").lower()), None)
            if group is None and groups:
                group = groups[0]
            date = (group or {}).get("earliestAvailableDate", "")
            if date:
                results.append(SlotResult(True, _fmt_date(date), "public-api", "", group or {}, centre))
            else:
                results.append(SlotResult(False, None, "public-api", "", {}, centre))
        return results
