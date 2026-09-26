"""SQLite persistence for sessions, transcript entries, roles, and usage.

Writes are small and infrequent, so this uses the standard sqlite3 module
directly from the event loop.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .transcript import Entry, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    submission TEXT NOT NULL,
    constraints TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    created TEXT NOT NULL,
    last_activity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session INTEGER NOT NULL REFERENCES sessions(session_id),
    seq INTEGER NOT NULL,
    speaker TEXT NOT NULL,
    kind TEXT NOT NULL,
    phase TEXT NOT NULL,
    round INTEGER NOT NULL,
    text TEXT NOT NULL,
    citations TEXT NOT NULL DEFAULT '[]',
    message_ids TEXT NOT NULL DEFAULT '[]',
    created TEXT NOT NULL,
    UNIQUE (session, seq)
);
CREATE TABLE IF NOT EXISTS roles (
    session INTEGER NOT NULL REFERENCES sessions(session_id),
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (session, model)
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session INTEGER NOT NULL REFERENCES sessions(session_id),
    model TEXT NOT NULL,
    input INTEGER NOT NULL,
    output INTEGER NOT NULL,
    cached INTEGER NOT NULL,
    cache_write INTEGER NOT NULL DEFAULT 0,
    search_calls INTEGER NOT NULL,
    estimated_cost REAL NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        # Sessions used to be Discord threads; the key column was thread_id.
        columns = [r["name"] for r in self.db.execute("PRAGMA table_info(sessions)")]
        if "thread_id" in columns:
            self.db.execute("ALTER TABLE sessions RENAME COLUMN thread_id TO session_id")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # Sessions

    def create_session(self, session_id: int, mode: str, topic: str, title: str, submission: str,
                       constraints: str, settings: dict) -> None:
        ts = now_iso()
        self.db.execute(
            "INSERT INTO sessions (session_id, mode, topic, title, submission, constraints, settings, status, "
            "created, last_activity) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (session_id, mode, topic, title, submission, constraints, json.dumps(settings), ts, ts),
        )
        self.db.commit()

    def update_session(self, session_id: int, **fields) -> None:
        if "settings" in fields:
            fields["settings"] = json.dumps(fields["settings"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE sessions SET {cols} WHERE session_id = ?", (*fields.values(), session_id))
        self.db.commit()

    def touch(self, session_id: int) -> str:
        ts = now_iso()
        self.update_session(session_id, last_activity=ts)
        return ts

    def get_session(self, session_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["settings"] = json.loads(data["settings"])
        data["entries"] = self.entries(session_id)
        data["roles"] = self.roles(session_id)
        return data

    def open_session_ids(self) -> list[int]:
        rows = self.db.execute("SELECT session_id FROM sessions WHERE status = 'open' ORDER BY created")
        return [r["session_id"] for r in rows]

    # Entries

    def add_entry(self, session_id: int, e: Entry) -> None:
        self.db.execute(
            "INSERT INTO entries (session, seq, speaker, kind, phase, round, text, citations, message_ids, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, e.seq, e.speaker, e.kind, e.phase, e.round, e.text,
             json.dumps(e.citations), json.dumps(e.message_ids), e.created),
        )
        self.db.execute("UPDATE sessions SET last_activity = ? WHERE session_id = ?", (e.created, session_id))
        self.db.commit()

    def set_message_ids(self, session_id: int, seq: int, message_ids: list[int]) -> None:
        self.db.execute(
            "UPDATE entries SET message_ids = ? WHERE session = ? AND seq = ?",
            (json.dumps(message_ids), session_id, seq),
        )
        self.db.commit()

    def entries(self, session_id: int) -> list[Entry]:
        rows = self.db.execute("SELECT * FROM entries WHERE session = ? ORDER BY seq", (session_id,))
        return [
            Entry(seq=r["seq"], speaker=r["speaker"], kind=r["kind"], phase=r["phase"], round=r["round"],
                  text=r["text"], citations=json.loads(r["citations"]), message_ids=json.loads(r["message_ids"]),
                  created=r["created"])
            for r in rows
        ]

    # Roles

    def set_role(self, session_id: int, model: str, role: str | None) -> None:
        if role:
            self.db.execute(
                "INSERT INTO roles (session, model, role) VALUES (?, ?, ?) "
                "ON CONFLICT (session, model) DO UPDATE SET role = excluded.role",
                (session_id, model, role),
            )
        else:
            self.db.execute("DELETE FROM roles WHERE session = ? AND model = ?", (session_id, model))
        self.db.commit()

    def roles(self, session_id: int) -> dict[str, str]:
        rows = self.db.execute("SELECT model, role FROM roles WHERE session = ?", (session_id,))
        return {r["model"]: r["role"] for r in rows}

    # Usage

    def add_usage(self, session_id: int, model: str, input_tokens: int, output_tokens: int, cached: int,
                  cache_write: int, search_calls: int, cost: float) -> None:
        self.db.execute(
            "INSERT INTO usage (session, model, input, output, cached, cache_write, search_calls, estimated_cost, "
            "created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, model, input_tokens, output_tokens, cached, cache_write, search_calls, cost, now_iso()),
        )
        self.db.commit()

    def usage_by_model(self, session_id: int) -> dict[str, dict]:
        rows = self.db.execute(
            "SELECT model, COUNT(*) AS calls, SUM(input) AS input, SUM(output) AS output, SUM(cached) AS cached, "
            "SUM(cache_write) AS cache_write, SUM(search_calls) AS search_calls, SUM(estimated_cost) AS cost "
            "FROM usage WHERE session = ? GROUP BY model",
            (session_id,),
        )
        return {r["model"]: dict(r) for r in rows}

    # Rotation counters shared across sessions

    def next_counter(self, name: str) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (name,)).fetchone()
        value = int(row["value"]) if row else 0
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (name, str(value + 1)),
        )
        self.db.commit()
        return value
