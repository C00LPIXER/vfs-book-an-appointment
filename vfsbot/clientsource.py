"""VFS's `clientsource` request header.

It looks like a server-issued secret but is not: the SPA encrypts the current timestamp with an RSA
public key it ships to every visitor (`sessionStorage.csk_str` on the public login page). Replaying a
captured value therefore fails — it is a stale timestamp — which is exactly why repeated
`CheckIsSlotAvailable` calls used to come back `409 Repeated Delay` or `401101 Invalid Request`.

Generating a fresh one per request makes repeated authenticated calls legitimate, so a single login
can check every centre instead of needing a fresh login every two or three checks.

The key is cached in `data/csk_key.txt`; it only changes if VFS rotates it.
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key

log = logging.getLogger("vfsbot.clientsource")

KEY_FILE = Path("data/csk_key.txt")
PUBLIC_PAGE = "{base}/login"

_key_cache: tuple[str, object] | None = None      # (raw base64, loaded key)


def _load(raw: str):
    global _key_cache
    raw = raw.strip().strip('"').replace("\\n", "").replace("\n", "")
    if _key_cache and _key_cache[0] == raw:
        return _key_cache[1]
    key = load_der_public_key(base64.b64decode(raw))
    _key_cache = (raw, key)
    return key


def stored_key() -> str:
    try:
        return KEY_FILE.read_text().strip()
    except Exception:  # noqa: BLE001
        return ""


def save_key(raw: str) -> None:
    raw = raw.strip().strip('"').replace("\\n", "").replace("\n", "")
    if not raw:
        return
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if raw != stored_key():
        KEY_FILE.write_text(raw)
        log.info("stored VFS's clientsource key (%d chars)", len(raw))


def key_from_page(page) -> str:
    """Read `csk_str` out of a loaded VFS page and remember it."""
    try:
        raw = page.evaluate("() => sessionStorage.getItem('csk_str') || localStorage.getItem('csk_str') || ''")
    except Exception:  # noqa: BLE001
        return ""
    save_key(raw)
    return raw


def have_key() -> bool:
    return bool(stored_key())


def generate(raw: str = "") -> str:
    """A fresh `clientsource` for one request: RSA-OAEP(SHA-256) over the millisecond timestamp."""
    raw = raw or stored_key()
    if not raw:
        raise RuntimeError("no clientsource key yet — open a VFS page once so it can be read")
    key = _load(raw)
    ct = key.encrypt(str(int(time.time() * 1000)).encode(),
                     padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                  algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(ct).decode()


def generate_login(prefix: str = "GA", tz_hours: int = 5.5) -> str:
    """The login form wants `<prefix>;<ISO8601 local time>Z` encrypted instead of a timestamp."""
    raw = stored_key()
    if not raw:
        raise RuntimeError("no clientsource key yet")
    tz = timezone(timedelta(hours=tz_hours))
    ts = datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S")
    key = _load(raw)
    ct = key.encrypt(f"{prefix};{ts}Z".encode(),
                     padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                  algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(ct).decode()
