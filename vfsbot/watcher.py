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
import random
import re
import shutil
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext, Page, Response, TimeoutError as PWTimeout, sync_playwright

from .config import Config, Secrets
from .events import log_event
from .otp import fetch_latest_otp

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


OTP_FILE = Path("state/otp.txt")
BROWSER_CANDIDATES = [
    "/opt/brave.com/brave/brave", "brave-browser", "brave",
    "/opt/google/chrome/chrome", "google-chrome", "google-chrome-stable",
    "chromium", "chromium-browser",
]


def find_browser(configured: str = "") -> str | None:
    if configured:
        return configured
    for c in BROWSER_CANDIDATES:
        if c.startswith("/") and Path(c).exists():
            return c
        if not c.startswith("/") and shutil.which(c):
            return shutil.which(c)
    return None


class Blocked(Exception):
    """VFS/Cloudflare WAF returned a block page (e.g. {"code": "403201"}). Happens with headless
    browsers or after too many requests. Nothing to do but back off / use a visible browser."""


BLOCK_RE = re.compile(r'"code"\s*:\s*"403\d*"|access denied|attention required|cloudflare', re.I)


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
    def __init__(self, cfg: Config, secrets: Secrets):
        self.cfg = cfg
        self.secrets = secrets
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self._last_api: dict | None = None

    # ---- lifecycle ----------------------------------------------------------------

    def __enter__(self) -> "Watcher":
        self._pw = sync_playwright().start()
        profile = Path(self.cfg.browser.profile_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        exe = find_browser(self.cfg.browser.executable)
        if exe:
            log.info("using browser: %s", exe)
        else:
            log.warning("No real Chrome/Brave found — falling back to Playwright's Chromium, which FAILS "
                        "Cloudflare's check on VFS. Install Brave or Google Chrome (non-flatpak).")
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(profile),
            executable_path=exe,
            headless=self.cfg.browser.headless,
            no_viewport=True,   # no emulation at all: Cloudflare flags locale/timezone/viewport overrides
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(20_000)
        self.page.on("response", self._on_response)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ---- helpers ------------------------------------------------------------------

    def _on_response(self, resp: Response) -> None:
        if "CheckIsSlotAvailable" in resp.url or "/appointment/slots" in resp.url:
            try:
                self._last_api = resp.json()
                log.debug("API %s -> %s", resp.url, json.dumps(self._last_api)[:300])
            except Exception:  # noqa: BLE001
                pass

    @property
    def url(self) -> str:
        return self.page.url if self.page else ""

    def _ensure_page(self) -> None:
        """Re-open a tab if the user closed it (the persistent context keeps the session)."""
        if self.page is None or self.page.is_closed():
            log.warning("browser tab was closed — opening a new one")
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            self.page.set_default_timeout(20_000)
            self.page.on("response", self._on_response)

    def _on_login_page(self) -> bool:
        """True only if we actually need to authenticate. The post-OTP 'upload passport' page
        keeps /login in its URL, so check for the credential/OTP form, not just the URL."""
        if "/login" not in self.url:
            return False
        try:
            return self.page.locator(f"{self.LOGIN_FORM}, {self.OTP_FORM}").count() > 0
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
        """If the user dropped the OTP into state/otp.txt, type it and submit."""
        if not OTP_FILE.exists():
            return False
        code = OTP_FILE.read_text().strip()
        OTP_FILE.unlink(missing_ok=True)
        if not code:
            return False
        box = self.page.locator(self.OTP_FORM).first
        if not box.count():
            box = self.page.locator("input:not([type=hidden])").last
        box.fill(code)
        log.info("OTP entered from %s", OTP_FILE)
        try:
            self._submit_button().click(timeout=5000)
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

    def _raise_if_blocked(self) -> None:
        try:
            text = self.page.locator("body").inner_text(timeout=3000)
        except Exception:  # noqa: BLE001
            return
        if len(text) < 400 and BLOCK_RE.search(text):
            log_event("blocked", "VFS/Cloudflare block page", "error", {"text": text[:200]}, self.screenshot("blocked"))
            raise Blocked(text.strip()[:200])

    def _has_turnstile(self) -> bool:
        try:
            return self.page.locator("iframe[src*='challenges.cloudflare.com'], .cf-turnstile").count() > 0
        except Exception:  # noqa: BLE001
            return False

    # ---- login --------------------------------------------------------------------

    def ensure_logged_in(self, wait_minutes: int = 10) -> None:
        """Go to the dashboard. If we land on /login, pre-fill creds and wait for a human
        to clear Cloudflare + press Sign In (or press it ourselves if the check auto-passes)."""
        self.page.goto(f"{self.cfg.base_url}/dashboard", wait_until="domcontentloaded")
        self._wait_for_spa()
        self._dismiss_cookie_banner()
        if not self._on_login_page():
            return

        log.warning("Login required — please complete the Cloudflare check / sign in in the browser window.")
        log_event("login", "Login required — signing in", "warn", screenshot=self.screenshot("login"))
        self._prefill_login()
        login_started = datetime.now().astimezone()

        deadline = wait_minutes * 60_000
        step = 2_000
        waited = 0
        clicked = False
        otp_announced = False
        while waited < deadline:
            if self.page.is_closed():
                self._ensure_page()
                self.page.goto(f"{self.cfg.base_url}/dashboard", wait_until="domcontentloaded")
                self._wait_for_spa()
            if self._is_logged_in() or self._on_passport_upload():
                log.info("Logged in.")
                log_event("login", "Logged in", "info")
                self.page.wait_for_timeout(1500)
                if self._on_passport_upload():
                    self._upload_passport()
                return
            if self._on_otp_step():
                if not otp_announced:
                    log.warning("OTP required — type it in the browser window, or write it to %s", OTP_FILE)
                    log_event("otp", "OTP requested by VFS (sent to the account's email/SMS/WhatsApp)", "warn")
                    otp_announced = True
                    otp_asked_at = datetime.now().astimezone()
                    self.on_otp_required()
                if not self._try_submit_otp() and waited % 10_000 == 0 and self.secrets.imap_user:
                    code = fetch_latest_otp(self.secrets.imap_host, self.secrets.imap_user,
                                            self.secrets.imap_password, not_before=otp_asked_at)
                    if code:
                        OTP_FILE.parent.mkdir(parents=True, exist_ok=True)
                        OTP_FILE.write_text(code)
                        log_event("otp", "OTP fetched automatically from inbox", "info")
                        self._try_submit_otp()
            else:
                # If Turnstile has already produced a token and the button is enabled, click once.
                if not clicked and self.secrets.vfs_email:
                    try:
                        token = self.page.input_value("input[name='cf-turnstile-response']", timeout=500)
                        btn = self._submit_button()
                        if token and btn.count() and btn.is_enabled():
                            btn.click()
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
            self.page.wait_for_timeout(step)
            waited += step
        log_event("login", "Gave up waiting for login/OTP", "error", screenshot=self.screenshot("login_timeout"))
        raise (OtpRequired if self._on_otp_step() else LoginRequired)(self.url)

    def _upload_passport(self) -> None:
        """One-time account step after first login: VFS wants the passport bio page.
        We select the file and stop — a human presses Continue (VFS locks the extracted data)."""
        f = Path(self.cfg.passport_file)
        if not f.exists():
            log_event("upload", f"VFS asks for the passport bio page but {f} is missing", "error",
                      screenshot=self.screenshot("passport_missing"))
            return
        try:
            self.page.locator("input[type=file]").first.set_input_files(str(f))
            log_event("upload", f"Passport file selected ({f.name}) — press Continue in the browser window", "warn",
                      screenshot=self.screenshot("passport_selected"))
            self.on_human_needed("Passport upload: press Continue in the bot's browser window")
        except Exception as e:  # noqa: BLE001
            log_event("upload", f"Passport upload failed: {e}", "error", screenshot=self.screenshot("passport_error"))

    def on_human_needed(self, what: str) -> None:
        """Hook: the CLI replaces this to send a Telegram nudge."""

    def on_otp_required(self) -> None:
        """Hook: the CLI replaces this to send a Telegram/email nudge."""

    def _prefill_login(self) -> None:
        if not self.secrets.vfs_email:
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
                if user.input_value() == self.secrets.vfs_email:
                    return
                user.fill(self.secrets.vfs_email)
                self.page.fill("input[formcontrolname='password']", self.secrets.vfs_password)
                self.page.wait_for_timeout(1500)  # verify it stuck
        except Exception:  # noqa: BLE001
            log.debug("could not pre-fill login form")

    # ---- booking form -------------------------------------------------------------

    def _open_new_booking(self) -> None:
        if "/application-detail" not in self.url:
            if self._on_login_page():
                raise LoginRequired(self.url)
            dash = self.page.get_by_role("link", name=re.compile(r"^dashboard$", re.I))
            if "/dashboard" not in self.url and dash.count():
                dash.first.click()            # in-app navigation, no reload
                self.page.wait_for_timeout(1500)
            elif "/dashboard" not in self.url:
                self.page.goto(f"{self.cfg.base_url}/dashboard", wait_until="domcontentloaded")
                self._wait_for_spa()
            if self._on_login_page():
                raise LoginRequired(self.url)
            btn = self.page.get_by_role("button", name=re.compile(r"start new booking", re.I))
            if btn.count():
                btn.first.click()
            else:
                self.page.goto(f"{self.cfg.base_url}/application-detail", wait_until="domcontentloaded")
            self.page.wait_for_timeout(2000)
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
        if wanted:
            cur = self._selected_text(index)
            if cur and wanted.lower() in cur.lower():
                return cur
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                selects.nth(index).wait_for(state="visible")
                self.page.wait_for_timeout(600 * attempt)
                selects.nth(index).click()
                options.first.wait_for(state="visible", timeout=8000)
                self.page.wait_for_timeout(400)
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
                options.get_by_text(choice, exact=True).first.click(timeout=5000)
                self.page.wait_for_timeout(1500)   # site reloads the next dropdown / slot info from the API
                return choice
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001  (option panel re-rendered, detached, etc.)
                last_err = e
                log.debug("select %d attempt %d failed: %s", index, attempt, str(e).splitlines()[0])
                try:
                    self.page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
        raise RuntimeError(f"could not choose option for dropdown {index}: {last_err}")

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
            return name
        except Exception as e:  # noqa: BLE001
            log.debug("screenshot failed: %s", e)
            return None

    def keepalive(self) -> None:
        """Nudge the page so VFS's 20-minute idle logout (ng2-idle) doesn't fire between checks."""
        try:
            self._ensure_page()
            x = random.randint(200, 600)
            self.page.mouse.move(x, 300)
            self.page.mouse.move(x + 15, 310)
        except Exception:  # noqa: BLE001
            pass

    def _selected_text(self, index: int) -> str:
        try:
            return self.page.locator("mat-select").nth(index).inner_text(timeout=2000).strip()
        except Exception:  # noqa: BLE001
            return ""

    def check(self, centre: str | None = None) -> SlotResult:
        """Fill the booking form for one centre (default: first configured) and report availability."""
        centre = self.cfg.centre_list[0] if centre is None else centre
        self._last_api = None
        self._open_new_booking()
        wanted = [centre, self.cfg.category, self.cfg.subcategory]
        n = min(self.page.locator("mat-select").count(), len(wanted))
        chosen = []
        for i in range(n):
            # Re-selecting an identical value fires no change event (and no API call), so when the
            # centre is already selected we still re-pick category/sub-category to force a refresh.
            chosen.append(self._select_option(i, wanted[i]))
        log.debug("selected: %s", chosen)
        full_centre = chosen[0] if chosen else centre

        # Give the SPA a moment to call CheckIsSlotAvailable and render the message.
        for _ in range(12):
            self.page.wait_for_timeout(700)
            if self._last_api is not None:
                break
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

    def check_all(self) -> list[SlotResult]:
        """Check every configured centre, in config order (nearest first)."""
        results: list[SlotResult] = []
        for centre in self.cfg.centre_list:
            try:
                r = self.check(centre)
            except LoginRequired:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("check %s failed: %s", centre, str(e).splitlines()[0])
                r = SlotResult(False, None, "error", "", {}, centre, error=str(e).splitlines()[0][:80])
                if self._on_login_page():
                    raise LoginRequired(self.url)
            results.append(r)
            log.info("  %s", r.summary())
            pause = self.cfg.centre_pause_seconds
            self.page.wait_for_timeout(int(random.uniform(pause * 0.7, pause * 1.3) * 1000))
        return results
