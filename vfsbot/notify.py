from __future__ import annotations

import logging
import smtplib
from datetime import datetime, time
from email.message import EmailMessage

import httpx

from .config import NotifyConfig, Secrets
from .events import log_event

log = logging.getLogger("vfsbot.notify")


class Notifier:
    def __init__(self, cfg: NotifyConfig, secrets: Secrets):
        self.cfg = cfg
        self.s = secrets

    # ---- channels -------------------------------------------------------------

    def telegram(self, text: str) -> bool:
        if not (self.s.telegram_bot_token and self.s.telegram_chat_id):
            return False
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        try:
            r = httpx.post(url, json={"chat_id": self.s.telegram_chat_id, "text": text}, timeout=15)
            r.raise_for_status()
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("telegram failed: %s", e)
            log_event("alert", f"Telegram send failed: {e}", "error")
            return False

    def email(self, subject: str, body: str) -> bool:
        if not (self.s.smtp_user and self.s.smtp_password and self.s.email_to):
            return False
        msg = EmailMessage()
        msg["From"] = self.s.smtp_user
        msg["To"] = self.s.email_to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(self.s.smtp_user, self.s.smtp_password)
                smtp.send_message(msg)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("email failed: %s", e)
            log_event("alert", f"Email send failed: {e}", "error")
            return False

    def _twilio(self):
        if not (self.s.twilio_account_sid and self.s.twilio_auth_token):
            return None
        from twilio.rest import Client

        return Client(self.s.twilio_account_sid, self.s.twilio_auth_token)

    def call(self, spoken: str) -> bool:
        if not (self.s.twilio_from and self.s.call_to):
            return False
        if self._in_quiet_hours():
            log.info("quiet hours — skipping phone call")
            return False
        try:
            client = self._twilio()
            if client is None:
                return False
            twiml = "<Response>" + "".join(f"<Say voice='alice'>{spoken}</Say><Pause length='1'/>" for _ in range(3)) + "</Response>"
            ok = True
            for to in [x.strip() for x in self.s.call_to.split(",") if x.strip()]:
                client.calls.create(to=to, from_=self.s.twilio_from, twiml=twiml)
            return ok
        except Exception as e:  # noqa: BLE001
            log.warning("twilio call failed: %s", e)
            log_event("alert", f"Phone call failed: {e}", "error")
            return False

    def whatsapp(self, text: str) -> bool:
        """WhatsApp via Twilio's WhatsApp sender (sandbox or approved business number)."""
        if not (self.s.whatsapp_from and self.s.whatsapp_to):
            return False
        try:
            client = self._twilio()
            if client is None:
                return False
            for to in [x.strip() for x in self.s.whatsapp_to.split(",") if x.strip()]:
                client.messages.create(from_=self.s.whatsapp_from, to=to, body=text)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("whatsapp failed: %s", e)
            log_event("alert", f"WhatsApp send failed: {e}", "error")
            return False

    def _in_quiet_hours(self) -> bool:
        a, b = self.cfg.quiet_hours_from, self.cfg.quiet_hours_to
        if not (a and b):
            return False
        now = datetime.now().time()
        ta, tb = time.fromisoformat(a), time.fromisoformat(b)
        return (ta <= now <= tb) if ta <= tb else (now >= ta or now <= tb)

    # ---- high-level -------------------------------------------------------------

    def slot_found(self, summary: str, url: str, spoken: str = "") -> list[str]:
        text = f"🎉 VFS SLOT AVAILABLE!\n{summary}\n\nBook now: {url}"
        sent = []
        if self.cfg.telegram and self.telegram(text):
            sent.append("telegram")
        if self.cfg.email and self.email("VFS slot available — book now", text):
            sent.append("email")
        if self.cfg.whatsapp and self.whatsapp(text):
            sent.append("whatsapp")
        if self.cfg.call and self.call(f"V F S appointment slot is available. {spoken}. Open your laptop and book it now."):
            sent.append("call")
        log.info("slot alert sent via: %s", ", ".join(sent) or "nothing (no channel configured!)")
        return sent

    def human_needed(self, what: str) -> None:
        text = f"⚠️ VFS bot needs a human: {what}"
        if self.cfg.telegram:
            self.telegram(text)
        if self.cfg.whatsapp:
            self.whatsapp(text)

    def login_needed(self, url: str) -> None:
        self.human_needed(f"session expired / Cloudflare check — log in again in the bot's browser window.\n{url}")

    def otp_needed(self, otp_file: str) -> None:
        self.human_needed(f"VFS sent an OTP. Type it in the bot's browser window, in the web tool, or save it to {otp_file}.")

    def heartbeat(self, text: str) -> None:
        if self.cfg.telegram:
            self.telegram(f"💤 VFS bot: {text}")

    def error(self, text: str) -> None:
        if self.cfg.telegram:
            self.telegram(f"❌ VFS bot error: {text}")

    def test_all(self) -> dict[str, bool]:
        return {
            "telegram": self.telegram("✅ VFS bot test message"),
            "email": self.email("VFS bot test", "If you can read this, email alerts work."),
            "whatsapp": self.whatsapp("✅ VFS bot test message"),
            "call": self.call("This is a test call from your V F S bot."),
        }
