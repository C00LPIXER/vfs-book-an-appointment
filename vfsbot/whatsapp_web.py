"""Drive an already-linked WhatsApp Web session in a dedicated Brave/Chrome profile:
send a message to a contact, place a voice call, and read their reply.

Link once:  vfsbot whatsapp-setup   (opens WhatsApp Web; scan the QR with the office phone:
WhatsApp > Linked devices > Link a device). The link persists in the profile.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

from .watcher import IGNORE_DEFAULT_ARGS, NO_RESTORE_ARGS, ensure_display, find_browser, prepare_profile, single_tab

log = logging.getLogger("vfsbot.whatsapp")
WA_URL = "https://web.whatsapp.com/"


class WhatsAppWeb:
    def __init__(self, profile_dir: str = "whatsapp-profile", executable: str = ""):
        self.profile_dir = profile_dir
        self.executable = executable
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> "WhatsAppWeb":
        ensure_display()
        self._pw = sync_playwright().start()
        try:
            return self._start()
        except Exception:
            try:
                self._pw.stop()
            finally:
                self._pw = None
            raise

    def _start(self) -> "WhatsAppWeb":
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        prepare_profile(Path(self.profile_dir))
        self.ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            executable_path=find_browser(self.executable),
            headless=False,
            no_viewport=True,
            args=NO_RESTORE_ARGS,
            ignore_default_args=IGNORE_DEFAULT_ARGS,
        )
        self.page = single_tab(self.ctx)
        self.page.set_default_timeout(30_000)
        return self

    def _close_extra_tabs(self) -> None:
        """WhatsApp session-restore reopens every old tab; keep just one so windows don't pile up."""
        try:
            pages = [pg for pg in self.ctx.pages if not pg.is_closed()]
            for pg in pages[1:]:
                try:
                    pg.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ---- session ------------------------------------------------------------------

    def _use_here(self) -> None:
        """WhatsApp shows 'open in another window — Use here' when the session is used elsewhere.
        React ignores a synthetic .click(); a real pointer event on the button's centre works."""
        for _ in range(4):
            box = self.page.evaluate("""() => {
                const t=[...document.querySelectorAll('button,div[role=button]')]
                    .find(e=>e.textContent.trim().toLowerCase()==='use here');
                if(!t) return null; const r=t.getBoundingClientRect();
                return {x:r.x+r.width/2, y:r.y+r.height/2};
            }""")
            if not box:
                return
            self.page.mouse.click(box["x"], box["y"])
            self.page.wait_for_timeout(2500)

    def open(self, wait_linked_seconds: int = 40) -> bool:
        self._close_extra_tabs()
        self.page.goto(WA_URL, wait_until="domcontentloaded")
        deadline = time.time() + wait_linked_seconds
        while time.time() < deadline:
            self._use_here()
            if self._search_box().count() and self.page.locator("#side").count():
                self.page.wait_for_timeout(1500)
                return True
            if self.page.get_by_text(re.compile(r"Link a device|Log into WhatsApp", re.I)).count():
                log.warning("WhatsApp Web is not linked yet — scan the QR (run: vfsbot whatsapp-setup)")
            self.page.wait_for_timeout(2000)
        return self.page.locator("#side").count() > 0

    def _search_box(self):
        """The chat-search box; WhatsApp changes this selector (now an <input>), so try several."""
        for sel in ("input[aria-label*='Search' i]",
                    "div[contenteditable='true'][data-tab='3']",
                    "div[role='textbox'][aria-label*='Search' i]",
                    "[aria-label='Search or start a new chat']"):
            loc = self.page.locator(sel).first
            if loc.count():
                return loc
        return self.page.locator("#side [contenteditable='true'], #side input").first

    def is_linked(self) -> bool:
        return self.page.locator("#side").count() > 0

    # ---- chat ---------------------------------------------------------------------

    def open_chat(self, name: str) -> bool:
        self._use_here()
        box = self._search_box()
        box.click()
        try:
            box.fill(name)
        except Exception:  # noqa: BLE001
            box.type(name, delay=25)
        self.page.wait_for_timeout(2500)
        # match the chat title case-insensitively (config casing may differ from WhatsApp's)
        hit = self.page.get_by_title(re.compile(re.escape(name), re.I)).first
        if not hit.count():
            hit = self.page.locator("#pane-side [role='listitem']").filter(
                has_text=re.compile(re.escape(name), re.I)).first
        if not hit.count():
            log.warning("chat '%s' not found", name)
            return False
        hit.click()
        self.page.wait_for_timeout(1500)
        return True

    def send_message(self, text: str) -> bool:
        try:
            entry = self.page.locator("div[contenteditable='true'][data-tab='10'], footer div[contenteditable='true']").last
            entry.click()
            for line in text.split("\n"):
                entry.type(line, delay=8)
                self.page.keyboard.down("Shift"); self.page.keyboard.press("Enter"); self.page.keyboard.up("Shift")
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(1500)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("send_message failed: %s", e)
            return False

    def place_call(self, ring_seconds: int = 25) -> bool:
        """Click the chat's Voice call button, let it ring, then hang up (a missed call)."""
        try:
            self._use_here()
            # the exact 'Voice call' button in the conversation header (NOT 'Video call'/'Calls')
            btn = self.page.locator("[aria-label='Voice call']").first
            if not btn.count():
                btn = self.page.get_by_role("button", name="Voice call", exact=True).first
            if not btn.count():
                log.warning("Voice call button not found")
                return False
            btn.click(timeout=8000)
            log.info("call placed, ringing ~%ss", ring_seconds)
            self.page.wait_for_timeout(2000)
            self._use_here()   # a call can trigger the 'use here' dialog again
            end = time.time() + ring_seconds
            while time.time() < end:
                self.page.wait_for_timeout(1500)
                # if the callee answered or it ended, stop waiting
                if not self._call_active():
                    break
            self._end_call()
            self.page.wait_for_timeout(1500)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("place_call failed: %s", e)
            self._end_call()
            return False

    def _call_active(self) -> bool:
        try:
            return self.page.locator("[aria-label='End call'], [data-testid='end-call']").count() > 0
        except Exception:  # noqa: BLE001
            return True

    def _end_call(self) -> None:
        for sel in ["[aria-label='End call']", "[aria-label*='End call' i]", "[data-testid='end-call']",
                    "[aria-label='Cancel']", "[aria-label*='Cancel' i]"]:
            try:
                el = self.page.locator(sel).first
                if el.count():
                    el.click(timeout=2000)
                    return
            except Exception:  # noqa: BLE001
                pass
        # last resort: close any call window/tab that opened
        try:
            for pg in list(self.ctx.pages):
                if pg is not self.page and ("call" in (pg.url or "").lower()):
                    pg.close()
        except Exception:  # noqa: BLE001
            pass

    def latest_incoming_text(self) -> str:
        """Text of the most recent message *received* (their side) in the open chat."""
        try:
            bubbles = self.page.locator("div.message-in span.selectable-text")
            n = bubbles.count()
            return bubbles.nth(n - 1).inner_text().strip() if n else ""
        except Exception:  # noqa: BLE001
            return ""

    def has_new_reply(self, baseline: int, keyword: str) -> bool:
        """True if the open chat has more incoming messages than `baseline` and the newest one
        contains `keyword` (case-insensitive)."""
        return self.incoming_count() > baseline and bool(re.search(re.escape(keyword), self.latest_incoming_text(), re.I))

    def wait_for_reply(self, keyword: str, timeout_seconds: int) -> bool:
        """Return True if a NEW incoming message matching `keyword` arrives within the timeout."""
        baseline = self.incoming_count()
        end = time.time() + timeout_seconds
        while time.time() < end:
            self.page.wait_for_timeout(3000)
            if self.has_new_reply(baseline, keyword):
                return True
        return False

    def incoming_count(self) -> int:
        try:
            return self.page.locator("div.message-in").count()
        except Exception:  # noqa: BLE001
            return 0
