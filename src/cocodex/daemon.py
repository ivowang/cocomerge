from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .config import CocodexConfig
from .git import (
    add_all,
    commit,
    current_head,
    diff,
    fast_forward_ref,
    has_unsafe_git_state,
    is_dirty,
    merge_base_is_ancestor,
    reset_hard,
    try_force_push_session_refs,
    update_ref,
)
from .state import (
    SessionRecord,
    claim_integration_task,
    connect,
    dequeue_session,
    enqueue_session,
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
from .tasks import IntegrationTask, create_task_id, validate_task_report, write_task_file
from .protocol import decode_message
from .transport import send_message, serve_forever


READY_TO_INTEGRATE_STATES = {"clean", "dirty", "queued"}
INCOMPLETE_INTEGRATION_STATES = {"frozen", "snapshot", "fusing", "verifying", "publishing"}
EXTERNAL_MAIN_RECOVERY_STATES = {"dirty", "queued"} | INCOMPLETE_INTEGRATION_STATES

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
    lock = get_lock(db)
    for session in list_sessions(db):
        if not session.connected or session.last_heartbeat is None:
            continue
        heartbeat_age = now_value - session.last_heartbeat
        if heartbeat_age <= timeout:
            continue

        reason = f"heartbeat timeout after {heartbeat_age:.1f}s"
        mark_session_disconnected(db, session.name, reason)
        if lock is not None and lock["owner"] == session.name:
            task_id = session.active_task or lock["task_id"]
            dequeue_session(db, session.name)
            transition_session(
                db,
                session.name,
                "recovery_required",
                reason=reason,
                active_task=task_id,
                blocked_reason=reason,
            )


def recover_incomplete_sessions(db: sqlite3.Connection) -> None:
    lock = get_lock(db)
    recovered: set[str] = set()
    if lock is not None:
        owner = get_session(db, lock["owner"])
        if owner is not None and (
            owner.active_task != lock["task_id"]
            or owner.state == "queued"
        ):
            reason = "startup recovery from inconsistent integration lock"
            dequeue_session(db, owner.name)
            transition_session(
                db,
                owner.name,
                "recovery_required",
                reason=reason,
                active_task=lock["task_id"],
                blocked_reason=reason,
            )
            recovered.add(owner.name)
    for session in list_sessions(db):
        if session.name in recovered:
            continue
        if session.state not in INCOMPLETE_INTEGRATION_STATES:
            continue

        task_id = session.active_task
        if lock is not None and lock["owner"] == session.name:
            task_id = task_id or lock["task_id"]
        reason = f"startup recovery from {session.state}"
        dequeue_session(db, session.name)
        transition_session(
            db,
            session.name,
            "recovery_required",
            reason=reason,
            active_task=task_id,
            blocked_reason=reason,
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

    reason = f"main moved externally from {observed} to {current}"
    for session in list_sessions(db):
        if (
            session.state not in EXTERNAL_MAIN_RECOVERY_STATES
            and session.active_task is None
        ):
            continue
        dequeue_session(db, session.name)
        transition_session(
            db,
            session.name,
            "recovery_required",
            reason=reason,
            active_task=session.active_task,
            blocked_reason=reason,
        )
    record_event(db, "external_main_updated", {"previous": observed, "current": current})
    set_metadata(db, "last_observed_main", current)
    return True


def _is_locked_pending_publish_recovery(db: sqlite3.Connection, current_main: str) -> bool:
    lock = get_lock(db)
    if lock is None:
        return False
    session = get_session(db, lock["owner"])
    pending_publish_reason = session is not None and session.blocked_reason in {
        "startup recovery from publishing",
    }
    if (
        session is None
        or session.state != "recovery_required"
        or session.active_task != lock["task_id"]
        or not session.blocked_reason
        or (
            not session.blocked_reason.startswith("remote push failed")
            and not pending_publish_reason
        )
    ):
        return False
    try:
        return current_head(Path(session.worktree)) == current_main
    except Exception:
        return False


def _session_has_changes(session: SessionRecord) -> bool:
    worktree = Path(session.worktree)
    if is_dirty(worktree):
        return True
    if session.last_seen_main is None:
        return False
    return current_head(worktree) != session.last_seen_main


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

    worktree = Path(session.worktree)
    unsafe = has_unsafe_git_state(worktree)
    if unsafe:
        transition_session(
            db,
            session.name,
            "blocked",
            reason=f"unsafe Git state: {unsafe}",
            active_task=None,
            blocked_reason=unsafe,
        )
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
                    "queued",
                    reason="main advanced before direct publish",
                    active_task=None,
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
                    "queued",
                    reason="main advanced before direct publish",
                    active_task=None,
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
                "blocked",
                reason=reason,
                active_task=None,
                blocked_reason=reason,
            )
            raise RuntimeError(reason) from exc
    finally:
        _release_lock_if_owned(db, session.name, task_id)

    remote_error = try_force_push_session_refs(
        repo,
        config.remote,
        main_branch=config.main_branch,
        session_branch=session.branch,
    )
    if remote_error is not None:
        record_event(
            db,
            "remote_sync_failed",
            {"session": session.name, "task_id": task_id, "error": remote_error},
        )
    return f"published directly to {candidate}"


def prepare_integration(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session_name: str,
    *,
    task_id: str | None = None,
    lock_already_held: bool = False,
) -> Path:
    session = get_session(db, session_name)
    if session is None:
        raise RuntimeError(f"unknown session: {session_name}")
    worktree = Path(session.worktree)
    unsafe = has_unsafe_git_state(worktree)
    if unsafe:
        transition_session(db, session_name, "blocked", reason=unsafe, blocked_reason=unsafe)
        raise RuntimeError(f"unsafe Git state: {unsafe}")

    task_id = task_id or create_task_id(session_name)
    lock_acquired = False
    no_changes = False
    if lock_already_held:
        if get_lock(db) != {"owner": session_name, "task_id": task_id}:
            raise RuntimeError("integration lock is not held by this task")
    else:
        if get_lock(db) is not None:
            raise RuntimeError("integration lock is already held")
        set_lock(db, owner=session_name, task_id=task_id)
        lock_acquired = True
    try:
        transition_session(db, session_name, "snapshot", reason="creating snapshot", active_task=task_id)
        latest_main = current_head(repo, config.main_branch)
        base = session.last_seen_main or latest_main
        head = current_head(worktree)
        if is_dirty(worktree):
            add_all(worktree)
            snapshot = commit(worktree, f"cocodex snapshot: {session_name} {task_id}")
        elif head != base:
            snapshot = head
        else:
            no_changes = True
            raise RuntimeError("no changes to snapshot")

        update_ref(worktree, f"refs/cocodex/snapshots/{task_id}", snapshot)
        update_ref(worktree, f"refs/cocodex/bases/{task_id}", latest_main)
        diff_summary = diff(worktree, base, snapshot)
        reset_hard(worktree, latest_main)
        task = IntegrationTask(
            task_id=task_id,
            session=session_name,
            latest_main=latest_main,
            last_seen_main=session.last_seen_main,
            snapshot_commit=snapshot,
            diff_summary=diff_summary,
        )
        task_path = write_task_file(repo, task)
        dequeue_session(db, session_name)
        transition_session(db, session_name, "fusing", reason="task started", active_task=task_id)
        return task_path
    except Exception as exc:
        if lock_acquired:
            set_lock(db, owner=None, task_id=None)
        reason = str(exc) or exc.__class__.__name__
        if no_changes:
            transition_session(
                db,
                session_name,
                "blocked",
                reason=reason,
                blocked_reason=reason,
            )
        else:
            transition_session(
                db,
                session_name,
                "recovery_required",
                reason=reason,
                blocked_reason=reason,
            )
        raise


def send_control_message(session: SessionRecord, message: dict) -> dict:
    if not session.control_socket:
        raise RuntimeError(f"session has no control socket: {session.name}")
    raw = send_message(Path(session.control_socket), message, timeout=5)
    return decode_message(raw)


def process_queue_once(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    *,
    send_control: ControlSender = send_control_message,
    task_id_factory: TaskIdFactory = create_task_id,
) -> bool:
    if get_lock(db) is not None:
        return False

    for session_name in list_queue(db):
        session = get_session(db, session_name)
        if session is None:
            dequeue_session(db, session_name)
            continue
        if session.state not in {"dirty", "queued"}:
            dequeue_session(db, session_name)
            continue
        if not session.connected or not session.control_socket:
            continue
        if session.active_task is not None:
            continue

        try:
            direct_message = publish_without_fusion_if_current(
                repo,
                db,
                config,
                session,
                task_id_factory=task_id_factory,
            )
        except Exception:
            dequeue_session(db, session.name)
            return False
        if direct_message is not None:
            dequeue_session(db, session.name)
            _mark_waiting_sessions_queued(db, exclude=session.name)
            return True

        task_id = task_id_factory(session.name)
        claim_integration_task(db, session.name, task_id, reason="freeze requested")
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
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            _stop_queue_attempt(
                db,
                session.name,
                task_id,
                state="queued",
                reason=reason,
                active_task=None,
            )
            return False
        if not _control_response_matches(
            freeze,
            expected_type="freeze_ack",
            session_name=session.name,
            task_id=task_id,
        ):
            reason = freeze.get("message") or freeze.get("reason") or "freeze failed"
            _stop_queue_attempt(
                db,
                session.name,
                task_id,
                state="queued",
                reason=reason,
                active_task=None,
            )
            return False

        transition_session(db, session.name, "frozen", reason="freeze acknowledged", active_task=task_id)
        try:
            task_path = prepare_integration(
                repo,
                db,
                config,
                session.name,
                task_id=task_id,
                lock_already_held=True,
            )
        except Exception:
            _release_lock_if_owned(db, session.name, task_id)
            return False
        refreshed = get_session(db, session.name)
        if refreshed is None:
            raise RuntimeError(f"unknown session after prepare: {session.name}")
        try:
            response = send_control(
                refreshed,
                {
                    "type": "start_fusion",
                    "session": session.name,
                    "task_id": task_id,
                    "task_file": str(task_path),
                },
            )
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            _stop_queue_attempt(
                db,
                session.name,
                task_id,
                state="recovery_required",
                reason=reason,
                active_task=task_id,
            )
            return False
        if not _control_response_matches(
            response,
            expected_type="ack",
            session_name=session.name,
            task_id=task_id,
        ):
            reason = response.get("message") or response.get("reason") or "start_fusion failed"
            _stop_queue_attempt(
                db,
                session.name,
                task_id,
                state="recovery_required",
                reason=reason,
                active_task=task_id,
            )
            return False
        _mark_waiting_sessions_queued(db, exclude=session.name)
        return True

    return False


def _mark_waiting_sessions_queued(db: sqlite3.Connection, *, exclude: str) -> None:
    for session_name in list_queue(db):
        if session_name == exclude:
            continue
        session = get_session(db, session_name)
        if session is None or session.active_task is not None or session.state != "dirty":
            continue
        transition_session(db, session.name, "queued", reason="waiting for integration")


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
    if session.state not in {"fusing", "blocked", "verifying", "publishing", "recovery_required"}:
        raise RuntimeError(f"invalid session state for publish: {session.state}")
    if session.state == "recovery_required" and not _is_retryable_publish_recovery(session):
        reason = session.blocked_reason or "recovery required"
        raise RuntimeError(
            f"Cannot continue sync from recovery_required: {reason}. "
            "An operator must inspect the task before retrying or abandoning it."
        )

    worktree = Path(session.worktree)
    try:
        unsafe = has_unsafe_git_state(worktree)
    except Exception as exc:
        reason = f"worktree unavailable: {exc}"
        transition_session(
            db,
            session_name,
            "recovery_required",
            reason=reason,
            active_task=task_id,
            blocked_reason=reason,
        )
        _release_lock_if_owned(db, session_name, task_id)
        raise RuntimeError(reason) from exc

    if unsafe:
        transition_session(
            db,
            session_name,
            "blocked",
            reason=f"unsafe Git state: {unsafe}",
            active_task=task_id,
            blocked_reason=unsafe,
        )
        raise RuntimeError(f"unsafe Git state: {unsafe}")

    try:
        session_head = current_head(worktree)
    except Exception as exc:
        reason = f"worktree unavailable: {exc}"
        transition_session(
            db,
            session_name,
            "recovery_required",
            reason=reason,
            active_task=task_id,
            blocked_reason=reason,
        )
        _release_lock_if_owned(db, session_name, task_id)
        raise RuntimeError(reason) from exc

    if candidate != session_head:
        reason = "candidate is not current session head"
        transition_session(
            db,
            session_name,
            "recovery_required",
            reason=reason,
            active_task=task_id,
            blocked_reason=f"{reason}: {candidate} != {session_head}",
        )
        _release_lock_if_owned(db, session_name, task_id)
        raise RuntimeError(reason)

    base = _task_base(worktree, task_id)
    if base is not None and candidate == base:
        _stop_publish(
            db,
            session_name,
            task_id,
            state="blocked",
            reason=(
                "sync task has no candidate commit; implement the snapshot feature "
                "or create an explicit no-op commit before running sync again"
            ),
            release_lock=False,
        )
        return

    try:
        if is_dirty(worktree):
            reason = "worktree is dirty before validation"
            _stop_publish(
                db,
                session_name,
                task_id,
                state="blocked",
                reason=reason,
                release_lock=False,
            )
            return
    except Exception as exc:
        reason = f"worktree unavailable: {exc}"
        _stop_publish(
            db,
            session_name,
            task_id,
            state="recovery_required",
            reason=reason,
        )
        raise RuntimeError(reason) from exc

    transition_session(db, session_name, "verifying", reason="candidate reported", active_task=task_id)
    validation_error = validate_task_report(repo, task_id)
    if validation_error is not None:
        transition_session(
            db,
            session_name,
            "blocked",
            reason="validation report required",
            active_task=task_id,
            blocked_reason=validation_error,
        )
        return

    try:
        verified_head = current_head(worktree)
        verified_dirty = is_dirty(worktree)
    except Exception as exc:
        reason = f"worktree unavailable after validation: {exc}"
        _stop_publish(
            db,
            session_name,
            task_id,
            state="recovery_required",
            reason=reason,
        )
        raise RuntimeError(reason) from exc

    if verified_head != candidate:
        reason = "candidate changed during validation"
        _stop_publish(
            db,
            session_name,
            task_id,
            state="recovery_required",
            reason=reason,
            blocked_reason=f"{reason}: {candidate} != {verified_head}",
        )
        raise RuntimeError(reason)

    if verified_dirty:
        reason = "worktree changed during validation"
        _stop_publish(
            db,
            session_name,
            task_id,
            state="blocked",
            reason=reason,
            release_lock=False,
        )
        return

    transition_session(db, session_name, "publishing", reason="validation report accepted", active_task=task_id)
    try:
        fast_forward_ref(repo, config.main_branch, candidate)
        set_metadata(db, "last_observed_main", candidate)
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        transition_session(
            db,
            session_name,
            "recovery_required",
            reason=reason,
            active_task=task_id,
            blocked_reason=reason,
        )
        _release_lock_if_owned(db, session_name, task_id)
        raise

    transition_session(db, session_name, "clean", reason="published", active_task=None)
    update_last_seen_main(db, session_name, candidate)
    _release_lock_if_owned(db, session_name, task_id)
    remote_error = try_force_push_session_refs(
        repo,
        config.remote,
        main_branch=config.main_branch,
        session_branch=session.branch,
    )
    if remote_error is not None:
        record_event(
            db,
            "remote_sync_failed",
            {"session": session_name, "task_id": task_id, "error": remote_error},
        )


def _stop_publish(
    db: sqlite3.Connection,
    session_name: str,
    task_id: str,
    *,
    state: str,
    reason: str,
    blocked_reason: str | None = None,
    release_lock: bool = True,
) -> None:
    transition_session(
        db,
        session_name,
        state,
        reason=reason,
        active_task=task_id,
        blocked_reason=blocked_reason or reason,
    )
    if release_lock:
        _release_lock_if_owned(db, session_name, task_id)


def _task_base(worktree: Path, task_id: str) -> str | None:
    try:
        return current_head(worktree, f"refs/cocodex/bases/{task_id}")
    except Exception:
        return None


def _is_retryable_publish_recovery(session: SessionRecord) -> bool:
    reason = session.blocked_reason or ""
    return reason.startswith("remote push failed") or reason == "startup recovery from publishing"


def _stop_queue_attempt(
    db: sqlite3.Connection,
    session_name: str,
    task_id: str,
    *,
    state: str,
    reason: str,
    active_task: str | None,
) -> None:
    transition_session(
        db,
        session_name,
        state,
        reason=reason,
        active_task=active_task,
        blocked_reason=reason,
    )
    _release_lock_if_owned(db, session_name, task_id)


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
) -> dict:
    message_type = message.get("type")
    session_name = message.get("session")

    if message_type == "register":
        branch = message.get("branch") or f"cocodex/{session_name}"
        worktree = message.get("worktree") or str(repo / config.worktree_root / session_name)
        existing = get_session(db, session_name)
        if existing is not None:
            if existing.branch != branch or existing.worktree != worktree:
                raise RuntimeError(f"conflicting registration for session: {session_name}")
            update_session_runtime(
                db,
                session_name,
                pid=message.get("pid"),
                control_socket=message.get("control_socket"),
                connected=True,
                heartbeat=now(),
            )
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
        register_session(db, record)
        update_session_runtime(
            db,
            session_name,
            pid=message.get("pid"),
            control_socket=message.get("control_socket"),
            connected=True,
            heartbeat=now(),
        )
        return {"type": "registered", "session": session_name}

    if message_type == "heartbeat":
        if get_session(db, session_name) is None:
            raise RuntimeError(f"unknown session: {session_name}")
        touch_session_heartbeat(db, session_name, now())
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
        if session.state not in READY_TO_INTEGRATE_STATES:
            raise RuntimeError(f"invalid session state for ready_to_integrate: {session.state}")
        if session.active_task is not None:
            raise RuntimeError(f"session has active task: {session_name}")
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
        transition_session(db, session_name, "queued", reason="sync requested", active_task=None)
        enqueue_session(db, session_name)
        return {"type": "queued", "session": session_name}

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
        candidate = current_head(Path(session.worktree))
        publish_candidate(repo, db, config, session_name, task_id, candidate)
        updated = get_session(db, session_name)
        if updated is not None and updated.state == "blocked":
            return {
                "type": "blocked",
                "session": session_name,
                "task_id": task_id,
                "reason": updated.blocked_reason or "blocked",
            }
        return {"type": "ack", "session": session_name, "task_id": task_id}

    return {"type": "ack", "session": session_name}


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
    _daemon_log(
        "daemon starting",
        repo=repo,
        main=config.main_branch,
        remote=config.remote or "none",
        interval_s=config.dirty_interval_s,
    )
    last_event_id = _latest_event_id(db)
    recover_incomplete_sessions(db)
    last_event_id = _emit_new_events(db, last_event_id)
    stop_event = start_socket_server(repo, db, config)
    _daemon_log("daemon ready", socket=repo / config.socket_path)
    try:
        while True:
            detect_disconnected_sessions(db)
            if not detect_external_main_update(repo, db, config):
                process_queue_once(repo, db, config)
            last_event_id = _emit_new_events(db, last_event_id)
            time.sleep(config.dirty_interval_s)
    finally:
        _daemon_log("daemon stopping")
        stop_event.set()
