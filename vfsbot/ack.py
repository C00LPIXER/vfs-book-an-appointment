"""Slot-open escalation to Aslam over WhatsApp Web, run as its own process so it never
blocks or disturbs the VFS watcher session.

Flow: send the slot details -> call -> wait 2 min for an "Ok" reply -> if none, call again
(up to N times) -> when "Ok" arrives, record it in the events DB and stop.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import Config
from .events import log_event
from .whatsapp_web import WhatsAppWeb

log = logging.getLogger("vfsbot.ack")
ACK_FILE = Path("state/acknowledged.json")


def run_escalation(message: str, key: str = "") -> int:
    cfg = Config.load()
    w = cfg.whatsapp_web
    if not w.enabled:
        log.info("whatsapp_web disabled — skipping escalation")
        return 0

    log_event("ack", f"Escalation started for: {key or 'slot'}", "alert", {"key": key})
    with WhatsAppWeb(w.profile_dir, cfg.browser.executable) as wa:
        if not wa.open():
            log_event("ack", "WhatsApp Web not linked — cannot reach " + w.chat, "error",
                      screenshot=None)
            return 2
        if not wa.open_chat(w.chat):
            log_event("ack", f"WhatsApp chat '{w.chat}' not found", "error")
            return 2

        wa.send_message(message)
        log_event("ack", f"WhatsApp message sent to {w.chat}", "info")

        acked = False
        for attempt in range(1, w.call_attempts + 1):
            log_event("ack", f"Calling {w.chat} (attempt {attempt}/{w.call_attempts})", "info")
            wa.place_call(w.ring_seconds)
            log_event("ack", f"Waiting {w.call_interval_seconds}s for an '{w.ack_keyword}' reply", "info")
            if wa.wait_for_reply(w.ack_keyword, w.call_interval_seconds):
                acked = True
                break

        if acked:
            rec = {"key": key, "by": w.chat, "at": datetime.now().isoformat(timespec="seconds"),
                   "reply": wa.latest_incoming_text()[:120]}
            ACK_FILE.write_text(json.dumps(rec, indent=2))
            log_event("ack", f"ACKNOWLEDGED by {w.chat} — stopped calling", "alert", rec)
            return 0

        log_event("ack", f"No '{w.ack_keyword}' after {w.call_attempts} calls — giving up", "warn", {"key": key})
        return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("state/vfsbot.log")])
    argv = argv if argv is not None else sys.argv[1:]
    message = argv[0] if argv else "VFS slot open — check the dashboard."
    key = argv[1] if len(argv) > 1 else ""
    return run_escalation(message, key)


if __name__ == "__main__":
    sys.exit(main())
