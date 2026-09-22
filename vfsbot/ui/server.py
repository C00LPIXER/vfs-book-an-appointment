"""Local web tool: config, control, status, logs and screenshots for the watcher."""
from __future__ import annotations

import json
import re
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import events
from ..accounts import AccountPool, load_raw_accounts, save_raw_accounts
from ..config import ALL_CENTRES, KNOWN_CATEGORIES, Config
from ..watcher import OTP_FILE, find_browser

ROOT = Path(".").resolve()
STATE = ROOT / "state"
STATE_FILE = STATE / "state.json"
PAUSE_FILE = STATE / "paused"
STOP_FILE = STATE / "stop"
SHOTS = STATE / "screenshots"
PID_FILE = STATE / "watcher.pid"
LOG_FILE = STATE / "vfsbot.log"
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="VFS Slot Watcher")
SHOTS.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=SHOTS), name="screenshots")
app.mount("/static", StaticFiles(directory=STATIC), name="static")



# ---- process control (two instances: public + login) ----------------------------

INSTANCES = {
    "public": {"mode": "public", "profile": "public-profile", "label": "Public (no login)"},
    "login":  {"mode": "login",  "profile": "browser-profile", "label": "Logged-in (rotating accounts)"},
}


def _paths(inst: str):
    return {
        "state": STATE / f"{inst}.json",
        "pause": STATE / f"{inst}.paused",
        "stop":  STATE / f"{inst}.stop",
        "pid":   STATE / f"{inst}.pid",
    }


def _find_watcher_pids(inst: str) -> list[int]:
    """Live 'vfsbot.cli watch --instance <inst>' processes, matched by command line."""
    pids: list[int] = []
    me = os.getpid()
    needle = f"--instance {inst}"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except (OSError, PermissionError):
            continue
        if "vfsbot.cli" in cmd and " watch" in cmd and needle in cmd:
            pids.append(int(entry.name))
    return pids


def _browser_pids(inst: str) -> list[int]:
    prof = INSTANCES[inst]["profile"]
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except (OSError, PermissionError):
            continue
        if prof in cmd and "--type=" not in cmd:
            pids.append(int(entry.name))
    return pids


def _running(inst: str) -> bool:
    return bool(_find_watcher_pids(inst))


def _start_watcher(inst: str) -> int:
    if (pids := _find_watcher_pids(inst)):
        return pids[0]
    _paths(inst)["stop"].unlink(missing_ok=True)
    _paths(inst)["pause"].unlink(missing_ok=True)
    logf = open(STATE / f"{inst}.out", "a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "vfsbot.cli", "watch", "--instance", inst, "--mode", INSTANCES[inst]["mode"]],
        cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    _paths(inst)["pid"].write_text(str(proc.pid))
    events.log_event("control", f"{INSTANCES[inst]['label']} watcher started", "info")
    return proc.pid


def _stop_watcher(inst: str) -> None:
    import time as _t
    _paths(inst)["stop"].touch()
    for _ in range(6):
        if not _find_watcher_pids(inst):
            break
        _t.sleep(0.5)
    for _ in range(15):
        pids = _find_watcher_pids(inst)
        if not pids:
            break
        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        _t.sleep(0.2)
    for pid in _find_watcher_pids(inst):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for pid in _browser_pids(inst):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    _paths(inst)["pid"].unlink(missing_ok=True)
    _paths(inst)["stop"].unlink(missing_ok=True)
    events.log_event("control", f"{INSTANCES[inst]['label']} watcher stopped", "info")


def _instance_status(inst: str) -> dict:
    sp = _paths(inst)
    st = json.loads(sp["state"].read_text()) if sp["state"].exists() else {}
    running = _running(inst)
    if not running and st.get("status") not in (None, "stopped"):
        st["status"] = "stopped"
    return {
        "instance": inst, "label": INSTANCES[inst]["label"], "mode": INSTANCES[inst]["mode"],
        "running": running, "paused": sp["pause"].exists(), "state": st,
        "otp_pending": st.get("task") == "waiting for OTP" and running,
    }


# ---- API -----------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/api/status")
def status():
    cfg = Config.load()
    insts = {name: _instance_status(name) for name in INSTANCES}
    pub = insts["public"]
    return {
        # top-level keys kept for backward compatibility = the public instance
        "running": pub["running"], "paused": pub["paused"], "state": pub["state"],
        "otp_pending": insts["login"]["otp_pending"],
        "instances": insts,
        "accounts": AccountPool().status(),
        "browser": find_browser(cfg.browser.executable),
        "team": cfg.team, "counts_today": events.counts_today(),
        "now": datetime.now().isoformat(timespec="seconds"),
    }


class Control(BaseModel):
    action: str
    instance: str = "public"


@app.post("/api/control")
def control(c: Control):
    inst = c.instance if c.instance in INSTANCES else "public"
    sp = _paths(inst)
    if c.action == "start":
        _start_watcher(inst)
    elif c.action in ("stop", "kill"):
        _stop_watcher(inst)
    elif c.action == "pause":
        sp["pause"].touch()
    elif c.action == "resume":
        sp["pause"].unlink(missing_ok=True)
    else:
        raise HTTPException(400, "unknown action")
    return status()


class Otp(BaseModel):
    code: str


@app.post("/api/refresh")
def refresh(instance: str = "public"):
    inst = instance if instance in INSTANCES else "public"
    (STATE / f"{inst}.sweep_now").touch()
    return {"ok": True}


@app.post("/api/otp")
def otp(o: Otp):
    code = o.code.strip()
    if not code.isdigit():
        raise HTTPException(400, "OTP must be digits")
    OTP_FILE.parent.mkdir(parents=True, exist_ok=True)
    OTP_FILE.write_text(code)
    events.log_event("otp", "OTP entered in web tool", "info")
    return {"ok": True}


@app.get("/api/config")
def get_config():
    cfg = Config.load()
    return {"config": cfg.model_dump(mode="json"), "all_centres": ALL_CENTRES, "categories": KNOWN_CATEGORIES}


class ConfigIn(BaseModel):
    config: dict


@app.post("/api/config")
def set_config(body: ConfigIn):
    cfg = Config.model_validate(body.config)
    cfg.save()
    events.log_event("control", "Settings saved", "info")
    return {"ok": True}


# ---- accounts (data/accounts.json — passwords never leave the server unmasked) ----------

MASK = "••••••"


def _mask_proxy(p: str) -> str:
    """Hide the proxy password in the UI but keep host/port visible: scheme://user:••••••@host:port."""
    return re.sub(r"(://[^:@/]+:)[^@]+@", r"\1" + MASK + "@", p or "")


@app.get("/api/accounts")
def get_accounts():
    stat = {s["email"]: s for s in AccountPool().status()}
    return [{"label": a.get("label", ""), "email": a.get("email", ""), "orig_email": a.get("email", ""),
             "password": MASK if a.get("password") else "",
             "imap_host": a.get("imap_host") or "imap.gmail.com",
             "imap_user": a.get("imap_user", ""),
             "imap_password": MASK if a.get("imap_password") else "",
             "proxy": _mask_proxy(a.get("proxy", "")), "proxy_auto": bool(a.get("proxy_auto")),
             "enabled": bool(a.get("enabled", True)),
             "status": stat.get(a.get("email", ""), {})} for a in load_raw_accounts()]


class AccountsIn(BaseModel):
    accounts: list


@app.post("/api/accounts")
def set_accounts(body: AccountsIn):
    # Masked fields mean "unchanged": look the stored record up by the email the row was LOADED
    # with (orig_email), so editing the email doesn't lose or corrupt the stored secrets.
    current = {a.get("email"): a for a in load_raw_accounts()}
    out, errors = [], []
    for a in body.accounts:
        email = (a.get("email") or "").strip()
        if not email:
            continue
        cur = current.get((a.get("orig_email") or "").strip()) or current.get(email) or {}
        pw = a.get("password") or ""
        if pw == MASK:
            pw = cur.get("password", "")
        if not pw:
            errors.append(f"{email}: VFS password is empty")
        imap_pw = a.get("imap_password") or ""
        if imap_pw == MASK:
            imap_pw = cur.get("imap_password", "")
        imap_pw = imap_pw.replace(" ", "")   # Google shows App Passwords as "abcd efgh ijkl mnop"
        proxy = (a.get("proxy") or "").strip()
        if MASK in proxy:
            proxy = cur.get("proxy", "")
        auto = bool(cur.get("proxy_auto")) and proxy == cur.get("proxy", "")   # edited by hand → no longer auto
        if any(o["email"] == email for o in out):
            errors.append(f"{email}: listed twice")
            continue
        out.append({"label": (a.get("label") or "").strip(), "email": email, "password": pw,
                    "imap_host": (a.get("imap_host") or "imap.gmail.com").strip(),
                    "imap_user": (a.get("imap_user") or "").strip(), "imap_password": imap_pw,
                    "proxy": proxy, "proxy_auto": auto, "enabled": bool(a.get("enabled", True))})
    if errors:
        raise HTTPException(400, "; ".join(errors))
    save_raw_accounts(out)
    events.log_event("control", f"Saved {len(out)} account(s): " + ", ".join(o["label"] or o["email"] for o in out), "info")
    return {"ok": True, "count": len(out)}


@app.post("/api/accounts/test-imap")
def test_imap(body: dict):
    """Try the account's IMAP login (auto-OTP inbox) and report the result."""
    from ..accounts import Account, load_raw_accounts
    from ..otp import test_imap_login
    email = (body.get("email") or "").strip()
    raw = next((a for a in load_raw_accounts() if a.get("email") == email), None)
    if not raw:
        raise HTTPException(404, "no such account")
    a = Account.from_dict(raw)
    ok, msg = test_imap_login(a.imap_host, a.imap_login, a.imap_password)
    pool = AccountPool(); pool.mark_imap(a, "" if ok else msg)
    events.log_event("otp", f"IMAP test for {a.name} ({a.imap_login}): {msg}", "info" if ok else "warn")
    return {"ok": ok, "message": msg, "user": a.imap_login}


@app.post("/api/accounts/test-ip")
def test_ip(body: dict):
    """Launch a browser through the account's proxy and report the IP it gets (blocks ~10 s)."""
    email = (body.get("email") or "").strip()
    try:
        r = subprocess.run([sys.executable, "-m", "vfsbot.cli", "ip", "--account", email, "--json"],
                           cwd=ROOT, capture_output=True, text=True, timeout=90)
        line = [l for l in r.stdout.splitlines() if l.startswith("[")]
        res = json.loads(line[-1])[0] if line else {"ok": False, "note": (r.stderr or r.stdout)[-200:]}
    except subprocess.TimeoutExpired:
        res = {"ok": False, "note": "timed out (proxy not answering?)"}
    events.log_event("control", f"Proxy test for {email}: {res.get('ip') or '-'} — {res.get('note')}", "info" if res.get("ok") else "warn")
    return res


# ---- automatic proxies (vfsbot.proxies) ---------------------------------------------------

_prov_state = {"running": False, "log": []}


def _prov_log(msg: str) -> None:
    _prov_state["log"].append(f"{datetime.now():%H:%M:%S} {msg}")
    _prov_state["log"] = _prov_state["log"][-60:]
    events.log_event("control", "proxy: " + msg, "info")


def _prov_run(fn) -> None:
    import threading
    if _prov_state["running"]:
        raise HTTPException(409, "a provisioning job is already running")

    def go():
        _prov_state["running"] = True
        try:
            fn(Config.load(), _prov_log)
        except Exception as e:  # noqa: BLE001
            _prov_log(f"FAILED — {e}")
        finally:
            _prov_state["running"] = False
    threading.Thread(target=go, daemon=True).start()


@app.get("/api/proxies")
def proxies_status():
    from .. import proxies
    return {"token_set": bool(proxies.load_token()), "running": _prov_state["running"],
            "log": _prov_state["log"], "accounts": proxies.status(), "providers": list(proxies.PROVIDERS),
            "public_key": proxies.public_key()}


@app.post("/api/ip-rotate/test")
def ip_rotate_test():
    """Run the IP rotation command once and report before/after (blocks up to ~2 min)."""
    from ..cli import _direct_ip, rotate_ip
    from ..watcher import ProxyError
    cfg = Config.load()
    before = _direct_ip(cfg.rotation.ip_check_url)
    try:
        after = rotate_ip(cfg, before)
        return {"ok": after != before, "before": before, "after": after}
    except ProxyError as e:
        return {"ok": False, "before": before, "after": "", "error": str(e)}


@app.post("/api/proxies/manual")
def proxies_manual(body: dict):
    """Attach a server you created yourself (free tier etc.) to an account."""
    from .. import proxies
    email, ip, user = (body.get("email") or "").strip(), (body.get("ip") or "").strip(), (body.get("user") or "root").strip()
    port = int(body.get("ssh_port") or 22)
    _prov_run(lambda cfg, log: proxies.attach_manual(email, ip, user, port, log))
    return {"ok": True}


class ProxyToken(BaseModel):
    token: str


@app.post("/api/proxies/token")
def proxies_token(body: ProxyToken):
    from .. import proxies
    if body.token and body.token != MASK:
        proxies.save_token(body.token.strip())
        events.log_event("control", "Cloud API token saved", "info")
    return {"ok": True}


@app.post("/api/proxies/provision")
def proxies_provision(body: dict):
    """Provision a server + tunnel for one account (email) or for every account without a proxy."""
    from .. import proxies
    email = (body.get("email") or "").strip()
    if email:
        _prov_run(lambda cfg, log: proxies.provision(cfg, email, log))
    else:
        _prov_run(lambda cfg, log: proxies.provision_missing(cfg, log))
    return {"ok": True}


@app.post("/api/proxies/destroy")
def proxies_destroy(body: dict):
    from .. import proxies
    email = (body.get("email") or "").strip()
    if email:
        _prov_run(lambda cfg, log: proxies.deprovision(cfg, email, log))
    else:
        def all_(cfg, log):
            for e in list(proxies.load_proxies()):
                proxies.deprovision(cfg, e, log)
            proxies.cleanup_orphans(cfg, log)
        _prov_run(all_)
    return {"ok": True}


@app.post("/api/accounts/clear-cooloff")
def clear_cooloff(body: dict):
    pool = AccountPool()
    pool.clear_cooloff((body.get("email") or "").strip())
    events.log_event("control", f"Cool-off cleared for {body.get('email')}", "info")
    return {"ok": True}


@app.post("/api/passport")
async def passport(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".pdf"):
        raise HTTPException(400, "PNG, JPG or PDF only")
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(400, "VFS limit is 2MB — compress the image first")
    dest = ROOT / "documents" / f"passport_bio{ext}"
    dest.parent.mkdir(exist_ok=True)
    dest.write_bytes(data)
    cfg = Config.load()
    cfg.passport_file = str(dest.relative_to(ROOT))
    cfg.save()
    events.log_event("upload", f"Passport file saved ({file.filename}, {len(data)//1024} KB)", "info")
    return {"ok": True, "path": cfg.passport_file, "size": len(data)}


@app.get("/api/passport")
def passport_info():
    cfg = Config.load()
    p = ROOT / cfg.passport_file
    return {"path": cfg.passport_file, "exists": p.exists(), "size": p.stat().st_size if p.exists() else 0}


@app.get("/api/events")
def get_events(limit: int = 200, kind: str | None = None, level: str | None = None, since_id: int = 0):
    return events.recent(limit=limit, kind=kind or None, level=level or None, since_id=since_id)


@app.get("/api/log")
def raw_log(lines: int = 300):
    if not LOG_FILE.exists():
        return {"lines": []}
    data = LOG_FILE.read_text(errors="ignore").splitlines()
    return {"lines": data[-lines:]}


class WaTest(BaseModel):
    kind: str = "slot"      # "slot" = full escalation to every contact (messages + calls) | "notice" = bot-issue contacts only


@app.post("/api/whatsapp-test")
def whatsapp_test(body: WaTest):
    import subprocess
    cfg = Config.load()
    if body.kind == "notice":
        args = ["notice", "\u2705 VFS bot test notice — you are set to receive bot issues (OTP / needs human / errors)."]
    else:
        args = ["slot", ("\U0001F9EA VFS bot escalation TEST\nCentre: Cochin\nEarliest: 23-09-2026\n"
                         "Category: " + cfg.category + "\n\nThis is a test of the slot alert. Reply "
                         + f"'{cfg.whatsapp_web.ack_keyword}' to stop the calls."), "TEST"]
    log = open(STATE / "ack.out", "a")
    subprocess.Popen([sys.executable, "-m", "vfsbot.ack", *args], cwd=ROOT, stdout=log,
                     stderr=subprocess.STDOUT, start_new_session=True)
    events.log_event("ack", f"WhatsApp {body.kind} test started from web tool", "info")
    return {"ok": True}


@app.get("/api/screenshots")
def list_shots(limit: int = 50):
    files = sorted(SHOTS.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [{"name": f.name, "ts": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")} for f in files]


def run(host: str = "127.0.0.1", port: int = 8787) -> int:
    import uvicorn

    print(f"VFS Slot Watcher web tool: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
