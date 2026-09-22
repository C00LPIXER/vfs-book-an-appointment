"""Fetch the VFS one-time password from the watcher account's own inbox over IMAP."""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header

log = logging.getLogger("vfsbot.otp")
OTP_RE = re.compile(r"OTP for your application with VFS Global is\s*(\d{6})", re.I)
SENDER_HINT = "vfshelpline"


def fetch_latest_otp(host: str, user: str, password: str, not_before: datetime, mailbox: str = "INBOX") -> str | None:
    """Return the newest 6-digit VFS OTP received after `not_before`, or None."""
    if not (host and user and password):
        return None
    try:
        m = imaplib.IMAP4_SSL(host)
        m.login(user, password)
        m.select(mailbox, readonly=True)
        since = (not_before - timedelta(days=1)).strftime("%d-%b-%Y")
        typ, data = m.search(None, f'(SINCE {since} FROM "{SENDER_HINT}")')
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        best: tuple[datetime, str] | None = None
        for uid in ids[-15:]:
            typ, msg_data = m.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            try:
                when = email.utils.parsedate_to_datetime(msg.get("Date", ""))
            except Exception:  # noqa: BLE001
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < not_before.astimezone(timezone.utc) - timedelta(seconds=30):
                continue
            body = _body_text(msg)
            mo = OTP_RE.search(body)
            if mo and (best is None or when > best[0]):
                best = (when, mo.group(1))
        m.logout()
        return best[1] if best else None
    except Exception as e:  # noqa: BLE001
        log.warning("IMAP OTP fetch failed: %s", e)
        return None


def _body_text(msg) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html"):
                try:
                    parts.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore"))
                except Exception:  # noqa: BLE001
                    pass
    else:
        try:
            parts.append(msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            pass
    text = "\n".join(parts)
    return re.sub(r"<[^>]+>", " ", text)
