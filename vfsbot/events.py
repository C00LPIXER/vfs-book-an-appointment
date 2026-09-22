"""Append-only event log (SQLite) shared by the bot and the web UI."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path("state/events.db")
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute(
        """CREATE TABLE IF NOT EXISTS events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts TEXT NOT NULL,
               level TEXT NOT NULL,      -- info | warn | error | alert
               kind TEXT NOT NULL,       -- login | otp | check | alert | error | blocked | control | upload
               message TEXT NOT NULL,
               data TEXT,                -- json
               screenshot TEXT           -- relative path under state/screenshots
           )"""
    )
    return c


def log_event(kind: str, message: str, level: str = "info", data: dict | None = None, screenshot: str | None = None) -> int:
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO events (ts, level, kind, message, data, screenshot) VALUES (?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), level, kind, message,
             json.dumps(data, default=str) if data else None, screenshot),
        )
        return cur.lastrowid


def recent(limit: int = 200, kind: str | None = None, level: str | None = None, since_id: int = 0) -> list[dict]:
    q = "SELECT id, ts, level, kind, message, data, screenshot FROM events WHERE id > ?"
    args: list = [since_id]
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    if level:
        q += " AND level = ?"
        args.append(level)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [
        {"id": r[0], "ts": r[1], "level": r[2], "kind": r[3], "message": r[4],
         "data": json.loads(r[5]) if r[5] else None, "screenshot": r[6]}
        for r in rows
    ]


def counts_today() -> dict:
    today = datetime.now().date().isoformat()
    with _conn() as c:
        rows = c.execute("SELECT kind, level, COUNT(*) FROM events WHERE ts >= ? GROUP BY kind, level", (today,)).fetchall()
    out: dict = {}
    for kind, level, n in rows:
        out[f"{kind}:{level}"] = n
    return out
