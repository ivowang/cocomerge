from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .config import CocodexConfig
from .git import current_head
from .state import get_lock, list_events, list_queue, list_sessions


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
        f"main: {current_head(repo, config.main_branch)}",
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
        lines.append(f"  {session.name}: {session.state}{task}{reason}{runtime}")
    lines.append("queue: " + (", ".join(queue) if queue else "empty"))
    return "\n".join(lines) + "\n"


def format_events(db: sqlite3.Connection) -> str:
    lines = []
    for event in list_events(db):
        lines.append(f"{event['id']} {event['type']} {json.dumps(event['payload'], sort_keys=True)}")
    return "\n".join(lines) + ("\n" if lines else "")
