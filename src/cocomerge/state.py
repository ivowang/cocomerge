from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SessionRecord:
    name: str
    branch: str
    worktree: str
    state: str
    last_seen_main: str | None
    active_task: str | None
    blocked_reason: str | None
    pid: int | None = None
    control_socket: str | None = None
    last_heartbeat: float | None = None
    connected: bool = False


def connect(repo: Path) -> sqlite3.Connection:
    db_path = repo / ".cocomerge" / "state.sqlite"
    db_path.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    return db


def initialize_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            name TEXT PRIMARY KEY,
            branch TEXT NOT NULL,
            worktree TEXT NOT NULL,
            state TEXT NOT NULL,
            last_seen_main TEXT,
            active_task TEXT,
            blocked_reason TEXT,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS queue (
            position INTEGER PRIMARY KEY AUTOINCREMENT,
            session TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS locks (
            name TEXT PRIMARY KEY,
            owner TEXT,
            task_id TEXT,
            CHECK (
                (owner IS NULL AND task_id IS NULL)
                OR (owner IS NOT NULL AND task_id IS NOT NULL)
            )
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO locks (name, owner, task_id)
        VALUES ('integration', NULL, NULL);
        """
    )
    _ensure_column(db, "sessions", "pid", "INTEGER")
    _ensure_column(db, "sessions", "control_socket", "TEXT")
    _ensure_column(db, "sessions", "last_heartbeat", "REAL")
    _ensure_column(db, "sessions", "connected", "INTEGER NOT NULL DEFAULT 0")
    db.commit()


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    if column not in {row["name"] for row in rows}:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def register_session(db: sqlite3.Connection, record: SessionRecord) -> None:
    db.execute(
        """
        INSERT INTO sessions
            (name, branch, worktree, state, last_seen_main, active_task, blocked_reason, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            branch=excluded.branch,
            worktree=excluded.worktree,
            state=excluded.state,
            last_seen_main=excluded.last_seen_main,
            active_task=excluded.active_task,
            blocked_reason=excluded.blocked_reason,
            updated_at=excluded.updated_at
        """,
        (
            record.name,
            record.branch,
            record.worktree,
            record.state,
            record.last_seen_main,
            record.active_task,
            record.blocked_reason,
            time.time(),
        ),
    )
    _record_event(db, "session_registered", {"session": record.name, "state": record.state})
    db.commit()


def get_session(db: sqlite3.Connection, name: str) -> SessionRecord | None:
    row = db.execute("SELECT * FROM sessions WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    return _row_to_session(row)


def list_sessions(db: sqlite3.Connection) -> list[SessionRecord]:
    rows = db.execute("SELECT * FROM sessions ORDER BY name ASC").fetchall()
    return [_row_to_session(row) for row in rows]


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        name=row["name"],
        branch=row["branch"],
        worktree=row["worktree"],
        state=row["state"],
        last_seen_main=row["last_seen_main"],
        active_task=row["active_task"],
        blocked_reason=row["blocked_reason"],
        pid=row["pid"],
        control_socket=row["control_socket"],
        last_heartbeat=row["last_heartbeat"],
        connected=bool(row["connected"]),
    )


def transition_session(
    db: sqlite3.Connection,
    name: str,
    state: str,
    *,
    reason: str | None = None,
    active_task: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    db.execute(
        """
        UPDATE sessions
        SET state = ?, active_task = ?, blocked_reason = ?, updated_at = ?
        WHERE name = ?
        """,
        (state, active_task, blocked_reason, time.time(), name),
    )
    _record_event(
        db,
        "session_transition",
        {"session": name, "state": state, "reason": reason, "active_task": active_task},
    )
    db.commit()


def update_last_seen_main(db: sqlite3.Connection, name: str, commit_sha: str) -> None:
    db.execute(
        """
        UPDATE sessions
        SET last_seen_main = ?, updated_at = ?
        WHERE name = ?
        """,
        (commit_sha, time.time(), name),
    )
    _record_event(db, "session_main_seen", {"session": name, "commit": commit_sha})
    db.commit()


def update_session_runtime(
    db: sqlite3.Connection,
    name: str,
    *,
    pid: int | None,
    control_socket: str | None,
    connected: bool,
    heartbeat: float | None,
) -> None:
    cursor = db.execute(
        """
        UPDATE sessions
        SET pid = ?, control_socket = ?, connected = ?, last_heartbeat = ?, updated_at = ?
        WHERE name = ?
        """,
        (pid, control_socket, int(connected), heartbeat, time.time(), name),
    )
    if cursor.rowcount != 1:
        db.rollback()
        raise ValueError(f"Unknown session: {name}")
    _record_event(
        db,
        "session_runtime_updated",
        {
            "session": name,
            "pid": pid,
            "control_socket": control_socket,
            "connected": connected,
            "last_heartbeat": heartbeat,
        },
    )
    db.commit()


def touch_session_heartbeat(db: sqlite3.Connection, name: str, heartbeat: float) -> None:
    cursor = db.execute(
        """
        UPDATE sessions
        SET connected = 1, last_heartbeat = ?, updated_at = ?
        WHERE name = ?
        """,
        (heartbeat, time.time(), name),
    )
    if cursor.rowcount != 1:
        db.rollback()
        raise ValueError(f"Unknown session: {name}")
    db.commit()


def mark_session_disconnected(db: sqlite3.Connection, name: str, reason: str) -> None:
    cursor = db.execute(
        """
        UPDATE sessions
        SET connected = 0, updated_at = ?
        WHERE name = ?
        """,
        (time.time(), name),
    )
    if cursor.rowcount != 1:
        db.rollback()
        raise ValueError(f"Unknown session: {name}")
    _record_event(db, "session_disconnected", {"session": name, "reason": reason})
    db.commit()


def enqueue_session(db: sqlite3.Connection, name: str) -> None:
    cursor = db.execute("INSERT OR IGNORE INTO queue (session) VALUES (?)", (name,))
    if cursor.rowcount:
        _record_event(db, "session_queued", {"session": name})
    db.commit()


def list_queue(db: sqlite3.Connection) -> list[str]:
    rows = db.execute("SELECT session FROM queue ORDER BY position ASC").fetchall()
    return [row["session"] for row in rows]


def dequeue_session(db: sqlite3.Connection, name: str) -> None:
    cursor = db.execute("DELETE FROM queue WHERE session = ?", (name,))
    if cursor.rowcount:
        _record_event(db, "session_dequeued", {"session": name})
    db.commit()


def claim_integration_task(
    db: sqlite3.Connection,
    name: str,
    task_id: str,
    *,
    reason: str,
) -> None:
    lock = db.execute(
        "SELECT owner, task_id FROM locks WHERE name = 'integration'"
    ).fetchone()
    if lock is None or lock["owner"] is not None or lock["task_id"] is not None:
        raise RuntimeError("integration lock is already held")
    cursor = db.execute(
        """
        UPDATE sessions
        SET state = ?, active_task = ?, blocked_reason = ?, updated_at = ?
        WHERE name = ?
        """,
        ("queued", task_id, None, time.time(), name),
    )
    if cursor.rowcount != 1:
        db.rollback()
        raise ValueError(f"Unknown session: {name}")
    db.execute(
        "UPDATE locks SET owner = ?, task_id = ? WHERE name = 'integration'",
        (name, task_id),
    )
    _record_event(
        db,
        "session_transition",
        {"session": name, "state": "queued", "reason": reason, "active_task": task_id},
    )
    _record_event(db, "lock_updated", {"owner": name, "task_id": task_id})
    db.commit()


def set_lock(db: sqlite3.Connection, owner: str | None, task_id: str | None) -> None:
    if (owner is None) != (task_id is None):
        raise ValueError("owner and task_id must both be set or both be None")
    db.execute(
        "UPDATE locks SET owner = ?, task_id = ? WHERE name = 'integration'",
        (owner, task_id),
    )
    _record_event(db, "lock_updated", {"owner": owner, "task_id": task_id})
    db.commit()


def get_lock(db: sqlite3.Connection) -> dict[str, str] | None:
    row = db.execute("SELECT owner, task_id FROM locks WHERE name = 'integration'").fetchone()
    if row is None or row["owner"] is None or row["task_id"] is None:
        return None
    return {"owner": row["owner"], "task_id": row["task_id"]}


def set_metadata(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO metadata (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    db.commit()


def get_metadata(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def _record_event(db: sqlite3.Connection, event_type: str, payload: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO events (created_at, type, payload) VALUES (?, ?, ?)",
        (time.time(), event_type, json.dumps(payload, sort_keys=True)),
    )


def record_event(db: sqlite3.Connection, event_type: str, payload: dict[str, Any]) -> None:
    _record_event(db, event_type, payload)
    db.commit()


def list_events(db: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT id, created_at, type, payload FROM (
            SELECT id, created_at, type, payload FROM events ORDER BY id DESC LIMIT ?
        ) ORDER BY id ASC
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "type": row["type"],
            "payload": json.loads(row["payload"]),
        }
        for row in rows
    ]
