"""Pool of VFS accounts for the login watcher.

Each sweep logs in with a *different* account (least recently used), through that account's own
proxy and browser profile, then closes the browser. Spreads the login footprint: with 4 accounts and
a 45-min interval, each account signs in once every ~3 h, from its own IP.

Accounts live in data/accounts.json (edited on the dashboard; the whole data/ folder is gitignored):
    {"label": "Amal", "email": "...", "password": "...",
     "imap_host": "imap.gmail.com", "imap_user": "", "imap_password": "",   # own inbox for auto-OTP
     "proxy": "socks5://user:pass@host:port",       # per-account proxy = per-account IP ("" = direct)
     "enabled": true}
Runtime bookkeeping (last use, IP, cool-off) is in data/accounts_state.json.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

DATA_DIR = Path("data")
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
STATE_FILE = DATA_DIR / "accounts_state.json"
PROFILES_ROOT = Path("browser-profile")


def slug(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", email.lower()).strip("_") or "account"


@dataclass
class Account:
    label: str
    email: str
    password: str
    imap_host: str = "imap.gmail.com"
    imap_user: str = ""
    imap_password: str = ""
    proxy: str = ""
    proxy_auto: bool = False     # proxy was provisioned by vfsbot.proxies (tunnel is started on demand)
    enabled: bool = True
    passport_file: str = ""

    @property
    def name(self) -> str:
        return self.label or self.email.split("@")[0]

    @property
    def profile_dir(self) -> str:
        # one Brave profile per account: separate cookies / cf_clearance / fingerprint
        return str(PROFILES_ROOT / slug(self.email))

    @property
    def imap_login(self) -> str:
        return self.imap_user or self.email

    def playwright_proxy(self) -> dict | None:
        """'scheme://user:pass@host:port' -> Playwright's {server, username, password}."""
        if not self.proxy.strip():
            return None
        u = urlsplit(self.proxy.strip() if "://" in self.proxy else "http://" + self.proxy.strip())
        out = {"server": f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"}
        if u.username:
            out["username"] = u.username
            out["password"] = u.password or ""
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        return cls(label=d.get("label", ""), email=d.get("email", "").strip(), password=d.get("password", ""),
                   imap_host=(d.get("imap_host") or "imap.gmail.com").strip(),
                   imap_user=d.get("imap_user", "").strip(), imap_password=d.get("imap_password", ""),
                   proxy=d.get("proxy", "").strip(), proxy_auto=bool(d.get("proxy_auto")), enabled=bool(d.get("enabled", True)),
                   passport_file=d.get("passport_file", ""))


def load_raw_accounts() -> list[dict]:
    """The accounts file as stored (passwords included) — for the dashboard editor."""
    _migrate()
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        return [a for a in json.loads(ACCOUNTS_FILE.read_text()) if a.get("email")]
    except Exception:  # noqa: BLE001
        return []


def save_raw_accounts(raw: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(raw, indent=2))


def load_accounts() -> list[Account]:
    return [Account.from_dict(a) for a in load_raw_accounts()]


def _migrate() -> None:
    """Older versions kept accounts under state/; move them into data/ once."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for old, new in ((Path("state/accounts.json"), ACCOUNTS_FILE), (Path("state/accounts_state.json"), STATE_FILE)):
        if old.exists() and not new.exists():
            old.replace(new)


def load_state() -> dict:
    _migrate()
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2, default=str))


def _now() -> datetime:
    return datetime.now()


class AccountPool:
    """Round-robin over enabled accounts, skipping the ones that are cooling off."""

    def __init__(self, accounts: list[Account] | None = None, rest_hours: float = 0.0):
        self.accounts = accounts if accounts is not None else load_accounts()
        self.state = load_state()
        self.rest_hours = rest_hours          # keep an account idle this long after it was used

    def _rec(self, email: str) -> dict:
        return self.state.setdefault(email, {})

    def cooling_until(self, a: Account) -> datetime | None:
        raw = self._rec(a.email).get("cooldown_until")
        if not raw:
            return None
        until = datetime.fromisoformat(raw)
        return until if until > _now() else None

    def resting_until(self, a: Account) -> datetime | None:
        """When this account may be used again (spreading logins over the day), or None."""
        if not self.rest_hours:
            return None
        raw = self._rec(a.email).get("last_used_at")
        if not raw:
            return None
        until = datetime.fromisoformat(raw) + timedelta(hours=self.rest_hours)
        return until if until > _now() else None

    def free_at(self, a: Account) -> datetime | None:
        """The later of the cool-off and the rest period, or None when the account is ready."""
        times = [t for t in (self.cooling_until(a), self.resting_until(a)) if t]
        return max(times) if times else None

    def available(self) -> list[Account]:
        return [a for a in self.accounts if a.enabled and a.password and not self.free_at(a)]

    def next(self, exclude: str = "") -> Account | None:
        """Least recently used account that is enabled, not cooling off and past its rest period."""
        cands = [a for a in self.available() if a.email != exclude] or self.available()
        if not cands:
            return None
        cands.sort(key=lambda a: self._rec(a.email).get("last_used_at") or "")
        return cands[0]

    def next_free_at(self) -> datetime | None:
        """When the first account becomes usable again (cool-off or rest), None if one is ready now."""
        times = [self.free_at(a) for a in self.accounts if a.enabled and a.password and self.free_at(a)]
        return min(times) if times else None

    def mark_used(self, a: Account, ip: str = "") -> None:
        r = self._rec(a.email)
        r["last_used_at"] = _now().isoformat(timespec="seconds")
        if ip:
            r["last_ip"] = ip
        save_state(self.state)

    def mark_login_ok(self, a: Account) -> None:
        r = self._rec(a.email)
        r["logins"] = int(r.get("logins", 0)) + 1
        r["last_login_at"] = _now().isoformat(timespec="seconds")
        r["fails"] = 0
        r.pop("last_error", None)
        save_state(self.state)

    def mark_cooloff(self, a: Account, hours: float, reason: str) -> datetime:
        r = self._rec(a.email)
        r["fails"] = int(r.get("fails", 0)) + 1
        # each consecutive failure doubles the cool-off (2h, 4h, 8h ...), capped at a day
        until = _now() + timedelta(hours=min(hours * (2 ** (r["fails"] - 1)), 24))
        r["cooldown_until"] = until.isoformat(timespec="seconds")
        r["last_error"] = reason[:200]
        save_state(self.state)
        return until

    def mark_imap(self, a: Account, error: str) -> None:
        r = self._rec(a.email)
        if error:
            r["imap_error"] = error[:160]
        else:
            r.pop("imap_error", None)
        save_state(self.state)

    def clear_cooloff(self, email: str) -> None:
        self._rec(email).pop("cooldown_until", None)
        save_state(self.state)

    def status(self) -> list[dict]:
        """Per-account summary for the dashboard."""
        out = []
        for a in self.accounts:
            r = self._rec(a.email)
            cu = self.cooling_until(a)
            out.append({
                "label": a.label, "email": a.email, "name": a.name, "enabled": a.enabled,
                "has_proxy": bool(a.proxy), "has_imap": bool(a.imap_password),
                "last_used_at": r.get("last_used_at"), "last_login_at": r.get("last_login_at"),
                "last_ip": r.get("last_ip"), "logins": r.get("logins", 0), "fails": r.get("fails", 0),
                "cooldown_until": cu.isoformat(timespec="seconds") if cu else None,
                "resting_until": (lambda r: r.isoformat(timespec="seconds") if r else None)(self.resting_until(a)),
                "imap_error": r.get("imap_error"),
                "last_error": r.get("last_error"),
            })
        return out
