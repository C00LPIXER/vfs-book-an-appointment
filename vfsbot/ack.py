"""WhatsApp escalation, run as its own process so it never blocks or disturbs the VFS watcher.

Slot alert (`python -m vfsbot.ack slot "<message>" "<key>"`):
    1. send the slot message to every enabled contact
    2. for the contacts in "message_call" mode: call each one (missed call), then wait
       `call_interval_seconds` while polling every one of those chats for an ack reply;
       a contact who acks is not called again; with `stop_all_on_first_ack` one ack stops everything
    3. repeat up to the (global or per-contact) call attempts; record who acked in the events DB

Notice (`python -m vfsbot.ack notice "<text>"`): message-only to the contacts flagged `bot_issues`
(OTP needed, human needed, errors, heartbeat).
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import Config, WhatsAppContact
from .events import log_event
from .whatsapp_web import WhatsAppWeb

log = logging.getLogger("vfsbot.ack")
ACK_FILE = Path("state/acknowledged.json")


def _in_quiet_hours(cfg: Config) -> bool:
    from datetime import time as _time
    a, b = cfg.notify.quiet_hours_from, cfg.notify.quiet_hours_to
    if not (a and b):
        return False
    now = datetime.now().time()
    ta, tb = _time.fromisoformat(a), _time.fromisoformat(b)
    return (ta <= now <= tb) if ta <= tb else (now >= ta or now <= tb)


def _send_to(wa: WhatsAppWeb, contacts: list[WhatsAppContact], message: str) -> dict[str, int]:
    """Send `message` to each contact; returns {chat: incoming-message count after sending}
    (the baseline for detecting a *new* reply later)."""
    baselines: dict[str, int] = {}
    for c in contacts:
        if not wa.open_chat(c.chat):
            log_event("ack", f"WhatsApp chat '{c.chat}' not found", "error")
            continue
        if wa.send_message(message):
            baselines[c.chat] = wa.incoming_count()
            log_event("ack", f"WhatsApp message sent to {c.chat}", "info")
        else:
            log_event("ack", f"WhatsApp message to {c.chat} FAILED", "error")
    return baselines


def run_notice(text: str) -> int:
    cfg = Config.load()
    w = cfg.whatsapp_web
    contacts = [c for c in w.active_contacts() if c.bot_issues]
    if not (w.enabled and contacts):
        log.info("no bot-issue contacts — notice not sent: %s", text[:80])
        return 0
    with WhatsAppWeb(w.profile_dir, cfg.browser.executable) as wa:
        if not wa.open():
            log_event("ack", "WhatsApp Web not linked — notice not sent", "error")
            return 2
        sent = _send_to(wa, contacts, text)
    return 0 if sent else 1


def run_escalation(message: str, key: str = "") -> int:
    cfg = Config.load()
    w = cfg.whatsapp_web
    contacts = w.active_contacts()
    if not (w.enabled and contacts):
        log.info("whatsapp_web disabled or no contacts — skipping escalation")
        return 0

    log_event("ack", f"Escalation started for: {key or 'slot'} → {', '.join(c.chat for c in contacts)}", "alert", {"key": key})
    with WhatsAppWeb(w.profile_dir, cfg.browser.executable) as wa:
        if not wa.open():
            log_event("ack", "WhatsApp Web not linked — cannot alert anyone", "error")
            return 2

        baselines = _send_to(wa, contacts, message)

        to_call = [c for c in contacts if c.mode == "message_call" and c.chat in baselines]
        if not to_call:
            return 0
        if _in_quiet_hours(cfg):
            log_event("ack", "Quiet hours — messages sent, no calls placed", "warn", {"key": key})
            return 0

        pending = {c.chat: c for c in to_call}
        attempts = {c.chat: 0 for c in to_call}
        acked: list[dict] = []
        while pending:
            # one round: call everyone still pending who has attempts left
            called_this_round = False
            for chat, c in list(pending.items()):
                limit = c.call_attempts or w.call_attempts
                if attempts[chat] >= limit:
                    log_event("ack", f"No ack from {chat} after {limit} calls — giving up on them", "warn", {"key": key})
                    pending.pop(chat)
                    continue
                attempts[chat] += 1
                log_event("ack", f"Calling {chat} (attempt {attempts[chat]}/{limit})", "info")
                if wa.open_chat(chat):
                    wa.place_call(w.ring_seconds)
                    called_this_round = True
            if not pending or not called_this_round:
                break
            # wait for acks, polling every pending chat
            log_event("ack", f"Waiting {w.call_interval_seconds}s for a reply from: {', '.join(pending)}", "info")
            end = time.time() + w.call_interval_seconds
            while pending and time.time() < end:
                for chat, c in list(pending.items()):
                    kw = c.ack_keyword or w.ack_keyword
                    if not wa.open_chat(chat):
                        continue
                    if wa.has_new_reply(baselines.get(chat, 0), kw):
                        rec = {"key": key, "by": chat, "at": datetime.now().isoformat(timespec="seconds"),
                               "reply": wa.latest_incoming_text()[:120]}
                        acked.append(rec)
                        log_event("ack", f"ACKNOWLEDGED by {chat}", "alert", rec)
                        pending.pop(chat)
                if pending and w.stop_all_on_first_ack and acked:
                    log_event("ack", "Stopping the remaining calls (first ack is enough)", "info")
                    pending.clear()
                if pending:
                    time.sleep(4)

        if acked:
            ACK_FILE.write_text(json.dumps(acked, indent=2))
            log_event("ack", "Escalation finished — acked by " + ", ".join(a["by"] for a in acked), "alert", {"key": key})
            return 0
        log_event("ack", "Escalation finished — nobody acknowledged", "warn", {"key": key})
        return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("state/vfsbot.log")])
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "notice":
        return run_notice(argv[1] if len(argv) > 1 else "VFS bot notice")
    if argv and argv[0] == "slot":
        argv = argv[1:]
    message = argv[0] if argv else "VFS slot open — check the dashboard."
    key = argv[1] if len(argv) > 1 else ""
    return run_escalation(message, key)


if __name__ == "__main__":
    sys.exit(main())
