"""SQLite persistence layer.

Owned by the application (visitors, credentials, session metadata, cards).
nanobot owns conversation *content* (JSONL session files under data/nanobot/sessions).

All DB access goes through Database; connection per operation (SQLite WAL mode),
executed in a threadpool by the route layer. Schema migrations via PRAGMA user_version.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    relationship TEXT NOT NULL DEFAULT '',
    disclosure_boundary TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    created_at REAL NOT NULL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_credentials_visitor ON credentials(visitor_id);

CREATE TABLE IF NOT EXISTS sessions (
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    session_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (visitor_id, session_key)
);

CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    visitor_id TEXT NOT NULL REFERENCES visitors(id) ON DELETE CASCADE,
    session_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    summary TEXT NOT NULL,
    context TEXT,
    status TEXT NOT NULL DEFAULT 'unread'
);
CREATE INDEX IF NOT EXISTS idx_cards_visitor ON cards(visitor_id);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
"""


def utcnow() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class Database:
    """Thread-safe SQLite wrapper (one connection per call, WAL mode)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _migrate(self) -> None:
        with self._lock, self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # visitors
    # ------------------------------------------------------------------

    def create_visitor(self, name: str, relationship: str = "",
                       disclosure_boundary: str = "") -> dict:
        now = utcnow()
        visitor = {
            "id": new_id(),
            "name": name,
            "relationship": relationship,
            "disclosure_boundary": disclosure_boundary,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO visitors (id, name, relationship, disclosure_boundary,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (visitor["id"], visitor["name"], visitor["relationship"],
                 visitor["disclosure_boundary"], now, now),
            )
        return visitor

    def list_visitors(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT v.*, (SELECT COUNT(*) FROM cards c WHERE c.visitor_id = v.id"
                " AND c.status = 'unread') AS unread_cards,"
                " (SELECT COUNT(*) FROM sessions s WHERE s.visitor_id = v.id) AS session_count"
                " FROM visitors v ORDER BY v.created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_visitor(self, visitor_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM visitors WHERE id = ?", (visitor_id,)).fetchone()
        return dict(row) if row else None

    def update_visitor(self, visitor_id: str, **fields) -> dict | None:
        allowed = {"name", "relationship", "disclosure_boundary"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get_visitor(visitor_id)
        updates["updated_at"] = utcnow()
        sets = ", ".join(f"{k} = ?" for k in updates)
        with self._lock, self._connect() as conn:
            cur = conn.execute(f"UPDATE visitors SET {sets} WHERE id = ?",
                               (*updates.values(), visitor_id))
            if cur.rowcount == 0:
                return None
        return self.get_visitor(visitor_id)

    def delete_visitor(self, visitor_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM visitors WHERE id = ?", (visitor_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------

    def create_credential(self, visitor_id: str, token_hash: str,
                          token_prefix: str) -> dict:
        cred = {
            "id": new_id(),
            "visitor_id": visitor_id,
            "token_hash": token_hash,
            "token_prefix": token_prefix,
            "created_at": utcnow(),
            "revoked_at": None,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO credentials (id, visitor_id, token_hash, token_prefix,"
                " created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?)",
                (cred["id"], cred["visitor_id"], cred["token_hash"],
                 cred["token_prefix"], cred["created_at"], None),
            )
        return cred

    def get_credential_by_hash(self, token_hash: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM credentials WHERE token_hash = ? AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
        return dict(row) if row else None

    def list_credentials(self, visitor_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, visitor_id, token_prefix, created_at, revoked_at"
                " FROM credentials WHERE visitor_id = ? ORDER BY created_at ASC",
                (visitor_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_credential(self, cred_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        return dict(row) if row else None

    def revoke_credential(self, cred_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (utcnow(), cred_id),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # sessions (metadata)
    # ------------------------------------------------------------------

    def create_session(self, visitor_id: str, session_key: str) -> dict:
        now = utcnow()
        session = {
            "visitor_id": visitor_id,
            "session_key": session_key,
            "created_at": now,
            "updated_at": now,
            "title": "",
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (visitor_id, session_key, created_at, updated_at, title)"
                " VALUES (?, ?, ?, ?, ?)",
                (visitor_id, session_key, now, now, ""),
            )
        return session

    def get_session(self, visitor_id: str, session_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE visitor_id = ? AND session_key = ?",
                (visitor_id, session_key),
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, visitor_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE visitor_id = ? ORDER BY updated_at DESC",
                (visitor_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def touch_session(self, visitor_id: str, session_key: str,
                      title: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            if title:
                conn.execute(
                    "UPDATE sessions SET updated_at = ?, title = ?"
                    " WHERE visitor_id = ? AND session_key = ? AND title = ''",
                    (utcnow(), title[:80], visitor_id, session_key),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE visitor_id = ? AND session_key = ?",
                    (utcnow(), visitor_id, session_key),
                )

    def delete_session(self, visitor_id: str, session_key: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE visitor_id = ? AND session_key = ?",
                (visitor_id, session_key),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # cards
    # ------------------------------------------------------------------

    def create_card(self, visitor_id: str, session_key: str, summary: str,
                    context: str | None = None) -> dict:
        card = {
            "id": new_id(),
            "visitor_id": visitor_id,
            "session_key": session_key,
            "created_at": utcnow(),
            "summary": summary,
            "context": context,
            "status": "unread",
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO cards (id, visitor_id, session_key, created_at, summary,"
                " context, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (card["id"], card["visitor_id"], card["session_key"],
                 card["created_at"], card["summary"], card["context"], card["status"]),
            )
        return card

    def list_cards(self, unread_only: bool = False, visitor_id: str | None = None) -> list[dict]:
        query = (
            "SELECT c.*, v.name AS visitor_name FROM cards c"
            " JOIN visitors v ON v.id = c.visitor_id"
        )
        conditions, params = [], []
        if unread_only:
            conditions.append("c.status = 'unread'")
        if visitor_id:
            conditions.append("c.visitor_id = ?")
            params.append(visitor_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY c.created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update_card_status(self, card_id: str, status: str) -> dict | None:
        if status not in {"unread", "read", "resolved"}:
            raise ValueError(f"invalid card status: {status}")
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE cards SET status = ? WHERE id = ?",
                               (status, card_id))
            if cur.rowcount == 0:
                return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return dict(row) if row else None


# ----------------------------------------------------------------------
# credential token helpers
# ----------------------------------------------------------------------

def generate_token(name_hint: str) -> tuple[str, str, str]:
    """Generate a visitor credential.

    Returns (plaintext_token, token_hash, token_prefix). The plaintext is shown
    once to the owner; only the sha256 hash is stored.
    """
    secret = secrets.token_urlsafe(12)
    token = f"{name_hint}-{secret}"
    return token, hash_token(token), token_prefix_of(token)


def hash_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix_of(token: str) -> str:
    return token[:4]
