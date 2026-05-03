from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Callable

from . import __version__
from .config import CocodexConfig
from .failures import next_step_for_session
from .git import current_head, is_dirty
from .guard import main_guard_status
from .state import get_lock, get_session, list_events, list_queue, list_sessions
from .tasks import task_file_path, validation_file_path


def format_status(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    *,
    now: Callable[[], float] = time.time,
) -> str:
    lock = get_lock(db)
    sessions = list_sessions(db)
    queue = list_queue(db)
    now_value = now()
    lines = [
        f"daemon_version: {__version__}",
        f"main: {current_head(repo, config.main_branch)}",
        f"remote: {config.remote or 'none'}",
        f"guard: {main_guard_status(repo, main_branch=config.main_branch)}",
        f"lock: {lock['owner']} ({lock['task_id']})" if lock else "lock: free",
        "sessions:",
    ]
    for session in sessions:
        reason = f" reason={session.blocked_reason}" if session.blocked_reason else ""
        task = f" task={session.active_task}" if session.active_task else ""
        connection = "connected" if session.connected else "disconnected"
        runtime = f" {connection}"
        if session.pid is not None:
            runtime += f" pid={session.pid}"
        if session.last_heartbeat is not None:
            heartbeat_age = max(0.0, now_value - session.last_heartbeat)
            runtime += f" heartbeat_age={heartbeat_age:.1f}s"
        if session.control_socket:
            runtime += f" socket={session.control_socket}"
        version = f" agent_version={session.agent_version}" if session.agent_version else ""
        head = _safe_head(Path(session.worktree))
        dirty = _safe_dirty(Path(session.worktree))
        detail = f" head={head[:12]}" if head else ""
        if dirty is not None:
            detail += f" dirty={str(dirty).lower()}"
        if session.last_seen_main:
            detail += f" last_seen_main={session.last_seen_main[:12]}"
        lines.append(f"  {session.name}: {session.state}{task}{reason}{version}{detail}{runtime}")
    lines.append("queue: " + (", ".join(queue) if queue else "empty"))
    return "\n".join(lines) + "\n"


def format_events(db: sqlite3.Connection) -> str:
    lines = []
    for event in list_events(db):
        lines.append(f"{event['id']} {event['type']} {json.dumps(event['payload'], sort_keys=True)}")
    return "\n".join(lines) + ("\n" if lines else "")


def format_task_status(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session_name: str,
) -> str:
    session = get_session(db, session_name)
    if session is None:
        raise RuntimeError(f"Unknown session: {session_name}")
    lock = get_lock(db)
    lines = [
        f"Session: {session.name}",
        f"State: {session.state}",
        next_step_for_session(
            session=session.name,
            state=session.state,
            active_task=session.active_task,
            blocked_reason=session.blocked_reason,
        ),
        f"Branch: {session.branch}",
        f"Worktree: {session.worktree}",
        f"Main: {current_head(repo, config.main_branch)}",
        f"Last seen main: {session.last_seen_main or 'unknown'}",
        f"Lock: {lock['owner']} ({lock['task_id']})" if lock else "Lock: free",
    ]
    if session.blocked_reason:
        lines.append(f"Reason: {session.blocked_reason}")
    if session.active_task is None:
        lines.append("Active task: none")
        return "\n".join(lines) + "\n"

    task_id = session.active_task
    task_path = task_file_path(repo, task_id)
    validation_path = validation_file_path(repo, task_id)
    lines.extend(
        [
            f"Active task: {task_id}",
            f"Task file: {task_path}",
            f"Validation file: {validation_path}",
            f"Snapshot ref: refs/cocodex/snapshots/{task_id}",
            f"Snapshot commit: {_safe_head(repo, f'refs/cocodex/snapshots/{task_id}') or 'missing'}",
            f"Base ref: refs/cocodex/bases/{task_id}",
            f"Base commit: {_safe_head(repo, f'refs/cocodex/bases/{task_id}') or 'missing'}",
        ]
    )
    if session.state in {"blocked", "recovery_required"}:
        lines.append(f"Resume: cocodex resume {session.name}")
        lines.append(f"Abandon: cocodex abandon {session.name}")
    return "\n".join(lines) + "\n"


def _safe_head(repo: Path, ref: str = "HEAD") -> str | None:
    try:
        return current_head(repo, ref)
    except Exception:
        return None


def _safe_dirty(repo: Path) -> bool | None:
    try:
        return is_dirty(repo)
    except Exception:
        return None
