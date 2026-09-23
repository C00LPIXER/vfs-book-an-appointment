"""Drives the VFS site in a real (persistent) browser and reports slot availability.

Flow on the site (Angular SPA, backed by lift-api.vfsglobal.com):
    /login  ->  /dashboard  --"Start New Booking"-->  /application-detail
    On /application-detail you pick Centre / Category / Sub-category (mat-select) and the page
    calls POST .../appointment/CheckIsSlotAvailable, then shows either
    "No appointment slots are currently available" or an "Earliest Available Slot" date.
We read both the API response and the page text so either one is enough.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext, Page, Response, TimeoutError as PWTimeout, sync_playwright

from . import human
from .accounts import Account, AccountPool
from .config import Config
from .events import log_event
from .otp import ImapAuthError, fetch_latest_otp

log = logging.getLogger("vfsbot.watcher")

NO_SLOT_RE = re.compile(r"no appointment slots? (are|is) currently available|no slots? available", re.I)
# e.g. "Earliest available slot for 1 Applicants is : 28-09-2026"
EARLIEST_RE = re.compile(r"earliest available slot.*?([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{4})", re.I | re.S)


def _fmt_date(raw) -> str:
    """API gives '09/28/2026 00:00:00' (US order); show it as 28-09-2026."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", str(raw))
    return f"{m.group(2)}-{m.group(1)}-{m.group(3)}" if m else str(raw)


class LoginRequired(Exception):
    pass


class OtpRequired(LoginRequired):
    pass


class PassportPending(LoginRequired):
    """Logged in, but VFS's one-time passport-upload step is waiting for a human to press Continue."""


OTP_FILE = Path("state/otp.txt")
def _browser_candidates() -> list[str]:
    """Where a real Brave/Chrome lives, per platform. VFS blocks Playwright's own Chromium, so
    finding the installed browser is not optional — an empty result means the bot cannot work."""
    if sys.platform == "win32":
        roots = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        rel = [r"BraveSoftware\Brave-Browser\Application\brave.exe",
               r"Google\Chrome\Application\chrome.exe",
               r"Microsoft\Edge\Application\msedge.exe"]
        return [str(Path(r) / x) for r in roots if r for x in rel]
    if sys.platform == "darwin":
        return ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "brave-browser", "google-chrome"]
    return [
        "/opt/brave.com/brave/brave", "brave-browser", "brave",
        "/opt/google/chrome/chrome", "google-chrome", "google-chrome-stable",
        "chromium", "chromium-browser",
    ]


BROWSER_CANDIDATES = _browser_candidates()


NO_RESTORE_ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check",
                   "--disable-session-crashed-bubble", "--hide-crash-restore-bubble"]
# Playwright disables extensions by default; we keep them so a VPN extension installed in an
# account's profile (Settings → "Open this account's browser") stays active during the bot's sessions.
IGNORE_DEFAULT_ARGS = ["--enable-automation", "--disable-extensions", "--disable-component-extensions-with-background-pages"]


def prepare_profile(profile: Path) -> None:
    """Stop Chrome/Brave from restoring last session's tabs: mark the last exit as clean and set
    'open the New Tab page' on startup. Otherwise every launch re-opens old ipify/login tabs and the
    bot ends up driving a background tab (Turnstile does not run reliably there)."""
    prefs = profile / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text()) if prefs.exists() else {}
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("profile", {}).update({"exit_type": "Normal", "exited_cleanly": True})
    data.setdefault("session", {})["restore_on_startup"] = 5
    data["session"]["startup_urls"] = []
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(json.dumps(data))


def single_tab(ctx: BrowserContext) -> Page:
    """Close whatever tabs the profile opened and return one fresh, foreground tab."""
    page = ctx.new_page()
    for pg in list(ctx.pages):
        if pg is not page:
            try:
                pg.close()
            except Exception:  # noqa: BLE001
                pass
    try:
        page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass
    return page


GUI_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def ensure_display() -> str:
    """Brave needs a desktop session. When the bot is started from a service, an ssh shell or any
    other environment without DISPLAY, Chrome exits the moment it launches ("Target page, context or
    browser has been closed"). Borrow the graphical session's variables from another process of this
    user so it works however the bot was started."""
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        _fill_xauthority()
        return os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY", "")
    uid = os.getuid()
    best: dict[str, str] | None = None
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            raw = (entry / "environ").read_bytes().decode("utf-8", "ignore")
        except (OSError, PermissionError):
            continue
        env = dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv)
        if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
            continue
        if best is None or (env.get("XAUTHORITY") and not best.get("XAUTHORITY")):
            best = env
        if best.get("XAUTHORITY"):      # a complete session: good enough
            break
    if best:
        for k in GUI_VARS:
            if best.get(k):
                os.environ[k] = best[k]
    else:
        os.environ.setdefault("DISPLAY", ":0")
    _fill_xauthority()
    log.info("desktop session: DISPLAY=%s XAUTHORITY=%s", os.environ.get("DISPLAY", "-"), os.environ.get("XAUTHORITY", "-"))
    return os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY", "")


def _fill_xauthority() -> None:
    """X11 also needs the session's auth cookie; find it when the borrowed environment lacked it."""
    if os.environ.get("XAUTHORITY") and Path(os.environ["XAUTHORITY"]).exists():
        return
    uid = os.getuid()
    for cand in sorted(Path(f"/run/user/{uid}").glob("xauth_*"), key=lambda f: f.stat().st_mtime, reverse=True):
        os.environ["XAUTHORITY"] = str(cand)
        return
    home_cookie = Path.home() / ".Xauthority"
    if home_cookie.exists():
        os.environ["XAUTHORITY"] = str(home_cookie)


def find_browser(configured: str = "") -> str | None:
    """The configured path wins; otherwise the first installed candidate for this platform."""
    if configured:
        return configured
    for c in BROWSER_CANDIDATES:
        if os.path.sep in c or (sys.platform == "win32" and ":" in c):
            if Path(c).exists():
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


class Blocked(Exception):
    """VFS/Cloudflare WAF returned a block page (e.g. {"code": "403201"}). Happens with headless
    browsers or after too many requests. Nothing to do but back off / use a visible browser."""


class SiteThrottled(Exception):
    """VFS is rate-limiting this IP (429 on its configuration endpoints, the SPA lands on
    /page-not-found). Nothing wrong with the account — wait, and give every account a rest."""


class TokenStale(Exception):
    """The `authorize` token captured from the SPA is no longer accepted — do one real form check,
    which makes the site issue (and us capture) a fresh one, then carry on over the API."""


class AccountRestricted(Blocked):
    """VFS restricted this specific account ("Access Restricted for User ID"). Nothing to wait for —
    the account must be switched off and the next one used."""


class CoolOff(Blocked):
    """Cloudflare is in its cool-off state for this account/IP: the Turnstile widget stays blank and
    never produces a token, so sign-in cannot proceed. Rotate to another account + IP and back off."""


class ProxyError(Exception):
    """The account's proxy is not working / the public IP did not change."""


class NoDisplay(Exception):
    """No graphical session is available, so a real browser cannot be opened."""


class ProfileInUse(Exception):
    """This account's browser profile is open in another window (e.g. the one opened by hand from
    the dashboard). Chrome allows a profile in one process at a time."""


BLOCK_RE = re.compile(r'"code"\s*:\s*"403\d*"|access denied|attention required|cloudflare', re.I)
# VFS restricts a single account: "Access Restricted for User ID (429001) ... unusual activity"
RESTRICTED_RE = re.compile(r"access restricted for user id|unusual activity coming from your user|\b429001\b", re.I)


@dataclass
class SlotResult:
    available: bool
    earliest: Optional[str] = None
    source: str = ""            # "api" | "text"
    page_text: str = ""
    api_payload: dict = field(default_factory=dict)
    centre: str = ""
    error: str = ""

    def summary(self) -> str:
        name = short_centre(self.centre)
        if self.error:
            return f"{name}: check failed ({self.error})"
        if self.available:
            return f"{name}: {self.earliest or 'see site'}"
        return f"{name}: none"


def short_centre(full: str) -> str:
    """'Bulgaria Visa Application Centre-Cochin' -> 'Cochin'."""
    m = re.search(r"[-,]\s*([A-Za-z ]+?)(?:-VAC)?\s*$", full)
    return (m.group(1) if m else full).strip() or full


def summarize(results: list["SlotResult"]) -> str:
    """Multi-line report in config order: available centres first, then the rest."""
    avail = [r for r in results if r.available]
    rest = [r for r in results if not r.available]
    lines = [f"✅ {r.summary()}" for r in avail] + [f"— {r.summary()}" for r in rest]
    return "\n".join(lines)


class Watcher:
    def __init__(self, cfg: Config, account: Account | None = None, profile_dir: str = ""):
        self.cfg = cfg
        # Each account brings its own creds, IMAP inbox, proxy and browser profile. Without an explicit
        # one (non-rotating mode / CLI helpers) take the pool's next available account.
        if account is None:
            account = AccountPool().next()
            if account is None:
                raise RuntimeError("no VFS account available — add one on the dashboard (data/accounts.json)")
        self.account = account
        self.profile_dir = profile_dir or account.profile_dir
        self.public_ip: str = ""
        self.imap_error: str = ""
        self.logged_in = False
        self.rate_limited = False
        self.upload_throttled = False
        self.site_throttled = False
        self._centre_hint = ""
        self._last_slot_call = 0.0      # monotonic time of the last CheckIsSlotAvailable
        self._otp_tried: set[str] = set()
        self.partial_results: list[SlotResult] = []
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self._last_api: dict | None = None
        self._api_template: dict | None = None      # captured CheckIsSlotAvailable request (url/headers/body)
        self._master_lists: dict[str, list] = {}    # captured lift-api list responses (centre list etc.)

    # ---- lifecycle ----------------------------------------------------------------

    def __enter__(self) -> "Watcher":
        # before the driver starts: the node process (and therefore the browser) inherits this env
        if not ensure_display() and not self.cfg.browser.headless:
            raise NoDisplay("no desktop session found — the bot needs a logged-in desktop to open Brave")
        self._pw = sync_playwright().start()
        try:
            return self._start()
        except Exception:
            # the driver keeps an asyncio loop alive; leaving it running poisons every later
            # sync_playwright() in this process ("Sync API inside the asyncio loop")
            try:
                self._pw.stop()
            finally:
                self._pw = None
            raise

    def _start(self) -> "Watcher":
        profile = Path(self.profile_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        prepare_profile(profile)
        exe = find_browser(self.cfg.browser.executable)
        if exe:
            log.info("using browser: %s  profile=%s  account=%s", exe, profile.name, self.account.name or "-")
        else:
            log.warning("No real Chrome/Brave found — falling back to Playwright's Chromium, which FAILS "
                        "Cloudflare's check on VFS. Install Brave or Google Chrome (non-flatpak).")
        if self.account.proxy_auto:
            from .proxies import ensure_tunnel
            self.account.proxy = ensure_tunnel(self.account.email)   # (re)starts the SSH SOCKS tunnel if needed
        proxy = self.account.playwright_proxy()
        if proxy:
            log.info("proxy: %s%s", proxy["server"], " (auto tunnel)" if self.account.proxy_auto else "")
        elif self.cfg.rotation.require_proxy:
            raise ProxyError(f"{self.account.name}: no proxy configured and rotation.require_proxy is on")
        self._release_profile(profile)
        try:
            self.ctx = self._launch(profile, exe, proxy)
        except Exception as e:  # noqa: BLE001
            if "Target page, context or browser has been closed" in str(e):
                raise NoDisplay(f"{Path(exe or 'the browser').name} exited immediately — no usable desktop session "
                                f"(DISPLAY={os.environ.get('DISPLAY', 'unset')})") from e
            raise
        self.page = single_tab(self.ctx)
        self.page.set_default_timeout(20_000)
        self.page.on("response", self._on_response)
        if self.ctx.background_pages or self.ctx.service_workers:
            log.info("extensions active in this profile: %d", len(self.ctx.background_pages) + len(self.ctx.service_workers))
            self.page.wait_for_timeout(4000)   # let a VPN extension connect before the first request
        return self

    def _launch(self, profile: Path, exe: str | None, proxy: dict | None):
        return self._pw.chromium.launch_persistent_context(
            str(profile),
            executable_path=exe,
            headless=self.cfg.browser.headless,
            no_viewport=True,   # no emulation at all: Cloudflare flags locale/timezone/viewport overrides
            proxy=proxy,
            args=NO_RESTORE_ARGS,
            ignore_default_args=IGNORE_DEFAULT_ARGS,
        )

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx:
                # leave the profile on a blank tab so nothing sensitive is in "last session"
                for pg in list(self.ctx.pages):
                    try:
                        pg.goto("about:blank", timeout=3000)
                    except Exception:  # noqa: BLE001
                        pass
                self.ctx.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._pw:
                self._pw.stop()
            self._wait_profile_free()

    def _release_profile(self, profile: Path) -> None:
        """Chrome refuses to open a profile that another process already has. Wait briefly for a
        closing window to let go, then say clearly what is holding it."""
        holder = self._profile_holder(profile)
        if holder is None:
            return
        end = time.monotonic() + 15
        while time.monotonic() < end:
            time.sleep(1)
            holder = self._profile_holder(profile)
            if holder is None:
                return
        raise ProfileInUse(f"{Path(profile).name} is open in another browser window (pid {holder}) — "
                           "close it (it was probably opened from the dashboard) and the bot will use it again")

    @staticmethod
    def _profile_holder(profile: Path) -> int | None:
        """PID of the live process holding this profile's lock, or None."""
        lock = Path(profile) / "SingletonLock"
        try:
            pid = int(os.readlink(lock).rsplit("-", 1)[-1])
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def _wait_profile_free(self, timeout: float = 20) -> None:
        """Block until Brave has released this profile, so the next login can reuse it."""
        lock = Path(self.profile_dir) / "SingletonLock"
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not lock.exists():
                return
            try:
                target = os.readlink(lock)                     # "<host>-<pid>"
                pid = int(target.rsplit("-", 1)[-1])
                os.kill(pid, 0)                                # still alive?
            except (OSError, ValueError):
                return                                         # stale lock or gone
            time.sleep(0.5)

    # ---- helpers ------------------------------------------------------------------

    # request headers the browser sets itself — never replay these from a captured request
    _BROWSER_HEADERS = {"host", "origin", "referer", "cookie", "content-length", "connection", "accept-encoding",
                        "user-agent", "priority", "te", "upgrade-insecure-requests"}

    def _on_response(self, resp: Response) -> None:
        url = resp.url
        if "lift-api" in url and resp.status >= 400 and not (resp.status == 409 and "CheckIsSlotAvailable" in url):
            # (409 on CheckIsSlotAvailable is VFS's normal "no slots" answer — not an error)
            path = url.split("lift-api.vfsglobal.com", 1)[-1][:80]
            try:
                body = resp.text()[:200]
            except Exception:  # noqa: BLE001
                body = ""
            log.warning("lift-api %s on %s: %s", resp.status, path, body)
            log_event("check" if resp.status in (409, 429) else "error", f"VFS API {resp.status} on {path}: {body}", "warn", {"status": resp.status})
        if resp.status == 429 and "lift-api" in url and "CheckIsSlotAvailable" not in url:
            self.site_throttled = True        # VFS is throttling this IP, not this account
        if "UploadApplicantDocument" in url and resp.status in (409, 429):
            self.upload_throttled = True      # VFS refuses more document uploads for now
        if "CheckIsSlotAvailable" in url and resp.status == 429:
            self.rate_limited = True      # VFS's per-session quota is spent: stop asking, or it logs us out
        if "CheckIsSlotAvailable" in url or "/appointment/slots" in url:
            try:
                self._last_api = resp.json()
                log.debug("API %s -> %s", url, json.dumps(self._last_api)[:300])
            except Exception:  # noqa: BLE001
                pass
            if "CheckIsSlotAvailable" in url and resp.status == 200:
                # remember exactly how the SPA asks, so the other centres can be queried directly
                try:
                    req = resp.request
                    hdrs = {k: v for k, v in req.headers.items()
                            if not k.startswith(":") and not k.lower().startswith("sec-") and k.lower() not in self._BROWSER_HEADERS}
                    self._api_template = {"url": url, "headers": hdrs, "body": json.loads(req.post_data or "{}")}
                except Exception as e:  # noqa: BLE001
                    log.debug("could not capture API template: %s", e)
        elif "lift-api" in url and resp.status == 200:
            # keep every list-shaped master response (centres, categories...) to look codes up later
            try:
                data = resp.json()
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    self._master_lists[url] = data
            except Exception:  # noqa: BLE001
                pass

    @property
    def url(self) -> str:
        return self.page.url if self.page else ""

    def goto(self, url: str, attempts: int = 3) -> None:
        """Navigate, retrying transient aborts. Chrome reports ERR_ABORTED when a navigation is
        superseded (the Angular app redirects on load) or when the profile is still settling right
        after a previous window closed."""
        last = None
        for i in range(attempts):
            try:
                self._ensure_page()
                self.page.goto(url, wait_until="domcontentloaded")
                return
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e).splitlines()[0]
                if "ERR_ABORTED" not in msg and "closed" not in msg and "Timeout" not in msg:
                    raise
                log.warning("navigation to %s failed (%s) — retry %d/%d", url.rsplit("/", 1)[-1], msg[-60:], i + 1, attempts)
                self.page.wait_for_timeout(human.jitter(1500, 4000))
        raise last  # type: ignore[misc]

    def _ensure_page(self) -> None:
        """Re-open a tab if the user closed it (the persistent context keeps the session)."""
        if self.page is None or self.page.is_closed():
            log.warning("browser tab was closed — opening a new one")
            self.page = single_tab(self.ctx)
            self.page.set_default_timeout(20_000)
            self.page.on("response", self._on_response)

    def _on_login_page(self) -> bool:
        """True only if we actually need to authenticate. The post-OTP 'upload passport' page
        keeps /login in its URL, so check for the credential/OTP form, not just the URL."""
        if "/login" not in self.url:
            return False
        try:
            if self.page.locator(f"{self.LOGIN_FORM}, {self.OTP_FORM}").count() > 0:
                return True
            return not self._on_passport_upload()   # blank / still-loading /login is still "not logged in"
        except Exception:  # noqa: BLE001
            return True

    def _is_logged_in(self) -> bool:
        try:
            return self.page.get_by_text("Sign Out", exact=True).count() > 0 and not self._on_login_page()
        except Exception:  # noqa: BLE001
            return False

    def _on_passport_upload(self) -> bool:
        try:
            return self.page.locator("input[type=file]").count() > 0 and bool(
                re.search(r"upload.*passport|bio page", self.page.locator("body").inner_text(timeout=2000), re.I))
        except Exception:  # noqa: BLE001
            return False

    def _on_otp_step(self) -> bool:
        try:
            if self.page.locator(self.OTP_FORM).count():
                return True
            return bool(re.search(r"one time password|\bOTP\b", self.page.locator("body").inner_text(timeout=2000), re.I))
        except Exception:  # noqa: BLE001
            return False

    def _submit_button(self):
        return self.page.get_by_role("button", name=re.compile(r"sign\s*in|submit|verify|continue", re.I)).first

    def _dismiss_cookie_banner(self) -> None:
        try:
            btn = self.page.locator("#onetrust-accept-btn-handler")
            if btn.count() and btn.is_visible():
                btn.click(timeout=2000)
        except Exception:  # noqa: BLE001
            pass

    def _try_submit_otp(self) -> bool:
        """Type the OTP waiting in state/otp.txt and submit it. A code is only ever tried once: the
        same mail stays the newest in the inbox, so without this the loop would retype a rejected
        code every few seconds instead of waiting for VFS to send a fresh one."""
        if not OTP_FILE.exists():
            return False
        code = OTP_FILE.read_text().strip()
        OTP_FILE.unlink(missing_ok=True)
        if not code:
            return False
        if code in self._otp_tried:
            log.debug("OTP %s already tried — waiting for a newer one", code)
            return False
        self._otp_tried.add(code)
        box = self.page.locator(self.OTP_FORM).first
        if not box.count():
            box = self.page.locator("input:not([type=hidden])").last
        human.type_into(self.page, box, code)
        log.info("OTP entered from %s", OTP_FILE)
        human.nap(self.page, 400, 1500)
        try:
            human.click(self.page, self._submit_button(), timeout=5000)
        except Exception:  # noqa: BLE001
            box.press("Enter")
        return True

    LOGIN_FORM = "input[formcontrolname='username']"
    OTP_FORM = "input[formcontrolname*='otp' i], input[type='tel'], input[placeholder*='OTP' i]"
    DASHBOARD = "button:has-text('Start New Booking'), a:has-text('Start New Booking'), mat-select, .dashboard, app-dashboard"

    def _wait_for_spa(self, timeout_ms: int = 25_000) -> None:
        """After a goto, the Angular app takes a few seconds to decide whether to bounce us to
        /login. Wait until either the login form or real dashboard content is visible."""
        self._raise_if_blocked()
        try:
            self.page.locator(f"{self.LOGIN_FORM}, {self.DASHBOARD}").first.wait_for(state="visible", timeout=timeout_ms)
        except PWTimeout:
            self._raise_if_blocked()
            log.warning("page did not settle within %ss (url=%s)", timeout_ms // 1000, self.url)
        self.page.wait_for_timeout(500)

    def _spa_rendered(self) -> bool:
        try:
            return self.page.locator(f"{self.LOGIN_FORM}, {self.OTP_FORM}, {self.DASHBOARD}, input[type=file]").count() > 0
        except Exception:  # noqa: BLE001
            return False

    def _raise_if_blocked(self) -> None:
        try:
            text = self.page.locator("body").inner_text(timeout=3000)
        except Exception:  # noqa: BLE001
            return
        if RESTRICTED_RE.search(text) or "page-not-found" in self.url and RESTRICTED_RE.search(text):
            m = re.search(r"Access Restricted for User ID[^\n]*", text, re.I)
            detail = (m.group(0) if m else "VFS restricted this account").strip()[:160]
            log_event("blocked", f"{self.account.name}: {detail}", "error", {"url": self.url},
                      self.screenshot("account_restricted"))
            raise AccountRestricted(detail)
        if len(text) < 400 and BLOCK_RE.search(text):
            log_event("blocked", "VFS/Cloudflare block page", "error", {"text": text[:200]}, self.screenshot("blocked"))
            raise Blocked(text.strip()[:200])

    def _has_turnstile(self) -> bool:
        try:
            return self.page.locator("iframe[src*='challenges.cloudflare.com'], .cf-turnstile").count() > 0
        except Exception:  # noqa: BLE001
            return False

    # ---- proxy / IP ---------------------------------------------------------------

    def check_ip(self, must_differ_from: str = "") -> str:
        """Look up the public IP as seen *through the browser* (i.e. through the proxy). Raises
        ProxyError when the proxy is dead or the IP is one we must not reuse (the machine's own IP
        when a proxy is configured, or the IP the previous account just used)."""
        url = self.cfg.rotation.ip_check_url
        try:
            # ctx.request goes through the context's proxy — no tab navigation, nothing visible
            text = self.ctx.request.get(url, timeout=30_000).text()
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}|[0-9a-f:]{6,})", text)
            if not m:
                raise ProxyError(f"unexpected reply from {url}: {text[:80]!r}")
            self.public_ip = m.group(1)
        except ProxyError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ProxyError(f"IP lookup through proxy failed: {str(e).splitlines()[0][:120]}") from e
        log.info("public IP for %s: %s", self.account.name, self.public_ip)
        if must_differ_from and self.public_ip == must_differ_from:
            raise ProxyError(f"public IP {self.public_ip} is the same as the last one — proxy not effective")
        return self.public_ip

    # ---- login --------------------------------------------------------------------

    def ensure_logged_in(self, wait_minutes: int = 10) -> None:
        """Go to the dashboard. If we land on /login, pre-fill creds and wait for a human
        to clear Cloudflare + press Sign In (or press it ourselves if the check auto-passes)."""
        self.goto(f"{self.cfg.base_url}/dashboard")
        self._wait_for_spa()
        if not self._spa_rendered():
            log.warning("VFS page is blank — reloading once")
            self.page.reload(wait_until="domcontentloaded")
            self._wait_for_spa()
        if self.site_throttled or "page-not-found" in self.url:
            self._raise_if_blocked()          # an account restriction says so on the page itself
            if self.site_throttled:
                raise SiteThrottled(f"VFS is rate-limiting this IP ({self.public_ip or 'no proxy'}) — it bounced to {self.url.rsplit('/', 1)[-1]}")
        self._dismiss_cookie_banner()
        human.dwell(self.page)
        if not self._on_login_page():
            return

        log.warning("Login required — please complete the Cloudflare check / sign in in the browser window.")
        log_event("login", f"Signing in as {self.account.name}", "info")
        self._prefill_login()
        login_started = datetime.now().astimezone()

        deadline = wait_minutes * 60_000
        stall_ms = 90_000   # blank Turnstile for this long = cool-off
        step = 2_000
        waited = 0
        clicked = False
        otp_announced = False
        imap_broken = False
        otp_waited = 0
        while waited < deadline:
            if waited % 6_000 == 0:
                self._raise_if_blocked()      # VFS may bounce us to the "account restricted" page
            if waited % 10_000 == 0:
                left = (deadline - waited) // 1000
                if self._on_otp_step():
                    self.on_status(f"waiting for the OTP mail ({left}s left)")
                elif self._has_turnstile():
                    self.on_status(f"waiting for Cloudflare's check ({left}s left)")
                else:
                    self.on_status(f"signing in ({left}s left)")
            if self.page.is_closed():
                self._ensure_page()
                self.goto(f"{self.cfg.base_url}/dashboard")
                self._wait_for_spa()
            if self._is_logged_in() or self._on_passport_upload():
                log.info("Logged in as %s.", self.account.name)
                self.logged_in = True
                self._log_jwt()
                log_event("login", f"Logged in ({self.account.name}" + (f", ip {self.public_ip}" if self.public_ip else "") + ")", "info",
                          screenshot=self.screenshot("logged_in"))
                self.page.wait_for_timeout(1500)
                if self._on_passport_upload():
                    self._upload_passport()      # raises PassportPending if a human still has to press Continue
                return
            if self._on_otp_step():
                if not otp_announced:
                    log.warning("OTP required — reading it from %s's inbox%s", self.account.name,
                                "" if self.account.imap_password else " is NOT configured; type it in the dashboard or write it to " + str(OTP_FILE))
                    log_event("otp", f"OTP requested by VFS for {self.account.name}" +
                              (" — reading it from the inbox" if self.account.imap_password else " — no App Password: type it in the dashboard"), "warn")
                    otp_announced = True
                    otp_asked_at = otp_since = datetime.now().astimezone()
                    otp_waited = 0
                    if not self.account.imap_password:
                        self.on_otp_required()      # nobody can fetch it for us: tell the humans right away
                otp_waited += step
                if otp_waited == 60_000 and self.account.imap_password and not imap_broken:
                    # a minute without the mail arriving: ask a human as a fallback (auto-fetch keeps trying)
                    log_event("otp", f"No OTP mail for {self.account.name} after 60 s — asking a human as fallback", "warn")
                    self.on_otp_required()
                if not self._try_submit_otp() and waited % 10_000 == 0 and self.account.imap_password and not imap_broken:
                    try:
                        code = fetch_latest_otp(self.account.imap_host, self.account.imap_login,
                                                self.account.imap_password, not_before=otp_since)
                    except ImapAuthError as e:
                        imap_broken = True   # don't hammer Google with a bad password; a human must fix it
                        msg = (f"OTP inbox login FAILED for {self.account.name} ({self.account.imap_login}): {e} — "
                               "fix the Gmail App Password on the Settings tab (or type the OTP in the dashboard)")
                        log.error(msg)
                        log_event("otp", msg, "error")
                        self.imap_error = str(e)[:160]
                        self.on_human_needed(msg)
                        code = None
                    if code and code not in self._otp_tried:
                        OTP_FILE.parent.mkdir(parents=True, exist_ok=True)
                        OTP_FILE.write_text(code)
                        log_event("otp", "OTP fetched automatically from inbox", "info")
                        self._try_submit_otp()
                    elif code:
                        # that code was rejected — from now on only take a mail sent after it
                        otp_since = datetime.now().astimezone()
                        if len(self._otp_tried) == 2:
                            log_event("otp", f"VFS rejected the OTP for {self.account.name} — waiting for a new one", "warn")
                        self.on_status(f"OTP rejected — waiting for a fresh one ({len(self._otp_tried)} tried)")
            else:
                # If Turnstile has already produced a token and the button is enabled, click once.
                if not clicked and self.account.email:
                    try:
                        token = self.page.input_value("input[name='cf-turnstile-response']", timeout=500)
                        btn = self._submit_button()
                        if token and btn.count() and btn.is_enabled():
                            human.nap(self.page, 500, 2200)
                            human.click(self.page, btn)
                            clicked = True
                    except Exception:  # noqa: BLE001
                        pass
                if waited % 10_000 == 0:
                    self._prefill_login()  # Angular sometimes re-inits the form; re-apply if it got cleared
                if waited == 60_000 and not clicked:
                    # A minute without a Cloudflare token: the check is probably the interactive kind.
                    log.warning("Cloudflare check not passing by itself — a human may need to click it in the browser window")
                    log_event("login", "Cloudflare check needs a human click in the browser window", "warn",
                              screenshot=self.screenshot("cloudflare_check"))
                    self.on_human_needed("Cloudflare 'verify you are human' check — click it in the bot's browser window")
                if waited >= stall_ms and not clicked and self._turnstile_stalled():
                    # Blank widget, no token, no checkbox: Cloudflare cool-off for this account/IP.
                    log_event("blocked", f"Cloudflare Turnstile stalled (blank) for {self.account.name} — cool-off",
                              "error", {"ip": self.public_ip}, self.screenshot("turnstile_stalled"))
                    raise CoolOff(f"Turnstile stalled for {self.account.name} (ip {self.public_ip or '?'})")
            self.page.wait_for_timeout(step)
            waited += step
        log_event("login", f"Gave up waiting for login/OTP ({self.account.name})", "error", screenshot=self.screenshot("login_timeout"))
        raise (OtpRequired if self._on_otp_step() else LoginRequired)(self.url)

    def _turnstile_stalled(self) -> bool:
        """True when the Turnstile widget is present but has neither produced a token nor rendered an
        interactive checkbox (the frame's inner document stays empty) — the observed cool-off state."""
        try:
            if self.page.input_value("input[name='cf-turnstile-response']", timeout=500):
                return False
        except Exception:  # noqa: BLE001
            pass
        if not self._has_turnstile():
            return False
        try:
            for fr in self.page.frames:
                if "challenges.cloudflare.com" in fr.url:
                    body = fr.locator("body").inner_text(timeout=1500).strip()
                    if body:                       # "Verifying…", checkbox label, "Success!" → not stalled
                        return False
        except Exception:  # noqa: BLE001
            return False
        return True

    def _log_jwt(self) -> None:
        """Log how long VFS's session token is valid (sessionStorage.JWT exp - iat)."""
        try:
            tok = self.page.evaluate("""() => { for (const k of ['JWT','jwt','token','access_token']) { const v = sessionStorage.getItem(k) || localStorage.getItem(k); if (v && v.split('.').length >= 3) return v; }
                                        const ls = JSON.parse(localStorage.getItem('loginResponse') || 'null'); if (ls && ls.accessToken) return ls.accessToken;
                                        return 'KEYS:' + Object.keys(sessionStorage).join(',') + '|' + Object.keys(localStorage).join(','); }""")
            if not tok or tok.startswith("KEYS:") or tok.count(".") < 2:
                log.info("no JWT found in storage (%s)", tok[:200] if tok else "empty")
                return
            import base64
            payload = tok.split(".")[1] + "=="
            claims = json.loads(base64.urlsafe_b64decode(payload))
            exp, iat = claims.get("exp"), claims.get("iat") or claims.get("nbf")
            if exp:
                mins = (exp - (iat or time.time())) / 60
                left = (exp - time.time()) / 60
                log.info("JWT valid %.1f min (expires in %.1f min)", mins, left)
                log_event("login", f"Session token valid {mins:.0f} min (expires {left:.0f} min from now)", "info",
                          {k: claims.get(k) for k in ("exp", "iat", "nbf")})
        except Exception as e:  # noqa: BLE001
            log.debug("JWT inspect failed: %s", e)

    def _upload_passport(self) -> None:
        """One-time account step after the first login: VFS wants the passport bio page. We select
        the file; then either press Continue ourselves (cfg.passport_auto_continue — VFS locks the
        data it extracts, so make sure the image is the right passport) or wait for a human to."""
        f = Path(self.account.passport_file or self.cfg.passport_file)
        if not f.exists():
            log_event("upload", f"VFS asks for {self.account.name}'s passport bio page but {f} is missing", "error",
                      screenshot=self.screenshot("passport_missing"))
            self.on_human_needed(f"VFS wants the passport bio page for {self.account.name} but {f} is missing — upload it on the Settings tab")
            raise PassportPending(self.url)
        try:
            self.page.locator("input[type=file]").first.set_input_files(str(f))
            self.page.wait_for_timeout(2500)
        except Exception as e:  # noqa: BLE001
            log_event("upload", f"Passport upload failed: {e}", "error", screenshot=self.screenshot("passport_error"))
            raise PassportPending(self.url)
        if self.cfg.passport_auto_continue:
            log_event("upload", f"Passport selected for {self.account.name} ({f.name}) — pressing Continue", "warn",
                      screenshot=self.screenshot("passport_selected"))
            try:
                btn = self._continue_control()
                btn.wait_for(state="visible", timeout=20_000)
                for _ in range(20):          # it enables once VFS has read the image
                    try:
                        if btn.is_enabled():
                            break
                    except Exception:  # noqa: BLE001  (a link is always "enabled")
                        break
                    self.page.wait_for_timeout(1000)
                human.nap(self.page, 800, 2500)
                human.click(self.page, btn)
            except Exception as e:  # noqa: BLE001
                log_event("upload", f"Could not press Continue: {str(e).splitlines()[0][:120]}", "error", screenshot=self.screenshot("passport_error"))
                self.on_human_needed(f"Passport step for {self.account.name}: press Continue in the bot's browser window")
        else:
            log_event("upload", f"Passport selected for {self.account.name} ({f.name}) — a human must press Continue in the browser window "
                      f"(waiting up to {self.cfg.passport_wait_minutes} min; or enable 'press Continue automatically' in Settings)", "warn",
                      screenshot=self.screenshot("passport_selected"))
            self.on_human_needed(f"First login of {self.account.name}: passport selected — press Continue in the bot's browser window")
        # wait for the step to be over (dashboard visible)
        started = time.monotonic()
        deadline = started + self.cfg.passport_wait_minutes * 60
        tries = 0
        while time.monotonic() < deadline:
            if self._is_logged_in() or "/dashboard" in self.url or not self._on_passport_upload():
                self.page.wait_for_timeout(1500)
                if not self._on_passport_upload():
                    log_event("upload", f"Passport step done for {self.account.name}", "info")
                    return
            waited_s = int(time.monotonic() - started)
            if (self.cfg.passport_auto_continue and tries < 2 and not self.upload_throttled
                    and waited_s >= 45 * (tries + 1)):
                tries += 1      # the first click is done above; nudge again only after a long pause
                log.info("passport: Continue not taken yet — clicking again (%d)", tries)
                try:
                    human.click(self.page, self._continue_control(), timeout=4000)
                except Exception:  # noqa: BLE001
                    pass
            self.on_status(("passport step — VFS is rate-limiting the upload, waiting"
                            if self.upload_throttled else "passport step — waiting for Continue")
                           + f" ({int(deadline - time.monotonic())}s left)")
            self.page.wait_for_timeout(3000)
        log_event("upload", f"Passport step for {self.account.name} still waiting for Continue — giving up this sweep", "warn",
                  screenshot=self.screenshot("passport_timeout"))
        raise PassportPending(self.url)

    def _continue_control(self):
        """VFS renders Continue sometimes as a <button>, sometimes as a link/span — take whichever
        is on the page."""
        pat = re.compile(r"^\s*(continue|submit|next|proceed)\s*$", re.I)
        for loc in (self.page.get_by_role("button", name=pat),
                    self.page.get_by_role("link", name=pat),
                    self.page.locator("a, button, span[role=button], div[role=button]").filter(has_text=pat)):
            try:
                if loc.count():
                    return loc.first
            except Exception:  # noqa: BLE001
                continue
        return self.page.get_by_text(pat).first

    def on_status(self, what: str) -> None:
        """Hook: the CLI replaces this to show what the login is waiting for on the dashboard."""

    def on_human_needed(self, what: str) -> None:
        """Hook: the CLI replaces this to send a WhatsApp nudge."""

    def on_otp_required(self) -> None:
        """Hook: the CLI replaces this to send a Telegram/email nudge."""

    def _prefill_login(self) -> None:
        if not self.account.email:
            return
        try:
            user = self.page.locator("input[formcontrolname='username']")
            user.wait_for(state="visible", timeout=10_000)
            # The SPA re-renders the form once its translations arrive; wait for that to settle.
            try:
                self.page.wait_for_load_state("networkidle", timeout=8_000)
            except PWTimeout:
                pass
            for _ in range(3):
                if user.input_value() == self.account.email:
                    return
                human.wander(self.page, 1)
                human.type_into(self.page, user, self.account.email)
                human.type_into(self.page, self.page.locator("input[formcontrolname='password']"), self.account.password)
                human.nap(self.page, 900, 2200)  # verify it stuck
        except Exception:  # noqa: BLE001
            log.debug("could not pre-fill login form")

    # ---- booking form -------------------------------------------------------------

    def _open_new_booking(self) -> None:
        if "/application-detail" not in self.url:
            if self._on_login_page():
                raise LoginRequired(self.url)
            dash = self.page.get_by_role("link", name=re.compile(r"^dashboard$", re.I))
            if "/dashboard" not in self.url and dash.count():
                human.click(self.page, dash.first)   # in-app navigation, no reload
                human.dwell(self.page, 800, 2200)
            elif "/dashboard" not in self.url:
                self.goto(f"{self.cfg.base_url}/dashboard")
                self._wait_for_spa()
            if self._on_login_page():
                raise LoginRequired(self.url)
            btn = self.page.get_by_role("button", name=re.compile(r"start new booking", re.I))
            if btn.count():
                human.nap(self.page, 400, 1800)
                human.click(self.page, btn.first)
            else:
                self.goto(f"{self.cfg.base_url}/application-detail")
            human.dwell(self.page, 1200, 3000)
            if self._on_login_page():
                raise LoginRequired(self.url)

    def _select_option(self, index: int, wanted: str) -> str:
        """Open the index-th mat-select on the page and choose `wanted` (partial, case-insensitive)
        or the first real option if `wanted` is empty. Returns the chosen option text.
        Dependent dropdowns are re-populated from the API after the previous choice, so the
        option panel can re-render mid-click; retry by reopening it."""
        selects = self.page.locator("mat-select")
        options = self.page.locator("mat-option")
        # Skip if this dropdown already shows the wanted value (category/sub-category don't change
        # between centres, so re-picking them is wasteful and is what times out).
        cur = self._selected_text(index)
        if wanted:
            if cur and wanted.lower() in cur.lower():
                return cur
        elif cur and not cur.lower().startswith("select"):
            return cur      # e.g. sub-category kept from the previous centre — the site already re-queried
        last_err: Exception | None = None
        for attempt in range(5):
            try:
                if attempt == 3:
                    # the list will not populate — re-open the booking form so Angular rebuilds it
                    log.info("re-opening the booking form before the last try at dropdown %d", index)
                    self.goto(f"{self.cfg.base_url}/application-detail")
                    self._wait_for_spa()
                    human.dwell(self.page, 900, 2200)
                    if self._on_login_page():
                        raise LoginRequired(self.url)
                    selects = self.page.locator("mat-select")
                    options = self.page.locator("mat-option")
                    if index > 0:              # re-pick the centre first, the form is blank again
                        self._select_option(0, self._centre_hint or "")
                selects.nth(index).wait_for(state="visible")
                self.page.wait_for_timeout(human.jitter(300, 900) + 600 * attempt)
                human.click(self.page, selects.nth(index))
                options.first.wait_for(state="visible", timeout=8000)
                human.nap(self.page, 350, 1100)   # read the list
                texts = [t.strip() for t in options.all_inner_texts()]
                real = [t for t in texts if t and not t.lower().startswith("select")]
                if wanted:
                    match = [t for t in texts if wanted.lower() in t.lower()]
                    if not match:
                        self.page.keyboard.press("Escape")
                        raise RuntimeError(f"Option '{wanted}' not found. Available: {real}")
                    choice = match[0]
                else:
                    choice = real[0] if real else texts[0]
                opt = options.get_by_text(choice, exact=True).first
                opt.wait_for(state="visible", timeout=5000)
                box = opt.bounding_box()
                if box:                       # drift the pointer there like a person would...
                    human.move_to(self.page, box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
                    human.nap(self.page, 80, 300)
                opt.click(timeout=5000)       # ...but let Playwright click: it waits for the list to stop animating
                human.nap(self.page, 1100, 2600)   # site reloads the next dropdown / slot info from the API
                got = self._selected_text(index)
                if wanted and got and wanted.lower() not in got.lower():
                    raise ValueError(f"picked '{got}' instead of '{choice}'")   # neighbour got clicked — retry
                return choice
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001  (option panel re-rendered, detached, wrong neighbour, etc.)
                last_err = e
                log.debug("select %d attempt %d failed: %s", index, attempt, str(e).splitlines()[0])
                try:
                    self.page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
        what = {0: "the centre list", 1: "the category list", 2: "the sub-category list"}.get(index, f"dropdown {index}")
        raise RuntimeError(f"{what} did not open: {str(last_err).splitlines()[0][:90]}")

    def discover(self) -> dict[str, list[str]]:
        """Print the options of every dropdown on the booking form (helps fill config.yaml)."""
        self._open_new_booking()
        out: dict[str, list[str]] = {}
        selects = self.page.locator("mat-select")
        n = selects.count()
        for i in range(n):
            label = ""
            try:
                label = selects.nth(i).evaluate(
                    "el => (el.closest('mat-form-field')?.innerText || el.getAttribute('aria-label') || '').split('\\n')[0]"
                ).strip()
            except Exception:  # noqa: BLE001
                pass
            selects.nth(i).click()
            options = self.page.locator("mat-option")
            try:
                options.first.wait_for(state="visible", timeout=5000)
                texts = [t.strip() for t in options.all_inner_texts()]
            except PWTimeout:
                texts = []
            self.page.keyboard.press("Escape")
            out[label or f"dropdown[{i}]"] = texts
            # choose the first real option so dependent dropdowns get populated
            real = [t for t in texts if t and not t.lower().startswith("select")]
            if real:
                selects.nth(i).click()
                options.filter(has_text=real[0]).first.click()
                self.page.wait_for_timeout(1200)
        return out

    SHOTS = Path("state/screenshots")

    def screenshot(self, tag: str) -> str | None:
        """Save a screenshot for the event log; returns the relative path or None."""
        try:
            self._ensure_page()
            self.SHOTS.mkdir(parents=True, exist_ok=True)
            name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{re.sub(r'[^a-z0-9]+', '_', tag.lower())}.png"
            self.page.screenshot(path=str(self.SHOTS / name), full_page=True)
            shots = sorted(self.SHOTS.glob("*.png"), key=lambda f: f.stat().st_mtime)
            for old in shots[:-400]:          # keep the newest 400
                old.unlink(missing_ok=True)
            return name
        except Exception as e:  # noqa: BLE001
            log.debug("screenshot failed: %s", e)
            return None

    def keepalive(self) -> None:
        """Nudge the page so VFS's 20-minute idle logout (ng2-idle) doesn't fire between checks."""
        try:
            self._ensure_page()
            human.wander(self.page, 1)
        except Exception:  # noqa: BLE001
            pass

    def _answered_for(self, centre: str) -> bool:
        """True when the form shows the wanted centre plus a definitive slot answer."""
        try:
            if short_centre(self._selected_text(0)).lower() != short_centre(centre).lower():
                return False
            if self._last_api is not None:
                return True
            text = self.page.locator("body").inner_text(timeout=3000)
            return bool(NO_SLOT_RE.search(text) or EARLIEST_RE.search(text))
        except Exception:  # noqa: BLE001
            return False

    def _selected_text(self, index: int) -> str:
        try:
            return self.page.locator("mat-select").nth(index).inner_text(timeout=2000).strip()
        except Exception:  # noqa: BLE001
            return ""

    def check(self, centre: str | None = None) -> SlotResult:
        """Fill the booking form for one centre (default: first configured) and report availability."""
        centre = self.cfg.centre_list[0] if centre is None else centre
        self._centre_hint = centre
        self._last_api = None
        self._open_new_booking()
        wanted = [centre, self.cfg.category, self.cfg.subcategory]
        n = min(self.page.locator("mat-select").count(), len(wanted))
        chosen = []
        for i in range(n):
            # Re-selecting an identical value fires no change event (and no API call), so when the
            # centre is already selected we still re-pick category/sub-category to force a refresh.
            try:
                chosen.append(self._select_option(i, wanted[i]))
            except Exception:  # noqa: BLE001
                # A dropdown that will not re-open is fine *if* VFS has already answered for this
                # centre (the form keeps category/sub-category and answers on the centre change).
                if i >= 1 and self._answered_for(centre):
                    log.debug("dropdown %d stayed shut but the page already answered for %s", i, centre)
                    break
                raise
            if i >= 1 and self._last_api is not None:
                # category/sub-category were kept from the previous centre and the site already
                # called CheckIsSlotAvailable for this one — nothing more to pick
                self.page.wait_for_timeout(human.jitter(300, 900))
                break
        log.debug("selected: %s", chosen)
        full_centre = chosen[0] if chosen else centre

        self._last_slot_call = time.monotonic()
        # Give the SPA a moment to call CheckIsSlotAvailable and render the message.
        for _ in range(12):
            self.page.wait_for_timeout(human.jitter(500, 1000))
            if self._last_api is not None:
                break
        human.dwell(self.page, 700, 2400)   # "read" the answer
        text = self.page.locator("body").inner_text()

        if self._last_api is not None:
            api = self._last_api
            earliest = api.get("earliestDate") or api.get("earliestSlotDate")
            err = api.get("error")
            available = bool(earliest) and not err
            return SlotResult(available, _fmt_date(earliest) if earliest else None, "api", text, api, full_centre)

        if NO_SLOT_RE.search(text):
            return SlotResult(False, None, "text", text, {}, full_centre)
        m = EARLIEST_RE.search(text)
        if m:
            return SlotResult(True, m.group(1), "text", text, {}, full_centre)
        enabled_days = self.page.locator(".mat-calendar-body-cell:not(.mat-calendar-body-disabled)").count()
        if enabled_days:
            return SlotResult(True, "see calendar", "text", text, {}, full_centre)
        return SlotResult(False, None, "unknown", text, {}, full_centre)

    _FETCH_JS = """async ({url, headers, body}) => {
      try { const r = await fetch(url, {method: 'POST', headers, body: JSON.stringify(body)});
            return {status: r.status, text: await r.text()}; }
      catch (e) { return {status: 0, text: String(e)}; }
    }"""

    def _centre_codes(self, first_full_name: str) -> tuple[str, dict[str, str]] | None:
        """From the captured master lists + request body work out (a) which body key carries the
        centre code and (b) short centre name -> code for every centre. None if we can't be sure."""
        if not self._api_template:
            return None
        body = self._api_template["body"]
        wanted = {short_centre(c) for c in self.cfg.centre_list}
        for data in self._master_lists.values():
            # the centre list: some item carries the full name of the centre we just selected
            hit = next((it for it in data if any(isinstance(v, str) and v.strip() == first_full_name.strip() for v in it.values())), None)
            if not hit:
                continue
            # the code key: an item value that the request body also contains AND that differs per
            # centre (missionCode/countryCode are in the body too but are the same for every centre)
            cands = [k for k, v in hit.items() if isinstance(v, str) and v and v in body.values()]
            cands = [k for k in cands if len({it.get(k) for it in data}) > 1]
            if not cands:
                continue
            code_key = max(cands, key=lambda k: len({it.get(k) for it in data}))
            body_key = next(bk for bk, bv in body.items() if bv == hit[code_key])
            codes: dict[str, str] = {}
            for it in data:
                name = next((v for v in it.values() if isinstance(v, str) and short_centre(v) in wanted), "")
                if name and isinstance(it.get(code_key), str) and it[code_key]:
                    codes[short_centre(name)] = it[code_key]
            if len(codes) >= 2:
                return body_key, codes
        return None

    # Measured against the live site: CheckIsSlotAvailable accepts just `authorize` +
    # `content-type`. The SPA also sends `route` and a per-request `clientsource`, but including
    # those makes VFS answer 409 "Repeated Delay" — it is the leaner request that is honoured.
    API_HEADERS = ("authorize", "content-type")

    def _api_request(self, body: dict) -> tuple[int, str]:
        # VFS answers 401101 "Invalid Request" when two slot checks arrive close together — the
        # spacing matters more than the headers, so wait out the gap before asking.
        lo, hi = self.cfg.api_pause_seconds
        since = time.monotonic() - self._last_slot_call
        need = random.uniform(lo, hi) - since
        if need > 0:
            self.on_status(f"waiting {need:.0f}s before the next centre")
            self.page.wait_for_timeout(int(need * 1000))
        self._last_slot_call = time.monotonic()
        hdrs = {k: v for k, v in self._api_template["headers"].items() if k.lower() in self.API_HEADERS}
        # prefer the token the page is holding right now; it rotates as the session is used
        try:
            live = self.page.evaluate("() => sessionStorage.getItem('JWT') || ''")
            if live:
                for k in list(hdrs):
                    if k.lower() == "authorize":
                        hdrs[k] = live
        except Exception:  # noqa: BLE001
            pass
        r = self.page.evaluate(self._FETCH_JS, {"url": self._api_template["url"], "headers": hdrs, "body": body})
        return r["status"], r["text"]

    @staticmethod
    def _repeated_delay(status: int, text: str) -> int | None:
        """VFS answers 409 {"code": N, "description": "Repeated Delay"} — N is how many seconds it
        wants us to wait. Returns those seconds, or None when this is not that answer."""
        if status != 409:
            return None
        try:
            d = json.loads(text)
        except Exception:  # noqa: BLE001
            return None
        if "repeated delay" not in str(d.get("description", "")).lower():
            return None
        try:
            return max(1, min(120, int(d.get("code"))))
        except Exception:  # noqa: BLE001
            return 30

    def _check_via_api(self, centre: str, key: str, code: str) -> SlotResult:
        """Ask the API directly for one centre, from inside the logged-in page (so it rides the
        session, cookies and proxy). Honours VFS's own "wait N seconds" answer."""
        body = dict(self._api_template["body"])
        body[key] = code
        for attempt in range(3):
            status, text = self._api_request(body)
            wait = self._repeated_delay(status, text)
            if wait is None:
                break
            if attempt == 2:
                raise RuntimeError(f"still rate-limited after waiting ({wait}s asked)")
            log.info("VFS asked for %ds before the next check — waiting", wait)
            self.on_status(f"VFS asked to wait {wait}s before the next centre")
            self.page.wait_for_timeout((wait + random.uniform(1, 3)) * 1000)
        if status in (401, 403):
            # the session token the SPA handed us has aged out; only the SPA can mint a fresh one
            self._api_template = None
            raise TokenStale(f"API {status}: {text[:80]}")
        if status != 200:
            raise RuntimeError(f"API {status}: {text[:100]}")
        api = json.loads(text)
        earliest = api.get("earliestDate") or api.get("earliestSlotDate")
        err = api.get("error")
        # error 1035 is simply "no slots available" — a real answer, not a failure
        if isinstance(err, dict) and err.get("code") not in (None, 0, 1035):
            raise RuntimeError(f"API error {err.get('code')}: {str(err.get('description'))[:80]}")
        return SlotResult(bool(earliest), _fmt_date(earliest) if earliest else None, "api", "", api, centre)

    def check_all(self, centres: list[str] | None = None, on_progress=None) -> list[SlotResult]:
        """Check the given centres (default: all configured) through the booking form, in order.
        VFS rate-limits CheckIsSlotAvailable to ~7 calls per login (HTTP 429, then it logs the session
        out), so callers pass a slice of the priority list and we stop at the first 429 — the centres
        left over are reported as 'not checked' and picked up by the next login."""
        results: list[SlotResult] = []
        self.partial_results = results          # visible to the caller even if we raise mid-way
        centres = list(centres if centres is not None else self.cfg.centre_list)
        api_key, codes = "", {}
        for i, centre in enumerate(centres):
            if self.rate_limited:
                log.warning("VFS rate limit hit — %d centre(s) left for the next login: %s", len(centres) - i, ", ".join(short_centre(c) for c in centres[i:]))
                results += [SlotResult(False, None, "skipped", "", {}, c, error="not checked: VFS rate limit — next login") for c in centres[i:]]
                if on_progress:
                    for c in centres[i:]:
                        on_progress(c, "skipped", None, i, len(centres))
                break
            if on_progress:
                on_progress(centre, "checking", None, i, len(centres))
            r = None
            if self.cfg.api_replay and api_key and self._api_template and short_centre(centre) in codes:
                try:
                    r = self._check_via_api(centre, api_key, codes[short_centre(centre)])
                    r.centre = centre
                except TokenStale as e:
                    # expected every few calls: check this centre through the form, which refreshes
                    # the token, and the centres after it go back to the fast path
                    log.info("session token aged out (%s) — refreshing it with a form check", str(e)[:60])
                    self.on_status(f"refreshing the session token on {short_centre(centre)}")
                    r = None
                except Exception as e:  # noqa: BLE001
                    log.warning("API check %s failed (%s) — using the form", centre, str(e).splitlines()[0][:80])
                    r = None
            if r is None:
                try:
                    if self._last_api is None and i:      # coming back from the API path
                        self.goto(f"{self.cfg.base_url}/application-detail")
                        self._wait_for_spa()
                        human.dwell(self.page, 800, 2000)
                    r = self.check(centre)
                except LoginRequired:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("check %s failed: %s", centre, str(e).splitlines()[0])
                    r = SlotResult(False, None, "error", "", {}, centre, error=str(e).splitlines()[0][:80])
                    if self._on_login_page():
                        raise LoginRequired(self.url)
                if self.cfg.api_replay and not api_key:
                    found = self._centre_codes(r.centre)
                    if found:
                        api_key, codes = found
                        log.info("API mode: centre key '%s', %d centre codes known — remaining centres via API", api_key, len(codes))
                        try:
                            redacted = {**self._api_template, "headers": {k: ("<redacted>" if k.lower() in ("authorize", "authorization") else v)
                                                                            for k, v in self._api_template["headers"].items()}}
                            Path("state/api_capture.json").write_text(json.dumps({"template": redacted, "key": api_key, "codes": codes}, indent=2))
                        except Exception:  # noqa: BLE001
                            pass
                    else:
                        log.warning("API request not captured — checking all centres through the form (slow)")
                pause = self.cfg.centre_pause_seconds * 1000
                self.page.wait_for_timeout(human.jitter(pause * 0.5, pause * 1.7))
            else:
                self.page.wait_for_timeout(random.randint(300, 900))   # spacing is enforced per call
            results.append(r)
            log.info("  %s", r.summary())
            if on_progress:
                on_progress(centre, "done", r, i, len(centres))
        return results
