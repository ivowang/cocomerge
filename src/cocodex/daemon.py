from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import __version__ as DAEMON_VERSION
from .config import CocodexConfig
from .git import (
    add_all,
    commit,
    create_backup_ref,
    current_head,
    diff,
    diff_check,
    fast_forward_ref,
    has_unsafe_git_state,
    is_dirty,
    merge_abort,
    merge_base_is_ancestor,
    merge_commit,
    reset_hard,
    update_ref,
)
from .guard import ensure_cocodex_excluded, install_main_guard
from .state import (
    SessionRecord,
    claim_integration_task,
    connect,
    dequeue_session,
    get_metadata,
    get_lock,
    get_session,
    initialize_schema,
    list_events_after,
    list_sessions,
    list_queue,
    mark_session_disconnected,
    record_event,
    register_session,
    set_lock,
    set_metadata,
    touch_session_heartbeat,
    transition_session,
    update_last_seen_main,
    update_session_runtime,
)
from .tasks import IntegrationTask, create_task_id, task_file_path, validate_task_report, write_task_file
from .protocol import decode_message
from .transport import send_message, serve_forever


READY_TO_INTEGRATE_STATES = {"clean", "dirty"}
ACTIVE_INTEGRATION_STATES = {"frozen", "snapshot", "fusing", "verifying", "publishing"}
LEGACY_RECOVERY_STATES = {"blocked", "recovery_required", "queued"} | ACTIVE_INTEGRATION_STATES

ControlSender = Callable[[SessionRecord, dict], dict]
TaskIdFactory = Callable[[str], str]


def _daemon_log(message: str, *, created_at: float | None = None, **fields: object) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(created_at or time.time()))
    details = " ".join(
        f"{key}={_format_log_value(value)}"
        for key, value in fields.items()
        if value is not None
    )
    suffix = f" {details}" if details else ""
    print(f"[{stamp} UTC] {message}{suffix}", file=sys.stderr, flush=True)


def _format_log_value(value: object) -> str:
    text = str(value)
    if not text:
        return "''"
    if any(ch.isspace() for ch in text):
        return repr(text)
    return text


def _latest_event_id(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COALESCE(MAX(id), 0) AS latest FROM events").fetchone()
    return int(row["latest"] if row is not None else 0)


def _emit_new_events(db: sqlite3.Connection, last_event_id: int) -> int:
    latest = last_event_id
    for event in list_events_after(db, last_event_id):
        latest = max(latest, int(event["id"]))
        _emit_event(event)
    return latest


def _emit_event(event: dict) -> None:
    event_type = event["type"]
    payload = event["payload"]
    created_at = event["created_at"]
    if event_type == "session_registered":
        _daemon_log(
            "session registered",
            created_at=created_at,
            session=payload.get("session"),
            state=payload.get("state"),
        )
    elif event_type == "session_runtime_updated":
        state = "connected" if payload.get("connected") else "runtime updated"
        _daemon_log(
            f"session {state}",
            created_at=created_at,
            session=payload.get("session"),
            pid=payload.get("pid"),
            socket=payload.get("control_socket"),
        )
    elif event_type == "session_transition":
        _daemon_log(
            "session state changed",
            created_at=created_at,
            session=payload.get("session"),
            state=payload.get("state"),
            reason=payload.get("reason"),
            task=payload.get("active_task"),
        )
    elif event_type == "session_queued":
        _daemon_log("session queued", created_at=created_at, session=payload.get("session"))
    elif event_type == "session_dequeued":
        _daemon_log("session dequeued", created_at=created_at, session=payload.get("session"))
    elif event_type == "lock_updated":
        owner = payload.get("owner")
        if owner:
            _daemon_log(
                "integration lock acquired",
                created_at=created_at,
                owner=owner,
                task=payload.get("task_id"),
            )
        else:
            _daemon_log("integration lock released", created_at=created_at)
    elif event_type == "session_main_seen":
        _daemon_log(
            "session saw main",
            created_at=created_at,
            session=payload.get("session"),
            commit=_short_commit(payload.get("commit")),
        )
    elif event_type == "session_disconnected":
        _daemon_log(
            "session disconnected",
            created_at=created_at,
            session=payload.get("session"),
            reason=payload.get("reason"),
        )
    elif event_type == "external_main_updated":
        _daemon_log(
            "external main update detected",
            created_at=created_at,
            previous=_short_commit(payload.get("previous")),
            current=_short_commit(payload.get("current")),
        )
    elif event_type == "remote_sync_failed":
        _daemon_log(
            "remote sync failed",
            created_at=created_at,
            session=payload.get("session"),
            task=payload.get("task_id"),
            error=payload.get("error"),
        )
    elif event_type == "version_mismatch":
        _daemon_log(
            "version mismatch",
            created_at=created_at,
            session=payload.get("session"),
            daemon=payload.get("daemon_version"),
            agent=payload.get("agent_version"),
        )
    else:
        _daemon_log(event_type, created_at=created_at, **payload)


def _short_commit(value: object) -> object:
    if not isinstance(value, str) or len(value) < 12:
        return value
    return value[:12]


def detect_disconnected_sessions(
    db: sqlite3.Connection,
    now: Callable[[], float] = time.time,
    timeout: float = 30.0,
) -> None:
    now_value = now()
    for session in list_sessions(db):
        if not session.connected or session.last_heartbeat is None:
            continue
        heartbeat_age = now_value - session.last_heartbeat
        if heartbeat_age <= timeout:
            continue

        reason = f"heartbeat timeout after {heartbeat_age:.1f}s"
        mark_session_disconnected(db, session.name, reason)


def recover_incomplete_sessions(repo: Path, db: sqlite3.Connection) -> None:
    _prune_legacy_queue(db)
    lock = get_lock(db)
    for session in list_sessions(db):
        if session.state not in LEGACY_RECOVERY_STATES and session.active_task is None:
            continue
        _normalize_session_after_startup(repo, db, session, lock)


def _normalize_session_after_startup(
    repo: Path,
    db: sqlite3.Connection,
    session: SessionRecord,
    lock: dict[str, str] | None,
) -> None:
    owns_lock = lock is not None and lock["owner"] == session.name
    lock_task_id = lock["task_id"] if owns_lock else None
    session_task_id = session.active_task

    if (
        owns_lock
        and session_task_id is not None
        and lock_task_id is not None
        and session_task_id != lock_task_id
    ):
        lock_task_path = task_file_path(repo, lock_task_id)
        if lock_task_path.exists():
            try:
                backup_ref = create_backup_ref(
                    Path(session.worktree),
                    session_name=session.name,
                    task_id=session_task_id,
                    reason=f"startup recovery before adopting lock task {lock_task_id}",
                )
            except Exception as exc:
                record_event(
                    db,
                    "recovery_backup_failed",
                    {
                        "session": session.name,
                        "task_id": session_task_id,
                        "lock_task_id": lock_task_id,
                        "reason": str(exc),
                    },
                )
                transition_session(
                    db,
                    session.name,
                    "fusing",
                    reason=f"startup recovery backup failed: {exc}",
                    active_task=session_task_id,
                    blocked_reason=None,
                )
                return
            record_event(
                db,
                "recovery_backup_created",
                {
                    "session": session.name,
                    "task_id": session_task_id,
                    "lock_task_id": lock_task_id,
                    "backup_ref": backup_ref,
                    "reason": "task_mismatch",
                },
            )
            transition_session(
                db,
                session.name,
                "fusing",
                reason=f"startup adopted lock task {lock_task_id} after task mismatch",
                active_task=lock_task_id,
                blocked_reason=None,
            )
            return

    task_id = lock_task_id if owns_lock else session_task_id

    if session.state == "queued" and task_id is None:
        dequeue_session(db, session.name)
        transition_session(
            db,
            session.name,
            "clean",
            reason="startup cleared queued sync request; rerun cocodex sync",
            active_task=None,
            blocked_reason=None,
        )
        return

    if task_id is None:
        transition_session(
            db,
            session.name,
            "clean",
            reason=f"startup cleared legacy {session.state} state",
            active_task=None,
            blocked_reason=None,
        )
        return

    task_path = task_file_path(repo, task_id)
    if owns_lock and task_path.exists():
        transition_session(
            db,
            session.name,
            "fusing",
            reason=f"startup restored active task from {session.state}",
            active_task=task_id,
            blocked_reason=None,
        )
        return

    try:
        backup_ref = create_backup_ref(
            Path(session.worktree),
            session_name=session.name,
            task_id=task_id,
            reason=f"startup recovery before clearing incomplete {session.state} task",
        )
    except Exception as exc:
        record_event(
            db,
            "recovery_backup_failed",
            {"session": session.name, "task_id": task_id, "reason": str(exc)},
        )
        transition_session(
            db,
            session.name,
            "fusing",
            reason=f"startup recovery backup failed: {exc}",
            active_task=task_id,
            blocked_reason=None,
        )
        return
    record_event(
        db,
        "recovery_backup_created",
        {"session": session.name, "task_id": task_id, "backup_ref": backup_ref},
    )
    _restore_task_snapshot_if_possible(session, task_id)
    if owns_lock:
        set_lock(db, owner=None, task_id=None)
    dequeue_session(db, session.name)
    transition_session(
        db,
        session.name,
        "clean",
        reason=f"startup restored snapshot and cleared incomplete {session.state} task",
        active_task=None,
        blocked_reason=None,
    )


def detect_external_main_update(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
) -> bool:
    observed = get_metadata(db, "last_observed_main")
    current = current_head(repo, config.main_branch)
    if observed is None:
        set_metadata(db, "last_observed_main", current)
        return False
    if observed == current:
        return False
    if _is_locked_pending_publish_recovery(db, current):
        return False

    record_event(db, "external_main_updated", {"previous": observed, "current": current})
    set_metadata(db, "last_observed_main", current)
    return True


def _is_locked_pending_publish_recovery(db: sqlite3.Connection, current_main: str) -> bool:
    _ = db, current_main
    return False


def _session_has_changes(session: SessionRecord) -> bool:
    worktree = Path(session.worktree)
    if is_dirty(worktree):
        return True
    if session.last_seen_main is None:
        return False
    return current_head(worktree) != session.last_seen_main


def _main_worktree_blocker(repo: Path) -> str | None:
    unsafe = has_unsafe_git_state(repo)
    if unsafe:
        return f"main worktree has unsafe Git state: {unsafe}"
    if is_dirty(repo):
        return "main worktree is dirty; clean or commit those files before running cocodex sync"
    return None


def _assert_main_publishable(repo: Path) -> None:
    blocker = _main_worktree_blocker(repo)
    if blocker is not None:
        raise RuntimeError(blocker)


def _sync_clean_session_to_main(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session: SessionRecord,
) -> str:
    worktree = Path(session.worktree)
    latest_main = current_head(repo, config.main_branch)
    head = current_head(worktree)
    if head == latest_main:
        if session.last_seen_main != latest_main:
            update_last_seen_main(db, session.name, latest_main)
        return "already synced"
    if session.last_seen_main is not None and head == session.last_seen_main:
        fast_forward_ref(worktree, session.branch, latest_main)
        update_last_seen_main(db, session.name, latest_main)
        return f"synced to {latest_main}"
    return "no changes to sync"


def publish_without_fusion_if_current(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session: SessionRecord,
    *,
    task_id_factory: TaskIdFactory = create_task_id,
) -> str | None:
    if session.last_seen_main is None or get_lock(db) is not None:
        return None

    latest_main = current_head(repo, config.main_branch)
    if latest_main != session.last_seen_main:
        return None

    _assert_main_publishable(repo)
    worktree = Path(session.worktree)
    unsafe = has_unsafe_git_state(worktree)
    if unsafe:
        raise RuntimeError(f"unsafe Git state: {unsafe}")

    head = current_head(worktree)
    if not merge_base_is_ancestor(worktree, latest_main, head):
        return None

    task_id = task_id_factory(session.name)
    set_lock(db, owner=session.name, task_id=task_id)
    try:
        try:
            latest_main = current_head(repo, config.main_branch)
            if latest_main != session.last_seen_main:
                transition_session(
                    db,
                    session.name,
                    "clean",
                    reason="main advanced before direct publish",
                    active_task=None,
                    blocked_reason=None,
                )
                return None

            transition_session(
                db,
                session.name,
                "publishing",
                reason="direct publish without fusion",
                active_task=task_id,
            )
            if is_dirty(worktree):
                add_all(worktree)
                candidate = commit(worktree, f"cocodex direct publish: {session.name}")
            else:
                candidate = head

            if candidate == latest_main:
                transition_session(
                    db,
                    session.name,
                    "clean",
                    reason="sync requested with no changes",
                    active_task=None,
                )
                update_last_seen_main(db, session.name, latest_main)
                return "already synced"

            current_main = current_head(repo, config.main_branch)
            if current_main != latest_main or not merge_base_is_ancestor(repo, current_main, candidate):
                transition_session(
                    db,
                    session.name,
                    "clean",
                    reason="main advanced before direct publish",
                    active_task=None,
                    blocked_reason=None,
                )
                return None

            fast_forward_ref(repo, config.main_branch, candidate)
            set_metadata(db, "last_observed_main", candidate)
            transition_session(
                db,
                session.name,
                "clean",
                reason="published without fusion",
                active_task=None,
            )
            update_last_seen_main(db, session.name, candidate)
        except Exception as exc:
            reason = f"direct publish failed: {exc}"
            transition_session(
                db,
                session.name,
                "clean",
                reason=reason,
                active_task=None,
                blocked_reason=None,
            )
            raise RuntimeError(reason) from exc
    finally:
        _release_lock_if_owned(db, session.name, task_id)

    return f"published directly to {candidate}"


def snapshot_session_work(
    repo: Path,
    config: CocodexConfig,
    session: SessionRecord,
    task_id: str,
) -> IntegrationTask:
    worktree = Path(session.worktree)
    latest_main = current_head(repo, config.main_branch)
    base = session.last_seen_main or latest_main
    head = current_head(worktree)
    if is_dirty(worktree):
        add_all(worktree)
        snapshot = commit(worktree, f"cocodex snapshot: {session.name} {task_id}")
    elif head != base:
        snapshot = head
    else:
        raise RuntimeError("no changes to snapshot")

    update_ref(worktree, f"refs/cocodex/snapshots/{task_id}", snapshot)
    update_ref(worktree, f"refs/cocodex/bases/{task_id}", latest_main)
    diff_summary = diff(worktree, base, snapshot)
    return IntegrationTask(
        task_id=task_id,
        session=session.name,
        latest_main=latest_main,
        last_seen_main=session.last_seen_main,
        snapshot_commit=snapshot,
        diff_summary=diff_summary,
    )


def prepare_locked_sync(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session_name: str,
    task_id: str,
) -> tuple[str, Path | str, IntegrationTask | None]:
    session = get_session(db, session_name)
    if session is None:
        raise RuntimeError(f"unknown session: {session_name}")
    worktree = Path(session.worktree)
    unsafe = has_unsafe_git_state(worktree)
    if unsafe:
        raise RuntimeError(f"unsafe Git state: {unsafe}")
    if get_lock(db) != {"owner": session_name, "task_id": task_id}:
        raise RuntimeError("integration lock is not held by this task")

    _assert_main_publishable(repo)
    transition_session(db, session_name, "snapshot", reason="creating snapshot", active_task=task_id)
    task = snapshot_session_work(repo, config, session, task_id)

    merged = publish_with_git_merge_if_clean(repo, db, config, session, task)
    if merged is not None:
        dequeue_session(db, session_name)
        return ("published", merged, task)

    reset_hard(worktree, task.latest_main)
    task_path = write_task_file(repo, task)
    dequeue_session(db, session_name)
    transition_session(db, session_name, "fusing", reason="task started", active_task=task_id)
    return ("task", task_path, task)


def publish_with_git_merge_if_clean(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session: SessionRecord,
    task: IntegrationTask,
) -> str | None:
    worktree = Path(session.worktree)
    _assert_main_publishable(repo)
    try:
        transition_session(
            db,
            session.name,
            "publishing",
            reason="attempting git merge without fusion",
            active_task=task.task_id,
        )
        merge_commit(
            worktree,
            task.latest_main,
            f"cocodex git merge: {session.name} {task.task_id}",
        )
        candidate = current_head(worktree)
        validate_git_merge_candidate(worktree, task, candidate)
    except Exception as exc:
        merge_abort(worktree)
        reset_hard(worktree, task.latest_main)
        record_event(
            db,
            "git_merge_fallback",
            {"session": session.name, "task_id": task.task_id, "reason": str(exc)},
        )
        return None

    try:
        fast_forward_ref(repo, config.main_branch, candidate)
        set_metadata(db, "last_observed_main", candidate)
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        transition_session(
            db,
            session.name,
            "clean",
            reason=reason,
            active_task=None,
            blocked_reason=None,
        )
        _release_lock_if_owned(db, session.name, task.task_id)
        raise

    transition_session(db, session.name, "clean", reason="published with git merge", active_task=None)
    update_last_seen_main(db, session.name, candidate)
    _release_lock_if_owned(db, session.name, task.task_id)
    return f"published with git merge to {candidate}"


def validate_git_merge_candidate(worktree: Path, task: IntegrationTask, candidate: str) -> None:
    unsafe = has_unsafe_git_state(worktree)
    if unsafe:
        raise RuntimeError(f"unsafe Git state after merge: {unsafe}")
    if is_dirty(worktree):
        raise RuntimeError("worktree is dirty after git merge")
    if not merge_base_is_ancestor(worktree, task.latest_main, candidate):
        raise RuntimeError("git merge candidate does not contain latest main")
    if not merge_base_is_ancestor(worktree, task.snapshot_commit, candidate):
        raise RuntimeError("git merge candidate does not contain session snapshot")
    diff_check(worktree, task.latest_main, candidate)


def send_control_message(session: SessionRecord, message: dict) -> dict:
    if not session.control_socket:
        raise RuntimeError(f"session has no control socket: {session.name}")
    raw = send_message(Path(session.control_socket), message, timeout=5)
    return decode_message(raw)


def start_integration_now(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session: SessionRecord,
    *,
    send_control: ControlSender = send_control_message,
    task_id_factory: TaskIdFactory = create_task_id,
) -> str:
    if not session.connected or not session.control_socket:
        raise RuntimeError(
            f"semantic merge requires an active Cocodex session for {session.name}; "
            f"run `cocodex join {session.name}` from that developer's tmux pane and retry"
        )

    task_id = task_id_factory(session.name)
    claim_integration_task(db, session.name, task_id, reason="sync requested")
    task: IntegrationTask | None = None
    try:
        freeze = send_control(
            session,
            {
                "type": "freeze",
                "session": session.name,
                "task_id": task_id,
                "reason": "integration lock acquired",
            },
        )
        if not _control_response_matches(
            freeze,
            expected_type="freeze_ack",
            session_name=session.name,
            task_id=task_id,
        ):
            reason = freeze.get("message") or freeze.get("reason") or "freeze failed"
            raise RuntimeError(str(reason))

        transition_session(db, session.name, "frozen", reason="freeze acknowledged", active_task=task_id)
        sync_result, sync_payload, task = prepare_locked_sync(
            repo,
            db,
            config,
            session.name,
            task_id,
        )
        if sync_result == "published":
            return str(sync_payload)

        task_path = Path(sync_payload)
        refreshed = get_session(db, session.name)
        if refreshed is None:
            raise RuntimeError(f"unknown session after prepare: {session.name}")
        response = send_control(
            refreshed,
            {
                "type": "start_fusion",
                "session": session.name,
                "task_id": task_id,
                "task_file": str(task_path),
            },
        )
        if not _control_response_matches(
            response,
            expected_type="ack",
            session_name=session.name,
            task_id=task_id,
        ):
            reason = response.get("message") or response.get("reason") or "start_fusion failed"
            raise RuntimeError(str(reason))
        if not response.get("prompt_injected"):
            reason = response.get("prompt_error") or "semantic merge prompt was not injected into the session"
            raise RuntimeError(str(reason))
        return f"started semantic merge task {task_id}"
    except Exception:
        if get_session(db, session.name) is not None:
            _restore_task_snapshot_if_possible(session, task_id)
        _release_lock_if_owned(db, session.name, task_id)
        dequeue_session(db, session.name)
        transition_session(
            db,
            session.name,
            "clean",
            reason="sync rejected before semantic task could safely start",
            active_task=None,
            blocked_reason=None,
        )
        raise


def process_queue_once(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    *,
    send_control: ControlSender = send_control_message,
    task_id_factory: TaskIdFactory = create_task_id,
) -> bool:
    _ = repo, config, send_control, task_id_factory
    _prune_legacy_queue(db)
    return False


def _prune_legacy_queue(db: sqlite3.Connection) -> None:
    for session_name in list_queue(db):
        session = get_session(db, session_name)
        if session is None:
            dequeue_session(db, session_name)
            continue
        dequeue_session(db, session_name)
        if session is not None and session.state == "queued" and session.active_task is None:
            transition_session(
                db,
                session.name,
                "clean",
                reason="cleared legacy queued sync request",
                active_task=None,
                blocked_reason=None,
            )


def publish_candidate(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session_name: str,
    task_id: str,
    candidate: str,
    *,
    send_control: ControlSender = send_control_message,
) -> None:
    session = get_session(db, session_name)
    if session is None or session.active_task != task_id:
        raise RuntimeError("stale or unknown task")

    expected_lock = {"owner": session_name, "task_id": task_id}
    if get_lock(db) != expected_lock:
        raise RuntimeError("integration lock is not held by this task")
    if session.state not in {"fusing", "verifying", "publishing"}:
        raise RuntimeError(f"invalid session state for publish: {session.state}")

    worktree = Path(session.worktree)
    try:
        task_base = _require_active_task_integrity(repo, worktree, task_id)
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        transition_session(
            db,
            session_name,
            "fusing",
            reason=reason,
            active_task=task_id,
            blocked_reason=None,
        )
        raise RuntimeError(reason) from exc

    try:
        unsafe = has_unsafe_git_state(worktree)
    except Exception as exc:
        reason = f"worktree unavailable: {exc}"
        transition_session(
            db,
            session_name,
            "fusing",
            reason=reason,
            active_task=task_id,
            blocked_reason=None,
        )
        raise RuntimeError(reason) from exc

    if unsafe:
        raise RuntimeError(f"unsafe Git state: {unsafe}")

    try:
        session_head = current_head(worktree)
    except Exception as exc:
        reason = f"worktree unavailable: {exc}"
        transition_session(
            db,
            session_name,
            "fusing",
            reason=reason,
            active_task=task_id,
            blocked_reason=None,
        )
        raise RuntimeError(reason) from exc

    if candidate != session_head:
        reason = "candidate is not current session head"
        transition_session(
            db,
            session_name,
            "fusing",
            reason=reason,
            active_task=task_id,
            blocked_reason=None,
        )
        raise RuntimeError(reason)

    if candidate == task_base:
        raise RuntimeError(
            "sync task has no candidate commit; implement the snapshot feature "
            "or create an explicit no-op commit before running sync again"
        )

    try:
        dirty_before_validation = is_dirty(worktree)
    except Exception as exc:
        reason = f"worktree unavailable: {exc}"
        raise RuntimeError(reason) from exc
    if dirty_before_validation:
        raise RuntimeError("worktree is dirty before validation")

    transition_session(db, session_name, "verifying", reason="candidate reported", active_task=task_id)
    validation_error = validate_task_report(repo, task_id)
    if validation_error is not None:
        transition_session(
            db,
            session_name,
            "fusing",
            reason="validation report required; sync refused",
            active_task=task_id,
            blocked_reason=None,
        )
        raise RuntimeError(validation_error)

    try:
        verified_head = current_head(worktree)
        verified_dirty = is_dirty(worktree)
    except Exception as exc:
        reason = f"worktree unavailable after validation: {exc}"
        raise RuntimeError(reason) from exc

    if verified_head != candidate:
        reason = "candidate changed during validation"
        raise RuntimeError(reason)

    if verified_dirty:
        raise RuntimeError("worktree changed during validation")

    transition_session(db, session_name, "publishing", reason="validation report accepted", active_task=task_id)
    _assert_main_publishable(repo)
    try:
        fast_forward_ref(repo, config.main_branch, candidate)
        set_metadata(db, "last_observed_main", candidate)
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        transition_session(
            db,
            session_name,
            "fusing",
            reason=reason,
            active_task=task_id,
            blocked_reason=None,
        )
        raise RuntimeError(reason) from exc

    transition_session(db, session_name, "clean", reason="published", active_task=None)
    update_last_seen_main(db, session_name, candidate)
    _release_lock_if_owned(db, session_name, task_id)


def _require_active_task_integrity(repo: Path, worktree: Path, task_id: str) -> str:
    task_path = task_file_path(repo, task_id)
    if not task_path.exists():
        raise RuntimeError(f"task file is missing for active task {task_id}: {task_path}")
    _required_task_ref(worktree, f"refs/cocodex/snapshots/{task_id}", "snapshot", task_id)
    return _required_task_ref(worktree, f"refs/cocodex/bases/{task_id}", "base", task_id)


def _required_task_ref(worktree: Path, ref: str, label: str, task_id: str) -> str:
    try:
        return current_head(worktree, ref)
    except Exception as exc:
        raise RuntimeError(f"{label} ref is missing for active task {task_id}: {ref}") from exc


def _restore_task_snapshot_if_possible(session: SessionRecord, task_id: str) -> bool:
    worktree = Path(session.worktree)
    try:
        snapshot = current_head(worktree, f"refs/cocodex/snapshots/{task_id}")
    except Exception:
        return False
    try:
        reset_hard(worktree, snapshot)
    except Exception:
        return False
    return True


def _control_response_matches(
    response: dict,
    *,
    expected_type: str,
    session_name: str,
    task_id: str,
) -> bool:
    return (
        response.get("type") == expected_type
        and response.get("session") == session_name
        and response.get("task_id") == task_id
    )


def _release_lock_if_owned(db: sqlite3.Connection, session_name: str, task_id: str) -> None:
    if get_lock(db) == {"owner": session_name, "task_id": task_id}:
        set_lock(db, owner=None, task_id=None)


def handle_session_message(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    message: dict,
    *,
    now: Callable[[], float] = time.time,
    send_control: ControlSender = send_control_message,
    task_id_factory: TaskIdFactory = create_task_id,
) -> dict:
    message_type = message.get("type")
    session_name = message.get("session")

    if message_type == "register":
        agent_version = _message_agent_version(message)
        branch = message.get("branch") or f"cocodex/{session_name}"
        worktree = message.get("worktree") or str(repo / config.worktree_root / session_name)
        existing = get_session(db, session_name)
        if existing is not None:
            if existing.state == "deleting":
                raise RuntimeError(
                    f"session {session_name} is being deleted; wait for delete to finish before rejoining"
                )
            if existing.branch != branch or existing.worktree != worktree:
                raise RuntimeError(f"conflicting registration for session: {session_name}")
            update_session_runtime(
                db,
                session_name,
                pid=message.get("pid"),
                control_socket=message.get("control_socket"),
                connected=True,
                heartbeat=now(),
                agent_version=agent_version,
            )
            _handle_agent_version(db, session_name, agent_version, reject=True)
            return {"type": "registered", "session": session_name}
        record = SessionRecord(
            name=session_name,
            branch=branch,
            worktree=worktree,
            state="clean",
            last_seen_main=current_head(repo, config.main_branch),
            active_task=None,
            blocked_reason=None,
        )
        if agent_version is not None and agent_version != DAEMON_VERSION:
            raise RuntimeError(_version_mismatch_message(agent_version))
        register_session(db, record)
        update_session_runtime(
            db,
            session_name,
            pid=message.get("pid"),
            control_socket=message.get("control_socket"),
            connected=True,
            heartbeat=now(),
            agent_version=agent_version,
        )
        return {"type": "registered", "session": session_name}

    if message_type == "heartbeat":
        session = get_session(db, session_name)
        if session is None:
            raise RuntimeError(f"unknown session: {session_name}")
        if session.state == "deleting":
            raise RuntimeError(f"session {session_name} is being deleted")
        agent_version = _message_agent_version(message)
        touch_session_heartbeat(db, session_name, now(), agent_version=agent_version)
        _handle_agent_version(db, session_name, agent_version, reject=False)
        return {"type": "ack", "session": session_name}

    if message_type == "shutdown":
        if get_session(db, session_name) is None:
            raise RuntimeError(f"unknown session: {session_name}")
        mark_session_disconnected(db, session_name, message.get("reason") or "shutdown")
        return {"type": "ack", "session": session_name}

    if message_type == "ready_to_integrate":
        session = get_session(db, session_name)
        if session is None:
            raise RuntimeError(f"unknown session: {session_name}")
        _reject_stale_agent(session)
        if session.state in {"blocked", "recovery_required", "queued"} and session.active_task is None:
            dequeue_session(db, session.name)
            transition_session(
                db,
                session.name,
                "clean",
                reason=f"cleared legacy {session.state} state before sync",
                active_task=None,
                blocked_reason=None,
            )
            session = get_session(db, session_name) or session
        session = _normalize_unknown_baseline(repo, db, config, session)
        if session.state not in READY_TO_INTEGRATE_STATES:
            raise RuntimeError(f"invalid session state for ready_to_integrate: {session.state}")
        if session.active_task is not None:
            raise RuntimeError(f"session has active task: {session_name}")
        busy_message = _integration_busy_message(db, session_name)
        if busy_message is not None:
            raise RuntimeError(busy_message)
        if not _session_has_changes(session):
            sync_message = _sync_clean_session_to_main(repo, db, config, session)
            dequeue_session(db, session_name)
            if session.state != "clean":
                transition_session(
                    db,
                    session_name,
                    "clean",
                    reason="sync requested with no changes",
                    active_task=None,
                )
            return {
                "type": "ack",
                "session": session_name,
                "message": sync_message,
            }
        direct_message = publish_without_fusion_if_current(repo, db, config, session)
        if direct_message is not None:
            dequeue_session(db, session_name)
            return {
                "type": "ack",
                "session": session_name,
                "message": direct_message,
            }
        started_message = start_integration_now(
            repo,
            db,
            config,
            session,
            send_control=send_control,
            task_id_factory=task_id_factory,
        )
        return {
            "type": "ack",
            "session": session_name,
            "message": started_message,
        }

    if message_type == "freeze_ack":
        task_id = message["task_id"]
        session = get_session(db, session_name)
        if session is None or session.active_task != task_id:
            raise RuntimeError("stale or unknown task")
        if session.state != "queued":
            raise RuntimeError(f"invalid session state for freeze_ack: {session.state}")
        transition_session(
            db,
            session_name,
            "frozen",
            reason="session frozen",
            active_task=task_id,
        )
        return {"type": "ack", "session": session_name, "task_id": task_id}

    if message_type == "fusion_done":
        task_id = message["task_id"]
        session = get_session(db, session_name)
        if session is None:
            raise RuntimeError(f"unknown session: {session_name}")
        session = _normalize_active_task_for_sync(repo, db, session, task_id)
        candidate = current_head(Path(session.worktree))
        publish_candidate(repo, db, config, session_name, task_id, candidate)
        return {"type": "ack", "session": session_name, "task_id": task_id}

    return {"type": "ack", "session": session_name}


def _message_agent_version(message: dict) -> str | None:
    value = message.get("agent_version")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuntimeError("agent_version must be a non-empty string")
    return value


def _version_mismatch_message(agent_version: str) -> str:
    return (
        f"version mismatch: daemon {DAEMON_VERSION}, session agent {agent_version}; "
        "restart `cocodex join` after upgrading Cocodex"
    )


def _handle_agent_version(
    db: sqlite3.Connection,
    session_name: str,
    agent_version: str | None,
    *,
    reject: bool,
) -> None:
    if agent_version is None or agent_version == DAEMON_VERSION:
        return
    reason = _version_mismatch_message(agent_version)
    record_event(
        db,
        "version_mismatch",
        {
            "session": session_name,
            "daemon_version": DAEMON_VERSION,
            "agent_version": agent_version,
        },
    )
    if reject:
        raise RuntimeError(reason)


def _reject_stale_agent(session: SessionRecord) -> None:
    if session.agent_version is None or session.agent_version == DAEMON_VERSION:
        return
    raise RuntimeError(_version_mismatch_message(session.agent_version))


def _normalize_unknown_baseline(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session: SessionRecord,
) -> SessionRecord:
    if session.last_seen_main is not None:
        return session
    worktree = Path(session.worktree)
    head = current_head(worktree)
    latest_main = current_head(repo, config.main_branch)
    if head == latest_main or merge_base_is_ancestor(worktree, latest_main, head):
        baseline = latest_main
    elif merge_base_is_ancestor(worktree, head, latest_main):
        baseline = head
    else:
        raise RuntimeError(
            "unknown session baseline: this legacy session branch is divergent from main; "
            "preserve the worktree and ask an operator to inspect it before syncing"
        )
    update_last_seen_main(db, session.name, baseline)
    record_event(
        db,
        "unknown_baseline_adopted",
        {"session": session.name, "baseline": baseline, "head": head, "main": latest_main},
    )
    return get_session(db, session.name) or session


def _normalize_active_task_for_sync(
    repo: Path,
    db: sqlite3.Connection,
    session: SessionRecord,
    task_id: str,
) -> SessionRecord:
    if session.active_task != task_id:
        raise RuntimeError("stale or unknown task")
    lock = get_lock(db)
    if lock is None:
        set_lock(db, owner=session.name, task_id=task_id)
    elif lock != {"owner": session.name, "task_id": task_id}:
        if lock["owner"] != session.name:
            busy = _integration_busy_message(db, session.name)
            raise RuntimeError(busy or f"integration busy: {lock['owner']} is syncing")
        raise RuntimeError(
            f"{session.name} has an inconsistent sync task. Run `cocodex status` "
            "and keep this worktree unchanged; Cocodex will repair it on daemon restart."
        )

    if session.state in {"blocked", "recovery_required", "queued", "frozen", "snapshot", "verifying", "publishing"}:
        transition_session(
            db,
            session.name,
            "fusing",
            reason=f"normalized {session.state} before sync",
            active_task=task_id,
            blocked_reason=None,
        )
        return get_session(db, session.name) or session
    return session


def _integration_busy_message(db: sqlite3.Connection, requester: str) -> str | None:
    lock = get_lock(db)
    if lock is not None and lock["owner"] != requester:
        owner = get_session(db, lock["owner"])
        if owner is not None and not owner.connected:
            return (
                f"integration busy: {lock['owner']} is disconnected while syncing. "
                f"{lock['owner']} must run `cocodex join {lock['owner']}` from the project root, "
                f"then run `cocodex sync` from {owner.worktree}. "
                f"{requester} should keep this worktree unchanged and retry after {lock['owner']} finishes. "
                "Run `cocodex status` or `cocodex log` for details."
            )
        if owner is not None:
            return (
                f"integration busy: {lock['owner']} is syncing. "
                f"{requester} should keep this worktree unchanged and retry after {lock['owner']} finishes. "
                f"If {lock['owner']}'s Codex session is closed, they should run "
                f"`cocodex join {lock['owner']}` from the project root, then `cocodex sync` "
                f"from {owner.worktree}. Run `cocodex status` or `cocodex log` for details."
            )
        return (
            f"integration busy: {lock['owner']} is syncing. "
            "Keep this worktree unchanged and retry after the lock owner finishes. "
            "Run `cocodex status` or `cocodex log` for details."
        )
    return None


def start_socket_server(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
) -> threading.Event:
    stop_event = threading.Event()
    socket_path = repo / config.socket_path

    def handler(message: dict) -> dict:
        request_db = connect(repo)
        try:
            initialize_schema(request_db)
            return handle_session_message(repo, request_db, config, message)
        except Exception as exc:
            message_type = message.get("type")
            if message_type != "heartbeat":
                _daemon_log(
                    "request failed",
                    type=message_type,
                    session=message.get("session"),
                    error=str(exc) or exc.__class__.__name__,
                )
            raise
        finally:
            request_db.close()

    _ = db
    thread = serve_forever(socket_path, handler, stop_event=stop_event)
    thread.start()
    _daemon_log("socket listening", socket=socket_path)
    return stop_event


def run_daemon(repo: Path, db: sqlite3.Connection, config: CocodexConfig) -> int:
    ensure_cocodex_excluded(repo)
    installed_hooks = install_main_guard(repo, main_branch=config.main_branch)
    _daemon_log(
        "daemon starting",
        repo=repo,
        main=config.main_branch,
        remote=config.remote or "none",
        interval_s=config.dirty_interval_s,
    )
    _daemon_log("main guard checked", hooks=",".join(installed_hooks) or "none")
    last_event_id = _latest_event_id(db)
    recover_incomplete_sessions(repo, db)
    last_event_id = _emit_new_events(db, last_event_id)
    stop_event = start_socket_server(repo, db, config)
    _daemon_log("daemon ready", socket=repo / config.socket_path)
    try:
        while True:
            detect_disconnected_sessions(db)
            detect_external_main_update(repo, db, config)
            process_queue_once(repo, db, config)
            last_event_id = _emit_new_events(db, last_event_id)
            time.sleep(config.dirty_interval_s)
    finally:
        _daemon_log("daemon stopping")
        stop_event.set()
