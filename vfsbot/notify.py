"""Notifications — WhatsApp Web only. Everything is handed to `vfsbot.ack` in a separate process
so the watcher's browser session is never blocked by WhatsApp's."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from .config import Config, NotifyConfig

log = logging.getLogger("vfsbot.notify")


def _spawn(*args: str) -> None:
    out = open(Path("state") / "ack.out", "a")
    subprocess.Popen([sys.executable, "-m", "vfsbot.ack", *args], cwd=Path("."), stdout=out,
                     stderr=subprocess.STDOUT, start_new_session=True)


class Notifier:
    def __init__(self, cfg: NotifyConfig, _secrets=None):
        self.cfg = cfg

    @staticmethod
    def _has_issue_contacts() -> bool:
        w = Config.load().whatsapp_web
        return w.enabled and any(c.bot_issues for c in w.active_contacts())

    def notice(self, text: str) -> bool:
        """Message-only notice to the contacts flagged 'bot issues'."""
        if not self._has_issue_contacts():
            log.info("no bot-issue WhatsApp contact — notice dropped: %s", text[:80])
            return False
        _spawn("notice", text)
        return True

    def slot_found(self, message: str, key: str) -> list[str]:
        """Full escalation (message everyone, call the 'message_call' contacts until acked)."""
        w = Config.load().whatsapp_web
        if not (w.enabled and w.active_contacts()):
            log.warning("slot found but WhatsApp is disabled / has no contacts!")
            return []
        _spawn("slot", message, key)
        return ["whatsapp"]

    def human_needed(self, what: str) -> None:
        self.notice(f"\u26a0\ufe0f VFS bot needs a human: {what}")

    def login_needed(self, url: str) -> None:
        self.human_needed(f"session expired / Cloudflare check — log in again in the bot's browser window.\n{url}")

    def otp_needed(self, otp_file: str) -> None:
        self.human_needed(f"VFS sent an OTP. Type it in the dashboard's OTP box (or in the bot's browser window).")

    def heartbeat(self, text: str) -> None:
        self.notice(f"\U0001F4A4 VFS bot: {text}")

    def error(self, text: str) -> None:
        self.notice(f"\u274c VFS bot error: {text}")

    def test_all(self) -> dict[str, bool]:
        return {"whatsapp (bot-issue contacts)": self.notice("\u2705 VFS bot test message")}
