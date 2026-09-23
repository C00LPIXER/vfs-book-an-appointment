from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .events import log_event
from .notify import Notifier
from .schedule import in_burst_window, in_run_window, next_delay
from .accounts import AccountPool
from .watcher import (OTP_FILE, AccountRestricted, Blocked, CoolOff, SiteThrottled, LoginRequired, NoDisplay, OtpRequired, PassportPending,
                      ProfileInUse, ProxyError, SlotResult, Watcher, short_centre, summarize)

log = logging.getLogger("vfsbot")

# Files are namespaced per instance so the public and login watchers can run side by side.
INSTANCE = "public"
MODE_OVERRIDE = ""      # set by `watch --mode`; re-applied after every config reload
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


def _reload_config(path: str | Path = "config.yaml") -> Config:
    """Pick up dashboard edits without a restart, keeping this instance's --mode override."""
    cfg = Config.load(path)
    if MODE_OVERRIDE:
        cfg.mode = MODE_OVERRIDE
    return cfg


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
    _set(st, next_run_at=(datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds"), next_check_in=int(seconds))
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


def _watchdog(cfg: Config, st: dict, notifier: Notifier, ok: bool, why: str = "") -> None:
    """Silence must never look like 'no slots': if nothing has been checked successfully for
    `notify.blind_alert_minutes`, tell the bot-issue contacts (once an hour)."""
    now = datetime.now()
    if ok:
        _set(st, last_success_at=now.isoformat(timespec="seconds"))
        if st.get("blind_since"):
            _set(st, blind_since=None)
            notifier.notice(f"\u2705 VFS bot: checks are working again ({INSTANCE}).")
        return
    last = st.get("last_success_at")
    if not last:
        return
    quiet = (now - datetime.fromisoformat(last)).total_seconds() / 60
    if quiet < cfg.notify.blind_alert_minutes:
        return
    stamp = now.strftime("%Y-%m-%d %H")
    if st.get("blind_notice") == stamp:
        return
    _set(st, blind_since=st.get("blind_since") or last, blind_notice=stamp)
    notifier.notice(f"\u26a0\ufe0f VFS bot is BLIND: no successful {INSTANCE} check for {quiet:.0f} min.\n"
                    f"Reason: {why[:160]}\nNo slot alerts can arrive while this lasts.")


def _handle_results(w, cfg: Config, st: dict, notifier: Notifier, results: list, all_centres: list | None = None) -> None:
    """Log the sweep, update the state file / matrix, alert + escalate on availability, heartbeat.
    When only a slice of the centres was checked (login mode), centres not in this batch keep the
    value from the previous sweep so the dashboard always shows all of them."""
    url = f"{cfg.base_url}/dashboard"
    if all_centres:
        prev = {r["centre"]: r for r in st.get("last_results", [])}
        checked = {short_centre(r.centre): r for r in results if r.source != "skipped"}
        merged = []
        for c in all_centres:
            sc = short_centre(c)
            if sc in checked:
                merged.append(checked[sc])
            elif sc in prev:
                p_ = prev[sc]
                merged.append(SlotResult(bool(p_.get("available")), p_.get("earliest"), "previous", "", {}, c, error=p_.get("error", "")))
            else:
                merged.append(SlotResult(False, None, "skipped", "", {}, c, error="not checked yet"))
        results = merged
    _watchdog(cfg, st, notifier, ok=True)
    report = summarize(results)
    top_n = cfg.notify.alert_top_n or len(results)
    avail = [r for r in results[:top_n] if r.available]
    log.info("result:\n%s", report)
    lu = getattr(w, "last_updated", "")
    n_checked = len([r for r in results if r.source not in ("previous", "skipped")])
    log_event("check", f"Sweep done: {len(avail)} centre(s) with slots" + (f" ({n_checked} checked now)" if all_centres else "")
              + (f" (VFS updated {lu})" if lu else ""), "alert" if avail else "info",
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
                        "error": r.error, "source": r.source,
                        "checked_at": (datetime.now().isoformat(timespec="seconds") if r.source not in ("previous", "skipped")
                                       else next((p.get("checked_at") for p in st.get("last_results", []) if p["centre"] == short_centre(r.centre)), None))}
                       for r in results])

    if avail:
        key = [[short_centre(r.centre), r.earliest] for r in avail]
        if _should_alert(st, key, cfg.notify.cooldown_minutes):
            nearest = avail[0]
            headline = f"Nearest: {short_centre(nearest.centre)} — {nearest.earliest}"
            msg = ("\U0001F389 VFS SLOT OPEN\n"
                   f"Centre: {short_centre(nearest.centre)}\n"
                   f"Earliest: {nearest.earliest}\n"
                   f"Category: {cfg.category}"
                   + (f" / {cfg.subcategory}" if cfg.subcategory else "") + "\n"
                   f"Checked: {datetime.now().strftime('%d-%m %H:%M')}\n\n"
                   f"{report}\n\nBook now: {url}\nReply {cfg.whatsapp_web.ack_keyword.upper()} to stop the calls.")
            sent = notifier.slot_found(msg, short_centre(nearest.centre) + " " + str(nearest.earliest))
            log_event("alert", f"SLOT ALERT via {', '.join(sent) or 'NO CHANNEL — add WhatsApp contacts!'}: {headline}", "alert",
                      {"channels": sent, "available": key}, w.screenshot("slot_found"))
            _set(st, last_alert_at=datetime.now().isoformat(timespec="seconds"), last_alert_key=key,
                 last_alert_channels=sent)
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
        from .public_watcher import PublicApiWatcher
        with PublicApiWatcher(cfg) as w:     # plain HTTPS; a browser only re-mints the CF cookie
            return watch_loop(w, cfg, once)
    if cfg.rotation.enabled and AccountPool().accounts:
        return rotate_loop(cfg, once)
    with Watcher(cfg) as w:
        return watch_loop(w, cfg, once)


def rotate_loop(cfg: Config, once: bool) -> int:
    """One account per round, in turn:

        log in -> check centres -> session ends -> log in again (SAME account) -> ... until every
        centre is checked -> alert if a slot turned up -> that account rests `account_rest_hours`
        -> the next account does exactly the same.

    If VFS/Cloudflare blocks an account it is switched off (with the reason, on the dashboard and
    over WhatsApp) and the next account takes over immediately. A switched-off account is tried
    again by itself after `recheck_blocked_hours`, and switched back on if it can log in."""
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
    # A restart is not a reason to log in again: honour the next run time from before the restart.
    try:
        wait = (datetime.fromisoformat(st["next_run_at"]) - datetime.now()).total_seconds() if st.get("next_run_at") else 0
    except Exception:  # noqa: BLE001
        wait = 0
    if wait > 30 and not REFRESH_FILE.exists() and STATE_FILE.exists():
        log.info("resuming schedule — next login in %.0f min (use 'Refresh now' to sweep immediately)", wait / 60)
        _set(st, status="running", task="resuming schedule (restart) — waiting for the next login")
        _sleep_keepalive(None, wait, st)

    while True:
        if STOP_FILE.exists():
            STOP_FILE.unlink(missing_ok=True)
            _set(st, status="stopped", task="")
            log_event("control", "Watcher stopped", "info")
            return 0
        cfg = _reload_config()
        pool = AccountPool(rest_hours=cfg.rotation.account_rest_hours)
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
                go, why = _login_trigger(cfg, st)
                if not go:
                    _set(st, status="running", task=why, accounts=pool.status())
                    delay = 120.0
                    if once:
                        return 0
                    _sleep_keepalive(None, delay, st)
                    continue
                log.info("logging in — %s", why)
                acct = pool.next(exclude=st.get("last_account_email", ""))
                retrying_blocked = False
                if acct is None:
                    # nobody free: is a switched-off account old enough to test again?
                    acct = pool.due_for_recheck(cfg.rotation.recheck_blocked_hours)
                    if acct is not None:
                        retrying_blocked = True
                        log.info("testing switched-off account %s again", acct.name)
                        _set(st, status="running", task=f"testing switched-off account {acct.name} again")
                if acct is None:
                    free_at = pool.next_free_at()
                    when = free_at.strftime("%I:%M %p").lstrip("0") if free_at else ""
                    blocked = [a.name for a in pool.blocked()]
                    task = ("every account is resting" + (f" — next ready at {when}" if when else "")) if not blocked or pool.available() \
                        else f"all accounts switched off ({', '.join(blocked)}) — retrying them in a while"
                    _set(st, status="cooling", task=task, accounts=pool.status(),
                         next_run_at=free_at.isoformat(timespec="seconds") if free_at else None)
                    delay = max(60.0, min(cfg.rotation.retry_minutes * 60.0,
                                          (free_at - datetime.now()).total_seconds() + 5 if free_at else 60.0))
                else:
                    _set(st, last_login_round_at=datetime.now().isoformat(timespec="seconds"), login_reason=why)
                    _sweep_account(acct, pool, cfg, st, notifier, direct_ip)
                    if retrying_blocked:
                        pool.unblock(acct.email, "logged in again")
                        log_event("login", f"{acct.name} works again — switched back on", "info")
                        notifier.notice(f"\u2705 VFS bot: {acct.name} is working again and is back in the rotation.")
                    consecutive_errors = 0
                    _set(st, accounts=pool.status())
        except ProxyError as e:
            if acct is None:
                raise
            if not acct.proxy:
                # IP rotation (phone/VPN) problem — nothing wrong with the account; retry shortly
                log.error("IP rotation problem: %s — retrying in 2 min", e)
                log_event("error", f"IP rotation problem: {e}", "error")
                notifier.error(f"IP rotation problem: {e}")
                _set(st, status="error", task=f"IP rotation problem — {str(e)[:100]}", accounts=pool.status())
                delay = 120.0
            else:
                # the account's proxy is dead or leaking — nothing to do with VFS; try the next account
                log.error("proxy problem for %s: %s — trying the next account", acct.name, e)
                log_event("error", f"Proxy problem for {acct.name}: {e}", "error")
                _set(st, status="error", task=f"proxy problem ({acct.name}) — trying next account", accounts=pool.status())
                delay = 60.0
        except SiteThrottled as e:
            # the IP is throttled, not the account: rest everything, do not switch anyone off
            if cfg.ip_rotate.enabled:
                # a phone/VPN can simply hand us another IP — far better than waiting out the limit
                try:
                    new_ip = rotate_ip(cfg, st.get("last_ip", ""))
                    log.warning("%s — rotated to %s, carrying on", e, new_ip)
                    log_event("control", f"VFS rate-limited the old IP — rotated to {new_ip}, continuing", "info")
                    _set(st, status="running", task=f"VFS rate-limited the IP — switched to {new_ip}", ip=new_ip, last_ip=new_ip)
                    delay = 60.0
                    if once:
                        return 0
                    _sleep_keepalive(None, delay, st)
                    continue
                except ProxyError as re_:
                    log.error("could not rotate the IP after a throttle: %s", re_)
            mins = cfg.rotation.throttle_backoff_minutes
            log.error("%s — pausing all accounts for %d min", e, mins)
            log_event("blocked", f"{e} — pausing logins for {mins} min", "error", screenshot=None)
            if st.get("last_throttle_notice") != datetime.now().strftime("%Y-%m-%d %H"):
                notifier.notice(f"\u23f8\ufe0f VFS bot: {e}.\nAll accounts pause for {mins} min. "
                                f"If this keeps happening the machine needs a different IP per account.")
                _set(st, last_throttle_notice=datetime.now().strftime("%Y-%m-%d %H"))
            _set(st, status="cooling", task=f"VFS is rate-limiting this IP — all accounts pause for {mins} min",
                 accounts=pool.status())
            delay = mins * 60.0
        except (CoolOff, Blocked) as e:
            if acct is None:
                raise
            if isinstance(e, AccountRestricted):
                reason = f"VFS restricted this account — {e}"
            elif isinstance(e, CoolOff):
                reason = "Cloudflare will not let this account sign in (the check stays blank)"
            else:
                reason = f"VFS/Cloudflare blocked this account: {e}"
            pool.block(acct, reason)
            log.error("%s switched OFF — %s", acct.name, reason)
            log_event("blocked", f"{acct.name} switched off — {reason}", "error", {"account": acct.email})
            notifier.notice(f"\u26d4 VFS bot: account {acct.name} switched off.\nReason: {reason}\n"
                            f"The next account takes over now; {acct.name} is tested again in "
                            f"{cfg.rotation.recheck_blocked_hours:.0f} h.")
            left = [a.name for a in pool.available()]
            _set(st, status="running", task=f"{acct.name} switched off ({reason[:60]}) — next account now" if left
                 else f"{acct.name} switched off — no account left", accounts=pool.status())
            delay = 30.0 if left else cfg.rotation.retry_minutes * 60.0
        except NoDisplay as e:
            log.error("%s", e)
            log_event("error", f"Cannot open a browser: {e}", "error")
            notifier.error(f"Cannot open a browser: {e}")
            _set(st, status="needs_human", task=f"cannot open a browser — {str(e)[:100]}", accounts=pool.status())
            delay = 300.0
        except ProfileInUse as e:
            log.error("%s", e)
            log_event("error", str(e), "warn")
            notifier.error(str(e))
            _set(st, status="needs_human", task=str(e)[:120], accounts=pool.status())
            delay = cfg.rotation.retry_minutes * 60.0
        except PassportPending:
            # logged in fine; VFS's one-time passport step needs a human (no cool-off — nothing went wrong)
            _set(st, status="needs_human", task=f"{acct.name}: passport step needs Continue in the browser (or enable auto-continue in Settings)",
                 accounts=pool.status())
            delay = cfg.rotation.retry_minutes * 60.0
        except LoginRequired as e:
            if acct is None:
                raise
            # not a Cloudflare problem — just log in again next round (no cool-off)
            what = "OTP never arrived" if isinstance(e, OtpRequired) else "login did not complete"
            delay = cfg.rotation.retry_minutes * 60.0
            log.warning("%s (%s) — next sweep in %.0f min", what, acct.name, delay / 60)
            log_event("login", f"{what} ({acct.name}) — next sweep in {delay / 60:.0f} min", "warn")
            _set(st, status="running", task=f"{what} ({acct.name}) — next sweep in {delay / 60:.0f} min", accounts=pool.status())
        except KeyboardInterrupt:
            _set(st, status="stopped", task="")
            return 0
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            if "Target page, context or browser has been closed" in str(e) and "launch_persistent_context" not in str(e):
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
        log.info("next login in %.0f min", delay / 60)
        try:
            _sleep_keepalive(None, delay, st)
        except KeyboardInterrupt:
            _set(st, status="stopped", task="")
            return 0


def rotate_ip(cfg: Config, last_ip: str) -> str:
    """Run the IP-rotation command (phone tethering / VPN CLI) and return the new public IP."""
    import subprocess
    r = cfg.ip_rotate
    log.info("rotating IP: %s", r.command)
    try:
        res = subprocess.run(r.command, shell=True, capture_output=True, text=True, timeout=r.timeout_seconds)
    except subprocess.TimeoutExpired:
        raise ProxyError(f"IP rotation command timed out after {r.timeout_seconds}s")
    if res.returncode != 0:
        raise ProxyError(f"IP rotation command failed ({res.returncode}): {(res.stderr or res.stdout).strip()[-160:]}")
    ip = ""
    for _ in range(12):          # the link needs a moment to come back
        ip = _direct_ip(cfg.rotation.ip_check_url)
        if ip:
            break
        time.sleep(5)
    if not ip:
        raise ProxyError("no internet after the IP rotation command (is the phone tethered?)")
    if r.require_change and last_ip and ip == last_ip:
        raise ProxyError(f"public IP did not change ({ip}) after the rotation command")
    log.info("new public IP: %s (was %s)", ip, last_ip or "?")
    log_event("control", f"IP rotated: {ip}" + (f" (was {last_ip})" if last_ip else ""), "info")
    return ip


def _public_hit(cfg: Config) -> list[str]:
    """Centres where the public (no-login) data shows a date for the watched category."""
    try:
        pub = json.loads(Path("state/public.json").read_text())
    except Exception:  # noqa: BLE001
        return []
    want = (cfg.category or "D visa").lower().replace("d visa", "d visa")
    enabled = {short_centre(c).lower() for c in cfg.centre_list}
    hits = []
    for row in pub.get("earliest_matrix", []):
        if row.get("centre", "").lower() not in enabled:
            continue
        for cat, date in (row.get("cells") or {}).items():
            if date and ("d visa" in cat.lower() if "d visa" in want else want in cat.lower()):
                hits.append(f"{row['centre']} ({cat}: {date})")
    return hits


def _login_trigger(cfg: Config, st: dict) -> tuple[bool, str]:
    """Should we log in at all right now? (see RotationConfig.login_on_slot_only)"""
    if not cfg.rotation.login_on_slot_only:
        return True, "scheduled round"
    if REFRESH_FILE.exists():
        return True, "asked from the dashboard"
    if in_burst_window(cfg):
        return True, "slot-release window"
    hits = _public_hit(cfg)
    if hits:
        return True, "public data shows " + ", ".join(hits[:3])
    last = st.get("last_login_round_at")
    if not last:
        return True, "first round — checking the accounts work"
    try:
        due = datetime.fromisoformat(last) + timedelta(hours=cfg.rotation.keepalive_login_hours)
    except Exception:  # noqa: BLE001
        return True, "keep-alive round"
    if datetime.now() >= due:
        return True, f"keep-alive round ({cfg.rotation.keepalive_login_hours:.0f} h since the last one)"
    return False, f"no D-visa date in the public data — next keep-alive login at {due:%I:%M %p}".replace(" 0", " ")


def _live_rows(cfg: Config, live: dict) -> list[dict]:
    """Per-centre live status in priority order, for the dashboard."""
    rows = []
    for c in cfg.centre_list:
        sc = short_centre(c)
        rows.append({"centre": sc, **live.get(sc, {"state": "pending", "earliest": None, "checked_at": None, "by": None, "error": ""})})
    return rows


def _sweep_account(acct, pool: AccountPool, cfg: Config, st: dict, notifier: Notifier, direct_ip: str) -> None:
    """One rotation = one account. VFS answers only ~7 slot checks per login session, so the same
    account signs in again (up to max_logins_per_sweep) until every enabled centre is covered.
    The next rotation then moves to the next account."""
    all_centres = list(cfg.centre_list)
    start = int(st.get("centre_cursor", 0)) % max(1, len(all_centres))
    remaining = (all_centres * 2)[start:start + len(all_centres)]      # priority order, oldest first
    logins = max(1, cfg.max_logins_per_sweep) if cfg.sweep_all_centres else 1
    empty_in_a_row = 0
    for attempt in range(logins):
        if not remaining:
            break
        if attempt:
            gap = random.uniform(cfg.relogin_gap_seconds * 0.7, cfg.relogin_gap_seconds * 1.3)
            log.info("%d centre(s) left — signing in again as %s in %.0fs", len(remaining), acct.name, gap)
            _set(st, status="running", task=f"{len(remaining)} centre(s) left — next login as {acct.name} in {gap / 60:.0f} min")
            _sleep_keepalive(None, gap, st)
            if STOP_FILE.exists():
                return
        try:
            checked = _run_sweep_as(acct, pool, cfg, st, notifier, direct_ip, remaining)
        except (LoginRequired, Blocked, ProxyError, ProfileInUse, NoDisplay):
            raise
        except Exception as e:  # noqa: BLE001  (browser/navigation hiccup)
            log.warning("login %d of this rotation failed (%s) — %d centre(s) still to do",
                        attempt + 1, str(e).splitlines()[0][:100], len(remaining))
            log_event("error", f"{type(e).__name__} during {acct.name}'s sweep: {str(e).splitlines()[0][:150]}", "warn")
            if attempt + 1 >= logins:
                raise
            continue
        remaining = [c for c in remaining if short_centre(c) not in {short_centre(x) for x in checked}]
        _set(st, centre_cursor=(all_centres.index(remaining[0]) if remaining else 0))
        # VFS ends a session after a handful of checks, so a rotation keeps signing in with the SAME
        # account until every centre is covered. Only give up when two logins in a row return nothing.
        empty_in_a_row = empty_in_a_row + 1 if not checked else 0
        if empty_in_a_row >= 2:
            log.warning("two logins in a row checked nothing — leaving %d centre(s) for %s's next rotation", len(remaining), acct.name)
            break
    if not remaining:
        log.info("all %d centres checked this rotation by %s", len(all_centres), acct.name)
        log_event("check", f"Rotation complete — all {len(all_centres)} centres checked by {acct.name}", "info")
    else:
        log.warning("rotation ended with %d centre(s) unchecked (%s) — they go first next rotation",
                    len(remaining), ", ".join(short_centre(c) for c in remaining))


def _run_sweep_as(acct, pool: AccountPool, cfg: Config, st: dict, notifier: Notifier, direct_ip: str,
                  centres: list[str] | None = None) -> list[str]:
    """One login as `acct`: (rotate IP) -> browser -> verify IP -> login (+OTP) -> check centres -> close.
    Returns the centres it managed to check."""
    log.info("sweep as %s (%s)%s", acct.name, acct.email, " via proxy" if acct.proxy else "")
    _set(st, status="running", task=f"opening browser as {acct.name}", account=acct.name,
         account_email=acct.email, ip="", next_run_at=None, next_check_in=None)
    if cfg.ip_rotate.enabled and not acct.proxy:
        _set(st, task=f"getting a fresh IP for {acct.name}")
        direct_ip = rotate_ip(cfg, st.get("last_ip", ""))
    with Watcher(cfg, account=acct) as w:
        w.on_otp_required = lambda: (_set(st, status="needs_human", task=f"waiting for OTP ({acct.name})"),
                                     notifier.otp_needed(str(OTP_FILE)))
        w.on_human_needed = lambda what: (_set(st, status="needs_human", task=what), notifier.human_needed(what))
        w.on_status = lambda what: _set(st, task=f"{acct.name}: {what}")
        if cfg.rotation.verify_ip:
            _set(st, task=f"checking IP for {acct.name}")
            last_ip = st.get("last_ip", "")
            ip = w.check_ip(must_differ_from=last_ip if acct.proxy else "")
            if acct.proxy and direct_ip and ip == direct_ip:
                raise ProxyError(f"proxy leaks: public IP {ip} is the machine's own IP")
            if not acct.proxy and cfg.ip_rotate.enabled and cfg.ip_rotate.require_change and last_ip and ip == last_ip:
                raise ProxyError(f"public IP {ip} is the same as the last login's — rotation did not take effect")
            if not acct.proxy and not cfg.ip_rotate.enabled:
                log.warning("%s has no proxy — logging in from the machine's own IP %s", acct.name, ip)
            _set(st, ip=ip)
        pool.mark_used(acct, w.public_ip)
        _set(st, last_account_email=acct.email, last_ip=w.public_ip, task=f"logging in as {acct.name}")
        try:
            w.ensure_logged_in(wait_minutes=cfg.rotation.login_wait_minutes)
        finally:
            pool.mark_imap(acct, w.imap_error)
        pool.mark_login_ok(acct)
        all_centres = list(cfg.centre_list)
        todo = list(centres) if centres is not None else all_centres
        # 0 = no cap: keep checking until the session ends by itself, so a session that survives the
        # whole list finishes the rotation in one login
        per = len(todo) if cfg.login_centres_per_session <= 0 else max(1, min(cfg.login_centres_per_session, len(todo)))
        batch = todo[:per]
        burst = "  [burst]" if in_burst_window(cfg) else ""
        _set(st, status="running", task=f"checking {', '.join(short_centre(c) for c in batch)} as {acct.name}{burst}")
        log.info("checking %d of %d centres as %s (%s)%s", len(batch), len(all_centres), acct.name, ", ".join(short_centre(c) for c in batch), burst)

        live = {r["centre"]: r for r in st.get("centre_live", []) if r.get("state") not in ("checking",)}
        for r in st.get("last_results", []):      # older sweeps (before the live table existed)
            if r.get("checked_at") and r["centre"] not in live:
                live[r["centre"]] = {"state": "error" if r.get("error") else ("slot" if r.get("available") else "none"),
                                     "earliest": r.get("earliest"), "checked_at": r.get("checked_at"), "by": None, "error": r.get("error", "")}
        for c in batch:
            live[short_centre(c)] = {**live.get(short_centre(c), {}), "state": "queued", "error": ""}

        def progress(centre, state, r, i, n):
            sc = short_centre(centre)
            now = datetime.now().isoformat(timespec="seconds")
            if state == "checking":
                live[sc] = {**live.get(sc, {}), "state": "checking", "by": acct.name, "error": ""}
                _set(st, task=f"checking {sc} ({i + 1}/{n}) as {acct.name}{burst}", centre_live=_live_rows(cfg, live))
            elif state == "done":
                live[sc] = {"state": ("error" if r.error else ("slot" if r.available else "none")), "earliest": r.earliest,
                            "checked_at": now, "by": acct.name, "error": r.error}
                _set(st, centre_live=_live_rows(cfg, live))
                # one log line + a screenshot of the slot-check page per centre
                msg = (f"{sc}: check failed ({r.error})" if r.error else f"{sc}: SLOT {r.earliest}" if r.available else f"{sc}: no slot")
                log_event("check", f"{msg}  [{acct.name}]", "alert" if r.available else ("warn" if r.error else "info"),
                          {"centre": sc, "available": r.available, "earliest": r.earliest},
                          w.screenshot(f"{sc}_{'slot' if r.available else 'error' if r.error else 'noslot'}"))
            else:   # skipped (quota) — keep the previous result, note why
                live[sc] = {**live.get(sc, {}), "state": live.get(sc, {}).get("state") if live.get(sc, {}).get("checked_at") else "pending",
                            "error": "quota spent — next login"}
                _set(st, centre_live=_live_rows(cfg, live))

        checked_centres: list[str] = []
        try:
            results = w.check_all(batch, on_progress=progress)
        except LoginRequired as e:
            e.after_login = True
            done = [r for r in w.partial_results if r.source != "skipped"]
            for c in batch[len(w.partial_results):]:
                live[short_centre(c)] = {**live.get(short_centre(c), {}), "state": live.get(short_centre(c), {}).get("state") if live.get(short_centre(c), {}).get("checked_at") else "pending", "error": "session ended — next login"}
            for sc, v in live.items():
                if v.get("state") == "checking":
                    v["state"] = "pending"; v["error"] = "session ended — next login"
            _set(st, centre_live=_live_rows(cfg, live))
            log_event("login", f"VFS session ended during the sweep ({acct.name}) after {len(done)} centre(s) at {w.url}", "warn",
                      screenshot=w.screenshot("session_ended"))
            if done:   # keep what was checked; the next login continues from the next centre
                _handle_results(w, cfg, st, notifier, done, all_centres)
            checked_centres = [r.centre for r in done]
            results = None
        if results is not None:
            attempted = [r for r in results if r.source != "skipped"]
            _handle_results(w, cfg, st, notifier, results, all_centres)
            checked_centres = [r.centre for r in attempted]
    log.info("browser closed (%s)", acct.name)
    return checked_centres


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
        cfg = _reload_config()  # pick up UI edits without a restart
        w.cfg = cfg
        try:
            allowed, why = in_run_window(cfg)
            if PAUSE_FILE.exists():
                _set(st, status="paused", task="paused by user")
            elif not allowed:
                _set(st, status="sleeping", task=f"outside run window ({why})")
            else:
                _set(st, status="running", task="logging in / checking session", next_run_at=None, next_check_in=None)
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
            rate_limited = "429" in str(e) or "rate-limit" in str(e)
            mins = 30 if rate_limited else 15
            _watchdog(cfg, st, notifier, ok=False, why=str(e)[:160])
            log.error("%s — backing off %d min.", e, mins)
            _set(st, status="blocked",
                 task=("VFS is rate-limiting this IP — retrying in %d min" % mins) if rate_limited
                 else "blocked by Cloudflare/WAF — backing off %d min" % mins)
            if consecutive_errors in (1, 5):
                notifier.error(f"blocked by Cloudflare/WAF ({e}); backing off")
            if once:
                return 2
            _sleep_keepalive(w, mins * 60, st)
            continue
        except KeyboardInterrupt:
            _set(st, status="stopped", task="")
            return 0
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            _watchdog(cfg, st, notifier, ok=False, why=f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
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
    from .public_watcher import PublicApiWatcher, PublicWatcher
    from .watcher import short_centre
    if as_curl:
        with PublicWatcher(cfg) as w:      # needs a browser: it prints the full browser request
            w.load_page()
            print(_to_curl(w.request_spec()))
            return 0
    data = PublicApiWatcher(cfg).raw()     # no browser
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


def cmd_ip(cfg: Config, email: str, as_json: bool, throwaway: bool = False) -> int:
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
            # the account's real profile (so a VPN extension counts) unless a sweep may be using it
            with Watcher(cfg, account=a, profile_dir="browser-profile/_iptest" if throwaway else "") as w:
                rec["ip"] = w.check_ip()
                rec["extensions"] = len(w.ctx.background_pages) + len(w.ctx.service_workers)
            rec["ok"] = bool(rec["ip"]) and (not a.proxy or rec["ip"] != direct)
            if a.proxy:
                rec["note"] = "LEAK: same as the machine's own IP" if rec["ip"] == direct else "proxy working"
            elif rec["ip"] and rec["ip"] != direct:
                rec["ok"] = True; rec["note"] = "VPN extension / rotation active — differs from the machine's own IP"
            else:
                rec["note"] = "no proxy — machine's own IP" + (" (VPN extension installed but not connected?)" if rec.get("extensions") else "")
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


def cmd_browser(cfg: Config, email: str) -> int:
    """Open an account's own Brave profile with no automation, e.g. to install a VPN extension from
    the Chrome Web Store and connect it. Whatever you set up stays in that profile for the bot."""
    import subprocess
    from .accounts import AccountPool
    from .watcher import NO_RESTORE_ARGS, find_browser, prepare_profile
    accts = [a for a in AccountPool().accounts if not email or a.email == email]
    if not accts:
        print("no such account"); return 1
    a = accts[0]
    prof = Path(a.profile_dir).resolve(); prof.mkdir(parents=True, exist_ok=True); prepare_profile(prof)
    exe = find_browser(cfg.browser.executable)
    print(f"Opening {a.name}'s profile ({prof.name}). Install/connect the VPN extension, then close the window.")
    return subprocess.call([exe, f"--user-data-dir={prof}", *NO_RESTORE_ARGS, "chrome://extensions/", "https://chromewebstore.google.com/search/vpn"])


def cmd_rotate_ip(cfg: Config) -> int:
    """Test the IP rotation command once: shows the IP before and after."""
    before = _direct_ip(cfg.rotation.ip_check_url)
    print(f"IP before: {before or '?'}")
    try:
        after = rotate_ip(cfg, before)
    except ProxyError as e:
        print(f"FAILED: {e}")
        return 1
    print(f"IP after:  {after}  ({'changed' if after != before else 'SAME'})")
    return 0 if after != before else 1


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
    sub.add_parser("rotate-ip", help="run the IP rotation command once (phone tethering / VPN) and show before/after")
    bp = sub.add_parser("browser", help="open an account's browser profile by hand (install / connect a VPN extension)")
    bp.add_argument("--account", default="", help="account email (default: first)")
    ipp = sub.add_parser("ip", help="show the public IP each account gets through its proxy")
    ipp.add_argument("--account", default="", help="only this account (email)")
    ipp.add_argument("--json", action="store_true")
    ipp.add_argument("--throwaway", action="store_true", help="use a scratch profile (when the account's own profile is in use)")
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
        global MODE_OVERRIDE
        _set_instance(getattr(a, "instance", "public") or "public")
        if getattr(a, "mode", ""):
            MODE_OVERRIDE = a.mode
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
    if a.cmd == "browser":
        return cmd_browser(cfg, a.account)
    if a.cmd == "rotate-ip":
        logging.getLogger().setLevel(logging.WARNING)
        return cmd_rotate_ip(cfg)
    if a.cmd == "proxies":
        return cmd_proxies(cfg, a.action, a.account, a.ip, a.user)
    if a.cmd == "ip":
        logging.getLogger().setLevel(logging.WARNING)
        return cmd_ip(cfg, a.account, a.json, a.throwaway)
    return {
        "login": lambda: cmd_login(cfg),
        "discover": lambda: cmd_discover(cfg),
        "test-notify": lambda: cmd_test_notify(cfg),
        "watch": lambda: cmd_watch(cfg, a.once),
    }[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
