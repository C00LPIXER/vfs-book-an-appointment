"""Automatic per-account proxies: one tiny cloud server per VFS account, reached through an SSH
SOCKS tunnel. Zero per-account configuration — give the bot a cloud API token once and it
provisions, tunnels, verifies and assigns an IP to every account (including ones added later).

    data/proxy_provider.json   {"token": "..."}                       (secret, gitignored)
    data/proxy_key[.pub]       SSH keypair generated on first use
    data/proxies.json          {email: {provider, instance_id, ip, port, created_at}}
    config.yaml → proxy_auto   enabled / provider / region / plan

Tunnel per account:  ssh -N -D 127.0.0.1:<port> root@<ip>   → account.proxy = socks5://127.0.0.1:<port>
The port is derived from the account (18000 + index) and the tunnel is (re)started on demand by
`ensure_tunnel()` right before a browser is launched for that account.

Providers (both: Ubuntu image, root login with the generated key, Indian regions):
    digitalocean  POST /v2/droplets            region blr1 (Bangalore)   size s-1vcpu-512mb-10gb (~$4/mo)
    vultr         POST /v2/instances           region bom (Mumbai) / del (Delhi)   plan vc2-1c-1gb (~$5/mo)
Datacenter IPs get more Cloudflare scrutiny than residential ones; if Turnstile keeps stalling for
every account, switch the accounts' proxy field to a residential provider instead.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx

from .accounts import DATA_DIR, load_raw_accounts, save_raw_accounts, slug

log = logging.getLogger("vfsbot.proxies")

PROVIDER_FILE = DATA_DIR / "proxy_provider.json"
PROXIES_FILE = DATA_DIR / "proxies.json"
KEY_FILE = DATA_DIR / "proxy_key"
BASE_PORT = 18000
TAG = "vfs-proxy"

DEFAULTS = {
    "digitalocean": {"region": "blr1", "plan": "s-1vcpu-512mb-10gb", "image": "ubuntu-24-04-x64"},
    "vultr":        {"region": "bom",  "plan": "vc2-1c-1gb",         "image": "2284"},   # Ubuntu 24.04 LTS
}


# ---- local files ---------------------------------------------------------------------

def load_token() -> str:
    try:
        return json.loads(PROVIDER_FILE.read_text()).get("token", "")
    except Exception:  # noqa: BLE001
        return ""


def save_token(token: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROVIDER_FILE.write_text(json.dumps({"token": token}))


def load_proxies() -> dict:
    try:
        return json.loads(PROXIES_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_proxies(d: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROXIES_FILE.write_text(json.dumps(d, indent=2))


def ensure_keypair() -> str:
    """Generate data/proxy_key (ed25519) once; return the public key text."""
    pub = KEY_FILE.with_suffix(".pub")
    if not KEY_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "vfsbot-proxy", "-f", str(KEY_FILE)], check=True)
    return pub.read_text().strip()


def port_for(email: str) -> int:
    """Stable local port per account (by position in accounts.json, falling back to a hash)."""
    emails = [a.get("email") for a in load_raw_accounts()]
    idx = emails.index(email) if email in emails else abs(hash(email)) % 500
    return BASE_PORT + idx


# ---- cloud providers ------------------------------------------------------------------

class Provider:
    name = ""
    base = ""

    def __init__(self, token: str, region: str = "", plan: str = ""):
        self.token = token
        self.region = region or DEFAULTS[self.name]["region"]
        self.plan = plan or DEFAULTS[self.name]["plan"]
        self.image = DEFAULTS[self.name]["image"]
        self.http = httpx.Client(base_url=self.base, headers={"Authorization": f"Bearer {token}"}, timeout=40)

    def _ok(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            raise RuntimeError(f"{self.name} API {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    # subclasses
    def ensure_key(self, pub: str) -> str: ...
    def create(self, name: str, key_id: str) -> str: ...
    def status(self, instance_id: str) -> tuple[str, str]: ...   # (state, ip)
    def delete(self, instance_id: str) -> None: ...
    def list_ours(self) -> list[dict]: ...


class DigitalOcean(Provider):
    name = "digitalocean"
    base = "https://api.digitalocean.com/v2"

    def ensure_key(self, pub: str) -> str:
        r = self.http.post("/account/keys", json={"name": "vfsbot-proxy", "public_key": pub})
        if r.status_code == 422:   # already exists → find it by public key
            for k in self._ok(self.http.get("/account/keys", params={"per_page": 200})).get("ssh_keys", []):
                if k.get("public_key", "").split()[:2] == pub.split()[:2]:
                    return str(k["id"])
        return str(self._ok(r)["ssh_key"]["id"])

    def create(self, name: str, key_id: str) -> str:
        body = {"name": name, "region": self.region, "size": self.plan, "image": self.image,
                "ssh_keys": [key_id], "tags": [TAG], "monitoring": False, "backups": False}
        return str(self._ok(self.http.post("/droplets", json=body))["droplet"]["id"])

    def status(self, instance_id: str) -> tuple[str, str]:
        d = self._ok(self.http.get(f"/droplets/{instance_id}"))["droplet"]
        ip = next((n["ip_address"] for n in d.get("networks", {}).get("v4", []) if n.get("type") == "public"), "")
        return d.get("status", ""), ip

    def delete(self, instance_id: str) -> None:
        r = self.http.delete(f"/droplets/{instance_id}")
        if r.status_code not in (204, 404):
            self._ok(r)

    def list_ours(self) -> list[dict]:
        d = self._ok(self.http.get("/droplets", params={"tag_name": TAG, "per_page": 200}))
        return [{"id": str(x["id"]), "name": x["name"],
                 "ip": next((n["ip_address"] for n in x.get("networks", {}).get("v4", []) if n.get("type") == "public"), "")}
                for x in d.get("droplets", [])]


class Vultr(Provider):
    name = "vultr"
    base = "https://api.vultr.com/v2"

    def ensure_key(self, pub: str) -> str:
        for k in self._ok(self.http.get("/ssh-keys", params={"per_page": 500})).get("ssh_keys", []):
            if k.get("ssh_key", "").split()[:2] == pub.split()[:2]:
                return k["id"]
        return self._ok(self.http.post("/ssh-keys", json={"name": "vfsbot-proxy", "ssh_key": pub}))["ssh_key"]["id"]

    def create(self, name: str, key_id: str) -> str:
        body = {"region": self.region, "plan": self.plan, "os_id": int(self.image), "label": name,
                "hostname": name, "sshkey_id": [key_id], "tags": [TAG], "backups": "disabled"}
        return self._ok(self.http.post("/instances", json=body))["instance"]["id"]

    def status(self, instance_id: str) -> tuple[str, str]:
        d = self._ok(self.http.get(f"/instances/{instance_id}"))["instance"]
        ip = d.get("main_ip", "")
        state = "active" if d.get("status") == "active" and d.get("server_status") == "ok" and ip not in ("", "0.0.0.0") else d.get("status", "")
        return state, ip

    def delete(self, instance_id: str) -> None:
        r = self.http.delete(f"/instances/{instance_id}")
        if r.status_code not in (204, 404):
            self._ok(r)

    def list_ours(self) -> list[dict]:
        d = self._ok(self.http.get("/instances", params={"tag": TAG, "per_page": 500}))
        return [{"id": x["id"], "name": x.get("label", ""), "ip": x.get("main_ip", "")} for x in d.get("instances", [])]


PROVIDERS = {"digitalocean": DigitalOcean, "vultr": Vultr}


def get_provider(cfg) -> Provider:
    token = load_token()
    if not token:
        raise RuntimeError("no cloud API token — paste one under Settings → Proxy auto-provision")
    cls = PROVIDERS.get(cfg.proxy_auto.provider)
    if not cls:
        raise RuntimeError(f"unknown provider {cfg.proxy_auto.provider!r}")
    return cls(token, cfg.proxy_auto.region, cfg.proxy_auto.plan)


# ---- tunnels -------------------------------------------------------------------------

def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ssh_base(ip: str) -> list[str]:
    return ["ssh", "-i", str(KEY_FILE), "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={DATA_DIR / 'proxy_known_hosts'}",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", f"root@{ip}"]


def wait_for_ssh(ip: str, timeout: int = 240) -> bool:
    """Wait for sshd on the new server and for our key to be accepted."""
    end = time.time() + timeout
    while time.time() < end:
        if _tcp(ip, 22):
            r = subprocess.run(_ssh_base(ip) + ["true"], capture_output=True, timeout=30)
            if r.returncode == 0:
                return True
        time.sleep(5)
    return False


def _tcp(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def ensure_tunnel(email: str, wait: float = 20) -> str:
    """Make sure the SOCKS tunnel for this account is listening; returns the proxy URL.
    A dead/missing tunnel is (re)started detached so it outlives the calling process."""
    rec = load_proxies().get(email)
    if not rec or not rec.get("ip"):
        raise RuntimeError(f"no provisioned proxy for {email}")
    port = rec["port"]
    if not _port_open(port):
        log.info("starting SOCKS tunnel for %s: 127.0.0.1:%d -> %s", email, port, rec["ip"])
        logf = open(DATA_DIR / "tunnels.log", "a")
        subprocess.Popen(_ssh_base(rec["ip"]) + ["-N", "-D", f"127.0.0.1:{port}", "-o", "ExitOnForwardFailure=yes"],
                         stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.time() + wait
        while time.time() < end and not _port_open(port):
            time.sleep(0.5)
        if not _port_open(port):
            raise RuntimeError(f"tunnel to {rec['ip']} did not come up (see data/tunnels.log)")
    return f"socks5://127.0.0.1:{port}"


def tunnel_up(email: str) -> bool:
    rec = load_proxies().get(email)
    return bool(rec) and _port_open(rec.get("port", 0))


def stop_tunnel(email: str) -> None:
    rec = load_proxies().get(email)
    if not rec:
        return
    subprocess.run(["pkill", "-f", f"ssh .* -D 127.0.0.1:{rec['port']} "], capture_output=True)
    subprocess.run(["pkill", "-f", f"-D 127.0.0.1:{rec['port']}$"], capture_output=True)


# ---- provisioning ----------------------------------------------------------------------

def _set_account_proxy(email: str, proxy: str, auto: bool) -> None:
    raw = load_raw_accounts()
    for a in raw:
        if a.get("email") == email:
            a["proxy"] = proxy
            a["proxy_auto"] = auto
    save_raw_accounts(raw)


def provision(cfg, email: str, progress=lambda msg: None) -> dict:
    """Create a server for `email`, wait for it, start the tunnel, assign the proxy. Idempotent:
    an existing healthy record is reused."""
    if shutil.which("ssh") is None or shutil.which("ssh-keygen") is None:
        raise RuntimeError("ssh / ssh-keygen not installed")
    prov = get_provider(cfg)
    proxies = load_proxies()
    rec = proxies.get(email)
    if rec and rec.get("provider") == prov.name and rec.get("ip"):
        try:
            state, ip = prov.status(rec["instance_id"])
            if state == "active" and ip:
                rec["ip"] = ip
                progress(f"{email}: server {ip} already exists")
            else:
                rec = None
        except RuntimeError:
            rec = None
    if not rec:
        pub = ensure_keypair()
        progress(f"{email}: registering SSH key with {prov.name}")
        key_id = prov.ensure_key(pub)
        name = f"{TAG}-{slug(email)}"[:63]
        progress(f"{email}: creating server {name} in {prov.region}")
        iid = prov.create(name, key_id)
        end = time.time() + 300
        ip = ""
        while time.time() < end:
            state, ip = prov.status(iid)
            if state == "active" and ip:
                break
            time.sleep(6)
        else:
            raise RuntimeError(f"{email}: server {iid} not active after 5 min")
        rec = {"provider": prov.name, "instance_id": iid, "ip": ip, "port": port_for(email),
               "created_at": datetime.now().isoformat(timespec="seconds")}
        proxies[email] = rec
        save_proxies(proxies)
        progress(f"{email}: server up at {ip}, waiting for SSH")
        if not wait_for_ssh(ip):
            raise RuntimeError(f"{email}: SSH to {ip} not accepting our key after 4 min")
    else:
        proxies[email] = rec
        save_proxies(proxies)
    progress(f"{email}: starting tunnel")
    url = ensure_tunnel(email)
    _set_account_proxy(email, url, True)
    progress(f"{email}: proxy ready → {url} (exit IP {rec['ip']})")
    return rec


def deprovision(cfg, email: str, progress=lambda msg: None) -> None:
    proxies = load_proxies()
    rec = proxies.pop(email, None)
    save_proxies(proxies)
    stop_tunnel(email)
    if rec:
        try:
            get_provider(cfg).delete(rec["instance_id"])
            progress(f"{email}: server {rec.get('ip')} destroyed")
        except RuntimeError as e:
            progress(f"{email}: could not delete server: {e}")
    raw = load_raw_accounts()
    for a in raw:
        if a.get("email") == email and a.get("proxy_auto"):
            a["proxy"] = ""; a["proxy_auto"] = False
    save_raw_accounts(raw)


def accounts_needing_proxy() -> list[str]:
    """Enabled accounts with no proxy at all (manual or auto)."""
    return [a["email"] for a in load_raw_accounts() if a.get("enabled", True) and not (a.get("proxy") or "").strip()]


def provision_missing(cfg, progress=lambda msg: None) -> list[str]:
    done = []
    for email in accounts_needing_proxy():
        try:
            provision(cfg, email, progress)
            done.append(email)
        except Exception as e:  # noqa: BLE001
            progress(f"{email}: FAILED — {e}")
            log.warning("provision %s failed: %s", email, e)
    return done


def cleanup_orphans(cfg, progress=lambda msg: None) -> int:
    """Destroy our tagged servers that no account references any more (removed accounts)."""
    prov = get_provider(cfg)
    known = {r["instance_id"] for r in load_proxies().values()}
    n = 0
    for inst in prov.list_ours():
        if inst["id"] not in known:
            prov.delete(inst["id"]); n += 1
            progress(f"destroyed orphan server {inst['name']} ({inst['ip']})")
    return n


def status(cfg=None) -> list[dict]:
    """Per-account proxy overview for the dashboard."""
    proxies = load_proxies()
    out = []
    for a in load_raw_accounts():
        email = a["email"]; rec = proxies.get(email, {})
        out.append({"email": email, "label": a.get("label", ""), "proxy": (a.get("proxy") or ""),
                    "auto": bool(a.get("proxy_auto")), "server_ip": rec.get("ip", ""), "provider": rec.get("provider", ""),
                    "tunnel_up": bool(rec) and _port_open(rec.get("port", 0)), "created_at": rec.get("created_at")})
    return out
