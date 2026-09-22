from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .events import log_event
from .notify import Notifier
from .schedule import in_burst_window, in_run_window, next_delay
from .accounts import AccountPool
from .watcher import OTP_FILE, Blocked, CoolOff, LoginRequired, OtpRequired, ProxyError, Watcher, short_centre, summarize

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


def _sleep_keepalive(w: Watcher | None, seconds: float, st: dict) -> None:
    """Sleep in short slices; nudge the page, honour stop, and break early on a force-refresh."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        if STOP_FILE.exists():
            return
        if REFRESH_FILE.exists():
            REFRESH_FILE.unlink(missing_ok=True)
            return  # break the wait so the loop sweeps immediately
        time.sleep(min(5, left))
        if w is not None:
            w.keepalive()
        _set(st, next_check_in=int(left))


def _direct_ip(url: str) -> str:
    """The machine's own public IP (no proxy) — used to detect a proxy that silently leaks."""
    import re as _re
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            m = _re.search(r"(\d{1,3}(?:\.\d{1,3}){3}|[0-9a-f:]{6,})", r.read().decode("utf-8", "ignore"))
            return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def cmd_whatsapp_setup(cfg: Config) -> int:
    from .whatsapp_web import WhatsAppWeb
    print("Opening WhatsApp Web. On your phone: WhatsApp > Linked devices > Link a device, scan the QR.")
    with WhatsAppWeb(cfg.whatsapp_web.profile_dir, cfg.browser.executable) as wa:
        wa.page.goto("https://web.whatsapp.com/")
        input("Press Enter here once WhatsApp Web shows your chats...")
        print("Linked." if wa.is_linked() else "Not linked yet — try again.")
    return 0


def cmd_watch(cfg: Config, once: bool) -> int:
    if cfg.mode == "public":
        from .public_watcher import PublicWatcher
        with PublicWatcher(cfg) as w:
            return watch_loop(w, cfg, once)
    if cfg.rotation.enabled and AccountPool().accounts:
        return rotate_loop(cfg, once)
    with Watcher(cfg) as w:
        return watch_loop(w, cfg, once)


def rotate_loop(cfg: Config, once: bool) -> int:
    """Login mode with account rotation: every sweep opens a fresh browser for the least recently
    used account (its own proxy/IP + profile), logs in, checks all centres, closes the browser.
    Accounts whose login stalls (Cloudflare cool-off) or gets blocked are benched for a while."""
    notifier = Notifier(cfg.notify)
    st = _load_state()
    consecutive_errors = 0
    STOP_FILE.unlink(missing_ok=True)
    _set(st, status="starting", task="preparing account rotation", pid=__import__("os").getpid(),
         started_at=datetime.now().isoformat(timespec="seconds"), mode="login")
    log_event("control", "Watcher started (account rotation)", "info")
    direct_ip = _direct_ip(cfg.rotation.ip_check_url) if cfg.rotation.verify_ip else ""
    if direct_ip:
        log.info("machine's own IP: %s", direct_ip)
        _set(st, direct_ip=direct_ip)

    while True:
        if STOP_FILE.exists():
            STOP_FILE.unlink(missing_ok=True)
            _set(st, status="stopped", task="")
            log_event("control", "Watcher stopped", "info")
            return 0
        cfg = Config.load()
        pool = AccountPool()
        delay = next_delay(cfg)
        acct = None
        try:
            allowed, why = in_run_window(cfg)
            if PAUSE_FILE.exists():
                _set(st, status="paused", task="paused by user")
                delay = 30.0
            elif not allowed:
                _set(st, status="sleeping", task=f"outside run window ({why})")
                delay = 30.0
            elif not pool.accounts:
                _set(st, status="error", task="no accounts configured — add them on the Settings tab")
                delay = 60.0
            else:
                if cfg.proxy_auto.enabled:
                    # any enabled account without a proxy gets its own server + tunnel first
                    from .proxies import accounts_needing_proxy, provision
                    for email in accounts_needing_proxy():
                        _set(st, status="running", task=f"provisioning proxy server for {email}")
                        try:
                            provision(cfg, email, lambda m: log.info("proxy: %s", m))
                            log_event("control", f"Proxy provisioned automatically for {email}", "info")
                        except Exception as e:  # noqa: BLE001
                            log_event("error", f"Proxy provisioning failed for {email}: {e}", "error")
                    pool = AccountPool()   # reload: proxies were assigned
                acct = pool.next(exclude=st.get("last_account_email", ""))
                if acct is None:
                    free_at = pool.next_free_at()
                    _set(st, status="cooling", task="all accounts cooling off" + (f" until {free_at:%H:%M}" if free_at else ""),
                         accounts=pool.status())
                    delay = max(60.0, min(cfg.rotation.retry_minutes * 60.0,
                                          (free_at - datetime.now()).total_seconds() + 5 if free_at else 60.0))
                else:
                    _run_sweep_as(acct, pool, cfg, st, notifier, direct_ip)
                    consecutive_errors = 0
                    _set(st, accounts=pool.status())
        except ProxyError as e:
            if acct is None:
                raise
            # the proxy is dead or leaking — bench this account briefly, move on quickly
            until = pool.mark_cooloff(acct, 1.0, f"proxy: {e}")
            log.error("proxy problem for %s: %s — benched until %s", acct.name, e, until.strftime("%H:%M"))
            log_event("error", f"Proxy problem for {acct.name}: {e}", "error", {"until": until.isoformat()})
            _set(st, status="error", task=f"proxy problem ({acct.name}) — trying next account", accounts=pool.status())
            delay = 60.0
        except CoolOff as e:
            if acct is None:
                raise
            until = pool.mark_cooloff(acct, cfg.rotation.cooloff_hours, str(e))
            log.error("COOL-OFF for %s: %s — benched until %s", acct.name, e, until.strftime("%H:%M"))
            _set(st, status="cooling", task=f"{acct.name} in Cloudflare cool-off — next account in {cfg.rotation.retry_minutes} min",
                 accounts=pool.status())
            delay = cfg.rotation.retry_minutes * 60.0
        except Blocked as e:
            if acct is None:
                raise
            until = pool.mark_cooloff(acct, cfg.rotation.cooloff_hours, f"blocked: {e}")
            consecutive_errors += 1
            log.error("BLOCKED for %s: %s — benched until %s", acct.name, e, until.strftime("%H:%M"))
            if consecutive_errors in (1, 5):
                notifier.error(f"blocked by Cloudflare/WAF as {acct.name} ({e}); rotating")
            _set(st, status="blocked", task=f"{acct.name} blocked — next account in {cfg.rotation.retry_minutes} min",
                 accounts=pool.status())
            delay = cfg.rotation.retry_minutes * 60.0
        except LoginRequired as e:
            if acct is None:
                raise
            what = "OTP" if isinstance(e, OtpRequired) else "login"
            until = pool.mark_cooloff(acct, cfg.rotation.cooloff_hours / 2, f"{what} timed out")
            log.warning("%s timed out for %s — benched until %s", what, acct.name, until.strftime("%H:%M"))
            log_event("login", f"{what} timed out for {acct.name} — benched until {until:%H:%M}", "warn")
            _set(st, status="error", task=f"{what} timed out ({acct.name}) — next account in {cfg.rotation.retry_minutes} min",
                 accounts=pool.status())
            delay = cfg.rotation.retry_minutes * 60.0
        except KeyboardInterrupt:
            _set(st, status="stopped", task="")
            return 0
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            if "Target page, context or browser has been closed" in str(e):
                log.warning("the bot's browser window was closed by hand — retrying in %d min", cfg.rotation.retry_minutes)
                log_event("error", "Bot browser window was closed by hand — don't close it while a sweep runs; retrying", "warn")
                _set(st, status="error", task="browser window closed by hand — retrying")
                delay = cfg.rotation.retry_minutes * 60.0
                if once:
                    return 1
                _sleep_keepalive(None, delay, st)
                continue
            log.exception("sweep failed (%d in a row)", consecutive_errors)
            log_event("error", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}", "error", {"consecutive": consecutive_errors})
            _set(st, status="error", task=f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
            if consecutive_errors in (3, 10, 30):
                notifier.error(f"{consecutive_errors} consecutive failures: {e}")
            delay = cfg.rotation.retry_minutes * 60.0

        if once:
            return 0
        log.info("next sweep in %.0fs", delay)
        try:
            _sleep_keepalive(None, delay, st)
        except KeyboardInterrupt:
            _set(st, status="stopped", task="")
            return 0


def _run_sweep_as(acct, pool: AccountPool, cfg: Config, st: dict, notifier: Notifier, direct_ip: str) -> None:
    """One full sweep as `acct`: open browser -> verify IP -> login (+OTP) -> check all centres -> close."""
    log.info("sweep as %s (%s)%s", acct.name, acct.email, " via proxy" if acct.proxy else "")
    _set(st, status="running", task=f"opening browser as {acct.name}", account=acct.name,
         account_email=acct.email, ip="")
    with Watcher(cfg, account=acct) as w:
        w.on_otp_required = lambda: (_set(st, status="needs_human", task=f"waiting for OTP ({acct.name})"),
                                     notifier.otp_needed(str(OTP_FILE)))
        w.on_human_needed = lambda what: (_set(st, status="needs_human", task=what), notifier.human_needed(what))
        if cfg.rotation.verify_ip:
            _set(st, task=f"checking IP for {acct.name}")
            last_ip = st.get("last_ip", "")
            ip = w.check_ip(must_differ_from=last_ip if acct.proxy else "")
            if acct.proxy and direct_ip and ip == direct_ip:
                raise ProxyError(f"proxy leaks: public IP {ip} is the machine's own IP")
            if not acct.proxy:
                log.warning("%s has no proxy — logging in from the machine's own IP %s", acct.name, ip)
            _set(st, ip=ip)
        pool.mark_used(acct, w.public_ip)
        _set(st, last_account_email=acct.email, last_ip=w.public_ip, task=f"logging in as {acct.name}")
        try:
            w.ensure_logged_in(wait_minutes=cfg.rotation.login_wait_minutes)
        finally:
            pool.mark_imap(acct, w.imap_error)
        pool.mark_login_ok(acct)
        n = len(cfg.centre_list)
        burst = "  [burst]" if in_burst_window(cfg) else ""
        _set(st, status="running", task=f"checking {n} centres as {acct.name}{burst}")
        log.info("checking %d centres as %s%s", n, acct.name, burst)
        results = w.check_all()
        _handle_results(w, cfg, st, notifier, results)
    log.info("browser closed (%s)", acct.name)


def watch_loop(w: Watcher, cfg: Config, once: bool) -> int:
    notifier = Notifier(cfg.notify)
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
                _handle_results(w, cfg, st, notifier, results)

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


def cmd_login(cfg: Config) -> int:
    notifier = Notifier(cfg.notify)
    with Watcher(cfg) as w:
        w.on_otp_required = lambda: notifier.otp_needed(str(OTP_FILE))
        w.on_human_needed = notifier.human_needed
        w.ensure_logged_in(wait_minutes=15)
        log.info("Logged in. NOTE: the session lives only while this browser is open; use `vfsbot watch`.")
        input("Press Enter to close the browser...")
    return 0


def cmd_discover(cfg: Config) -> int:
    with Watcher(cfg) as w:
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


def cmd_ip(cfg: Config, email: str, as_json: bool) -> int:
    """Open a browser through each account's proxy and print the public IP it gets (proxy check)."""
    pool = AccountPool()
    accts = [a for a in pool.accounts if not email or a.email == email]
    if not accts:
        print(json.dumps({"error": "no such account"}) if as_json else "no such account")
        return 1
    direct = _direct_ip(cfg.rotation.ip_check_url)
    out = []
    for a in accts:
        rec = {"email": a.email, "name": a.name, "proxy": bool(a.proxy), "direct_ip": direct}
        try:
            with Watcher(cfg, account=a) as w:
                rec["ip"] = w.check_ip()
            rec["ok"] = bool(rec["ip"]) and (not a.proxy or rec["ip"] != direct)
            rec["note"] = ("no proxy — machine's own IP" if not a.proxy else
                           ("LEAK: same as the machine's own IP" if rec["ip"] == direct else "proxy working"))
        except Exception as e:  # noqa: BLE001
            rec["ok"] = False; rec["ip"] = ""; rec["note"] = str(e).splitlines()[0][:160]
        out.append(rec)
        if not as_json:
            print(f"{a.name:12s} {rec.get('ip') or '-':18s} {rec['note']}")
    if as_json:
        print(json.dumps(out))
    return 0 if all(r["ok"] for r in out) else 1


def cmd_proxies(cfg: Config, action: str, email: str, ip: str = "", user: str = "root") -> int:
    from . import proxies
    say = lambda m: print(m)  # noqa: E731
    if action == "status":
        for r in proxies.status():
            print(f"{r['label'] or r['email']:14s} proxy={r['proxy'] or '-':28s} auto={r['auto']!s:5s} "
                  f"server={r['server_ip'] or '-':16s} tunnel={'up' if r['tunnel_up'] else 'down'}")
        return 0
    if action == "provision":
        (proxies.provision(cfg, email, say) if email else proxies.provision_missing(cfg, say))
    elif action == "destroy":
        if email:
            proxies.deprovision(cfg, email, say)
        else:
            for e in list(proxies.load_proxies()):
                proxies.deprovision(cfg, e, say)
            proxies.cleanup_orphans(cfg, say)
    elif action == "attach":
        proxies.attach_manual(email, ip, user, 22, say)
    elif action == "key":
        print(proxies.public_key())
    elif action == "tunnels":
        for e in proxies.load_proxies():
            print(e, proxies.ensure_tunnel(e))
    return 0


def cmd_test_notify(cfg: Config) -> int:
    for k, v in Notifier(cfg.notify).test_all().items():
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
    pp = sub.add_parser("proxies", help="automatic per-account proxy servers")
    pp.add_argument("action", choices=["status", "provision", "destroy", "tunnels", "attach", "key"])
    pp.add_argument("--account", default="", help="only this account (email)")
    pp.add_argument("--ip", default="", help="attach: your own server's IP")
    pp.add_argument("--user", default="root", help="attach: SSH user on that server (ubuntu / opc / root)")
    ipp = sub.add_parser("ip", help="show the public IP each account gets through its proxy")
    ipp.add_argument("--account", default="", help="only this account (email)")
    ipp.add_argument("--json", action="store_true")
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
    if a.cmd == "proxies":
        return cmd_proxies(cfg, a.action, a.account, a.ip, a.user)
    if a.cmd == "ip":
        logging.getLogger().setLevel(logging.WARNING)
        return cmd_ip(cfg, a.account, a.json)
    return {
        "login": lambda: cmd_login(cfg),
        "discover": lambda: cmd_discover(cfg),
        "test-notify": lambda: cmd_test_notify(cfg),
        "watch": lambda: cmd_watch(cfg, a.once),
    }[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
