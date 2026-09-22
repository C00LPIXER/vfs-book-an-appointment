"""Local web tool: config, control, status, logs and screenshots for the watcher."""
from __future__ import annotations

import json
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
from ..config import ALL_CENTRES, KNOWN_CATEGORIES, Config, Secrets
from ..notify import Notifier
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

SECRET_KEYS = {"vfs_password", "imap_password", "smtp_password", "twilio_auth_token", "telegram_bot_token"}


# ---- process control (two instances: public + login) ----------------------------

INSTANCES = {
    "public": {"mode": "public", "profile": "public-profile", "label": "Public (no login)"},
    "login":  {"mode": "login",  "profile": "browser-profile", "label": "Logged-in (amalkrishnap)"},
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
    sec = Secrets().model_dump()
    masked = {k: ("••••••" if (k in SECRET_KEYS and v) else v) for k, v in sec.items()}
    return {"config": cfg.model_dump(mode="json"), "secrets": masked,
            "all_centres": ALL_CENTRES, "categories": KNOWN_CATEGORIES}


class ConfigIn(BaseModel):
    config: dict
    secrets: dict


@app.post("/api/config")
def set_config(body: ConfigIn):
    cfg = Config.model_validate(body.config)
    cfg.save()
    current = Secrets().model_dump()
    for k, v in body.secrets.items():
        if k in current and v != "••••••":
            current[k] = v
    Secrets.model_validate(current).save()
    events.log_event("control", "Settings saved", "info")
    return {"ok": True}


ACCOUNTS_FILE = STATE / "accounts.json"


def _load_accounts() -> list:
    if ACCOUNTS_FILE.exists():
        try:
            return json.loads(ACCOUNTS_FILE.read_text())
        except Exception:  # noqa: BLE001
            return []
    return []


@app.get("/api/accounts")
def get_accounts():
    accts = _load_accounts()
    return [{"label": a.get("label", ""), "email": a.get("email", ""),
             "password": "••••••" if a.get("password") else ""} for a in accts]


class AccountsIn(BaseModel):
    accounts: list


@app.post("/api/accounts")
def set_accounts(body: AccountsIn):
    current = {a.get("email"): a for a in _load_accounts()}
    out = []
    for a in body.accounts:
        email = (a.get("email") or "").strip()
        if not email:
            continue
        pw = a.get("password") or ""
        if pw == "••••••":            # unchanged -> keep stored password
            pw = current.get(email, {}).get("password", "")
        out.append({"label": (a.get("label") or "").strip(), "email": email, "password": pw})
    ACCOUNTS_FILE.write_text(json.dumps(out, indent=2))
    # mirror the FIRST account into the main VFS creds (used by the login watcher)
    if out:
        cur = Secrets().model_dump()
        cur["vfs_email"] = out[0]["email"]; cur["vfs_password"] = out[0]["password"]
        Secrets.model_validate(cur).save()
    events.log_event("control", f"Saved {len(out)} account(s)", "info")
    return {"ok": True, "count": len(out)}


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


@app.post("/api/test-notify")
def test_notify():
    cfg = Config.load()
    res = Notifier(cfg.notify, Secrets()).test_all()
    events.log_event("alert", "Test notifications: " + ", ".join(f"{k}={'ok' if v else 'no'}" for k, v in res.items()), "info")
    return res


@app.post("/api/whatsapp-test")
def whatsapp_test():
    import subprocess
    cfg = Config.load()
    msg = ("\U0001F9EA VFS bot escalation TEST\nCentre: Cochin\nEarliest: 23-09-2026\n"
           "Category: " + cfg.category + "\n\nThis is a test of the slot-alert escalation. Reply "
           + f"'{cfg.whatsapp_web.ack_keyword}' to stop the calls.")
    log = open(STATE / "ack.out", "a")
    subprocess.Popen([sys.executable, "-m", "vfsbot.ack", msg, "TEST"], cwd=ROOT, stdout=log,
                     stderr=subprocess.STDOUT, start_new_session=True)
    events.log_event("ack", "WhatsApp escalation test started from web tool", "info")
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
