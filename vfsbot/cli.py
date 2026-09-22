from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config, Secrets
from .events import log_event
from .notify import Notifier
from .schedule import in_burst_window, in_run_window, next_delay
from .watcher import OTP_FILE, Blocked, LoginRequired, OtpRequired, Watcher, short_centre, summarize

log = logging.getLogger("vfsbot")

# Files are namespaced per instance so the public and login watchers can run side by side.
INSTANCE = "public"
STATE_FILE = Path("state/public.json")
PAUSE_FILE = Path("state/public.paused")
STOP_FILE = Path("state/public.stop")
REFRESH_FILE = Path("state/public.sweep_now")


def _set_instance(name: str) -> None:
    global INSTANCE, STATE_FILE, PAUSE_FILE, STOP_FILE, REFRESH_FILE
    INSTANCE = name
    STATE_FILE = Path(f"state/{name}.json")
    PAUSE_FILE = Path(f"state/{name}.paused")
    STOP_FILE = Path(f"state/{name}.stop")
    REFRESH_FILE = Path(f"state/{name}.sweep_now")


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2, default=str))


def _set(st: dict, **kw) -> None:
    st.update(kw)
    st["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save_state(st)


def _should_alert(st: dict, key: list, cooldown_min: int) -> bool:
    last = st.get("last_alert_at")
    if not last:
        return True
    if st.get("last_alert_key") != key:
        return True  # availability changed (new centre or new date) -> tell again
    return datetime.fromisoformat(last) + timedelta(minutes=cooldown_min) < datetime.now()


# canonical category labels — different centres spell these slightly differently
_CAT_CANON = [
    ("long stay d", "Long Stay D visa"),
    ("non-schengen", "Non-Schengen <90d"),
    ("schengen", "Schengen <90d"),
    ("business", "Business"),
    ("seasonal", "Seasonal worker"),
    ("embassy", "Embassy Interview"),
]
_CAT_ORDER = [label for _, label in _CAT_CANON]


def _canon_category(name: str) -> str:
    low = name.lower()
    for needle, label in _CAT_CANON:
        if needle in low:
            return label
    return name.strip()


def _matrix_from_raw(raw: dict, centre_order: list[str]) -> tuple[list[str], list[dict]]:
    """Turn the endpoint response into (ordered categories, rows[{centre, cells{cat:date}}]),
    normalising the category labels so columns are clean and consistent."""
    from .watcher import short_centre
    present: set[str] = set()
    by_centre: dict[str, dict] = {}
    for vac in raw.get("vacList", []):
        centre = short_centre(vac.get("vacName", ""))
        cells = {}
        for g in vac.get("visaGroupList", []):
            cat = _canon_category(g.get("displayName", ""))
            present.add(cat)
            date = g.get("earliestAvailableDate", "") or ""
            # if a centre lists the same canonical category twice, keep the earliest non-empty
            if cat not in cells or (date and not cells[cat]):
                cells[cat] = date
        by_centre[centre] = cells
    cats = [c for c in _CAT_ORDER if c in present] + sorted(present - set(_CAT_ORDER))
    order = [short_centre(c) for c in centre_order] or list(by_centre.keys())
    for c in by_centre:
        if c not in order:
            order.append(c)
    rows = [{"centre": c, "cells": by_centre.get(c, {})} for c in order if c in by_centre]
    return cats, rows


def _sleep_keepalive(w: Watcher, seconds: float, st: dict) -> None:
    """Sleep in short slices; nudge the page, honour stop, and break early on a force-refresh."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        if STOP_FILE.exists():
            return
        if REFRESH_FILE.exists():
            REFRESH_FILE.unlink(missing_ok=True)
            return  # break the wait so the loop sweeps immediately
        time.sleep(min(5, left))
        w.keepalive()
        _set(st, next_check_in=int(left))


def _spawn_escalation(message: str, key: str) -> None:
    import os
    import subprocess
    log = open(STATE_FILE.parent / "ack.out", "a")
    subprocess.Popen([sys.executable, "-m", "vfsbot.ack", message, key], cwd=Path("."),
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def cmd_whatsapp_setup(cfg: Config) -> int:
    from .whatsapp_web import WhatsAppWeb
    print("Opening WhatsApp Web. On your phone: WhatsApp > Linked devices > Link a device, scan the QR.")
    with WhatsAppWeb(cfg.whatsapp_web.profile_dir, cfg.browser.executable) as wa:
        wa.page.goto("https://web.whatsapp.com/")
        input("Press Enter here once WhatsApp Web shows your chats...")
        print("Linked." if wa.is_linked() else "Not linked yet — try again.")
    return 0


def cmd_watch(cfg: Config, secrets: Secrets, once: bool) -> int:
    if cfg.mode == "public":
        from .public_watcher import PublicWatcher
        with PublicWatcher(cfg, secrets) as w:
            return watch_loop(w, cfg, secrets, once)
    with Watcher(cfg, secrets) as w:
        return watch_loop(w, cfg, secrets, once)


def watch_loop(w: Watcher, cfg: Config, secrets: Secrets, once: bool) -> int:
    notifier = Notifier(cfg.notify, secrets)
    st = _load_state()
    url = f"{cfg.base_url}/dashboard"
    consecutive_errors = 0
    login_alerted = False
    def _otp_hook():
        _set(st, status="needs_human", task="waiting for OTP")
        notifier.otp_needed(str(OTP_FILE))

    def _human_hook(what: str):
        _set(st, status="needs_human", task=what)
        notifier.human_needed(what)

    w.on_otp_required = _otp_hook
    w.on_human_needed = _human_hook
    STOP_FILE.unlink(missing_ok=True)
    _set(st, status="starting", task="opening browser", pid=__import__("os").getpid(), started_at=datetime.now().isoformat(timespec="seconds"))
    log_event("control", "Watcher started", "info")

    while True:
        if STOP_FILE.exists():
            STOP_FILE.unlink(missing_ok=True)
            _set(st, status="stopped", task="")
            log_event("control", "Watcher stopped", "info")
            return 0
        cfg = Config.load()  # pick up UI edits without a restart
        w.cfg = cfg
        try:
            allowed, why = in_run_window(cfg)
            if PAUSE_FILE.exists():
                _set(st, status="paused", task="paused by user")
            elif not allowed:
                _set(st, status="sleeping", task=f"outside run window ({why})")
            else:
                _set(st, status="running", task="logging in / checking session")
                w.ensure_logged_in()
                login_alerted = False
                n = len(cfg.centre_list)
                _set(st, task=f"checking {n} centres" + ("  [burst]" if in_burst_window(cfg) else ""))
                log.info("checking %d centres%s", n, "  [burst]" if in_burst_window(cfg) else "")
                results = w.check_all()
                consecutive_errors = 0
                report = summarize(results)
                top_n = cfg.notify.alert_top_n or len(results)
                avail = [r for r in results[:top_n] if r.available]
                log.info("result:\n%s", report)
                lu = getattr(w, "last_updated", "")
                log_event("check", f"Sweep done: {len(avail)} centre(s) with slots" + (f" (VFS updated {lu})" if lu else ""), "alert" if avail else "info",
                          {"results": [{"centre": short_centre(r.centre), "available": r.available,
                                        "earliest": r.earliest, "source": r.source, "error": r.error} for r in results]})
                raw = getattr(w, "last_raw", {}) or {}
                if raw:
                    categories, matrix = _matrix_from_raw(raw, cfg.centre_list)
                else:
                    # login watcher: one column for the watched category, from the per-centre results
                    categories = [cfg.category or "D visa"]
                    matrix = [{"centre": short_centre(r.centre),
                               "cells": {categories[0]: (r.earliest or "")}} for r in results]
                _set(st, last_check_at=datetime.now().isoformat(timespec="seconds"), last_result=report,
                     vfs_updated=getattr(w, "last_updated", ""), mode=cfg.mode,
                     vfs_updated_value=raw.get("lastUpdatedValue"), vfs_updated_type=raw.get("lastUpdatedType"),
                     vfs_fetched_at=datetime.now().isoformat(timespec="seconds"),
                     earliest_categories=categories, earliest_matrix=matrix,
                     last_results=[{"centre": short_centre(r.centre), "available": r.available, "earliest": r.earliest,
                                    "error": r.error} for r in results])

                if avail:
                    key = [[short_centre(r.centre), r.earliest] for r in avail]
                    if _should_alert(st, key, cfg.notify.cooldown_minutes):
                        nearest = avail[0]
                        headline = f"Nearest: {short_centre(nearest.centre)} — {nearest.earliest}"
                        sent = notifier.slot_found(f"{headline}\n\n{report}", url,
                                                   spoken=f"{short_centre(nearest.centre)}, {nearest.earliest}")
                        log_event("alert", f"SLOT ALERT sent via {', '.join(sent) or 'no channel'}: {headline}", "alert",
                                  {"channels": sent, "available": key}, w.screenshot("slot_found"))
                        _set(st, last_alert_at=datetime.now().isoformat(timespec="seconds"), last_alert_key=key,
                             last_alert_channels=sent)
                        if cfg.whatsapp_web.enabled:
                            msg = ("\U0001F389 VFS SLOT OPEN\n"
                                   f"Centre: {short_centre(nearest.centre)}\n"
                                   f"Earliest: {nearest.earliest}\n"
                                   f"Category: {cfg.category}"
                                   + (f" / {cfg.subcategory}" if cfg.subcategory else "") + "\n"
                                   f"Checked: {datetime.now().strftime('%d-%m %H:%M')}\n\n"
                                   f"{report}\n\nBook now: {url}\nReply OK to stop the calls.")
                            _spawn_escalation(msg, short_centre(nearest.centre) + " " + str(nearest.earliest))
                    else:
                        log.info("same availability as last alert, within cooldown — not re-alerting")
                else:
                    st.pop("last_alert_key", None)

                hb = cfg.notify.heartbeat_hours
                if hb and not avail:
                    last_hb = st.get("last_heartbeat_at")
                    if not last_hb or datetime.fromisoformat(last_hb) + timedelta(hours=hb) < datetime.now():
                        notifier.heartbeat(f"still watching, nothing yet.\n{report}")
                        _set(st, last_heartbeat_at=datetime.now().isoformat(timespec="seconds"))

        except OtpRequired as e:
            log.warning("waiting for OTP: %s", e)
            _set(st, status="needs_human", task="waiting for OTP")
        except LoginRequired as e:
            log.warning("still on login page: %s", e)
            _set(st, status="needs_human", task="waiting for login")
            if not login_alerted:
                notifier.login_needed(f"{cfg.base_url}/login")
                login_alerted = True
        except Blocked as e:
            consecutive_errors += 1
            log.error("BLOCKED by VFS/Cloudflare: %s — backing off 15 min.", e)
            _set(st, status="blocked", task="blocked by Cloudflare/WAF — backing off 15 min")
            if consecutive_errors in (1, 5):
                notifier.error(f"blocked by Cloudflare/WAF ({e}); backing off")
            if once:
                return 2
            _sleep_keepalive(w, 15 * 60, st)
            continue
        except KeyboardInterrupt:
            _set(st, status="stopped", task="")
            return 0
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            log.exception("check failed (%d in a row)", consecutive_errors)
            log_event("error", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}", "error",
                      {"consecutive": consecutive_errors}, w.screenshot("error"))
            _set(st, status="error", task=f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
            if consecutive_errors in (3, 10, 30):
                notifier.error(f"{consecutive_errors} consecutive failures: {e}")

        if once:
            return 0
        if PAUSE_FILE.exists() or not in_run_window(cfg)[0]:
            delay = 30.0
        elif st.get("status") == "needs_human":
            delay = 60.0        # retry login quickly once someone has helped
        else:
            delay = next_delay(cfg)
        log.info("next check in %.0fs", delay)
        try:
            _sleep_keepalive(w, delay, st)
        except KeyboardInterrupt:
            _set(st, status="stopped", task="")
            return 0


def cmd_login(cfg: Config, secrets: Secrets) -> int:
    notifier = Notifier(cfg.notify, secrets)
    with Watcher(cfg, secrets) as w:
        w.on_otp_required = lambda: notifier.otp_needed(str(OTP_FILE))
        w.on_human_needed = notifier.human_needed
        w.ensure_logged_in(wait_minutes=15)
        log.info("Logged in. NOTE: the session lives only while this browser is open; use `vfsbot watch`.")
        input("Press Enter to close the browser...")
    return 0


def cmd_discover(cfg: Config, secrets: Secrets) -> int:
    with Watcher(cfg, secrets) as w:
        w.ensure_logged_in()
        opts = w.discover()
    print(json.dumps(opts, indent=2, ensure_ascii=False))
    return 0


def _to_curl(spec: dict) -> str:
    import shlex
    parts = ["curl", "-i", "-X", spec["method"], shlex.quote(spec["url"])]
    for k, v in spec["headers"].items():
        parts += ["-H", shlex.quote(f"{k}: {v}")]
    cookie = "; ".join(f"{k}={v}" for k, v in spec["cookies"].items())
    if cookie:
        parts += ["-H", shlex.quote("cookie: " + cookie)]
    parts += ["--data-raw", shlex.quote(json.dumps(spec["body"]))]
    return " ".join(parts)


def cmd_earliest(cfg: Config, category: str, as_json: bool, only_available: bool, as_curl: bool) -> int:
    from .public_watcher import PublicWatcher
    from .watcher import short_centre
    with PublicWatcher(cfg, None) as w:
        w.load_page()
        if as_curl:
            print(_to_curl(w.request_spec()))
            return 0
        data = w.raw()
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    cat = (category or "").lower()
    print(f"VFS earliest available dates  (mission bgr / ind)   last updated: {data.get('lastUpdatedOn','?')}")
    print(f"{'CENTRE':30s} {'CATEGORY':38s} EARLIEST")
    print("-" * 84)
    any_slot = False
    for vac in data.get("vacList", []):
        centre = short_centre(vac.get("vacName", ""))
        for g in vac.get("visaGroupList", []):
            name = g.get("displayName", "")
            if cat and cat not in name.lower():
                continue
            date = g.get("earliestAvailableDate", "") or ""
            if only_available and not date:
                continue
            if date:
                any_slot = True
            mark = date if date else "-"
            print(f"{centre:30s} {name:38s} {mark}")
    if only_available and not any_slot:
        print("(no availability for the current filter)")
    return 0


def cmd_test_notify(cfg: Config, secrets: Secrets) -> int:
    for k, v in Notifier(cfg.notify, secrets).test_all().items():
        print(f"{k:9s}: {'sent' if v else 'not configured / failed'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vfsbot", description="VFS Global slot watcher (notify-only)")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="open the browser, log in once (for checking the account works)")
    sub.add_parser("discover", help="print the dropdown options on the booking form")
    sub.add_parser("test-notify", help="send a test to every configured channel")
    e = sub.add_parser("earliest", help="fetch & print the public earliest-available-date data (no login)")
    e.add_argument("--category", default="", help="filter by category text, e.g. 'D visa' (default: all)")
    e.add_argument("--available", action="store_true", help="show only centres/categories that have a date")
    e.add_argument("--json", action="store_true", help="print the raw JSON response")
    e.add_argument("--curl", action="store_true", help="print the full curl command (all headers + cookies)")
    sub.add_parser("whatsapp-setup", help="open WhatsApp Web once to link it (QR scan)")
    a_ack = sub.add_parser("ack", help="run the WhatsApp escalation manually (message [key])")
    a_ack.add_argument("message"); a_ack.add_argument("key", nargs="?", default="")
    w = sub.add_parser("watch", help="poll for slots and alert")
    w.add_argument("--once", action="store_true", help="single sweep, then exit")
    w.add_argument("--instance", default="public", help="instance name (namespaces state/pid files)")
    w.add_argument("--mode", default="", choices=["", "public", "login"], help="override config mode for this instance")
    u = sub.add_parser("ui", help="start the web tool")
    u.add_argument("--port", type=int, default=8787)
    u.add_argument("--host", default="127.0.0.1")
    a = p.parse_args(argv)

    _log_stream = sys.stderr if a.cmd == "earliest" else sys.stdout
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else (logging.WARNING if a.cmd == "earliest" else logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(_log_stream), logging.FileHandler("state/vfsbot.log")],
    )
    Path("state").mkdir(exist_ok=True)
    cfg = Config.load(a.config)
    secrets = Secrets()
    if a.cmd == "watch":
        _set_instance(getattr(a, "instance", "public") or "public")
        if getattr(a, "mode", ""):
            cfg.mode = a.mode

    if a.cmd == "ui":
        from .ui.server import run
        return run(a.host, a.port)
    if a.cmd == "whatsapp-setup":
        return cmd_whatsapp_setup(cfg)
    if a.cmd == "ack":
        from .ack import run_escalation
        return run_escalation(a.message, a.key)
    if a.cmd == "earliest":
        return cmd_earliest(cfg, a.category, a.json, a.available, a.curl)
    return {
        "login": lambda: cmd_login(cfg, secrets),
        "discover": lambda: cmd_discover(cfg, secrets),
        "test-notify": lambda: cmd_test_notify(cfg, secrets),
        "watch": lambda: cmd_watch(cfg, secrets, a.once),
    }[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
