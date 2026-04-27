from __future__ import annotations

import re
import sqlite3
import textwrap
from pathlib import Path

from .config import CocomergeConfig
from .git import GitError, create_worktree, current_head, fast_forward_ref, is_dirty, run_git
from .protocol import ProtocolError, decode_message
from .state import (
    SessionRecord,
    get_lock,
    get_session,
    list_sessions,
    register_session,
    transition_session,
    update_last_seen_main,
)
from .tasks import task_file_path, validation_file_path
from .transport import send_message


SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
COCOMERGE_AGENTS_MARKER = "<!-- cocomerge-managed-session-agents -->"
COCOMERGE_AGENTS_FILE = "AGENTS.md"
REJOIN_RECOVERABLE_PREFIXES = (
    "heartbeat timeout",
    "startup recovery from fusing",
    "startup recovery from verifying",
)


def ensure_session_worktree(
    repo: Path,
    config: CocomergeConfig,
    db: sqlite3.Connection,
    session: str,
    *,
    git_user_name: str | None = None,
    git_user_email: str | None = None,
) -> SessionRecord:
    validate_session_name(session)
    branch = f"cocomerge/{session}"
    worktree = repo / config.worktree_root / session
    create_worktree(repo, branch=branch, worktree=worktree, start_point=config.main_branch)
    _validate_worktree(worktree, branch)
    _configure_worktree_identity(
        worktree,
        git_user_name=git_user_name,
        git_user_email=git_user_email,
    )
    _ensure_session_agents_file(worktree, session=session, branch=branch, config=config)

    existing = get_session(db, session)
    if existing is not None:
        if existing.branch != branch:
            raise ValueError(
                f"Existing session {session!r} uses branch {existing.branch!r}, expected {branch!r}"
            )
        if existing.worktree != str(worktree):
            raise ValueError(
                f"Existing session {session!r} uses worktree {existing.worktree!r}, "
                f"expected {str(worktree)!r}"
            )
        return existing

    record = SessionRecord(
        name=session,
        branch=branch,
        worktree=str(worktree),
        state="clean",
        last_seen_main=current_head(repo, config.main_branch),
        active_task=None,
        blocked_reason=None,
    )
    register_session(db, record)
    return record


def validate_session_name(session: str) -> None:
    if not SESSION_NAME_RE.fullmatch(session):
        raise ValueError(
            "Invalid session name: use letters, digits, underscores, or hyphens, "
            "and start with a letter or digit"
        )


def _validate_worktree(worktree: Path, branch: str) -> None:
    try:
        top_level = Path(run_git(worktree, ["rev-parse", "--show-toplevel"])).resolve()
        actual_branch = run_git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
    except GitError as exc:
        raise RuntimeError(f"{worktree} is not a Git worktree") from exc
    if top_level != worktree.resolve():
        raise RuntimeError(f"{worktree} is not a Git worktree")
    if actual_branch != branch:
        raise RuntimeError(
            f"{worktree} is on branch {actual_branch!r}, expected {branch!r}"
        )


def _configure_worktree_identity(
    worktree: Path,
    *,
    git_user_name: str | None,
    git_user_email: str | None,
) -> None:
    if (git_user_name is None) != (git_user_email is None):
        raise ValueError("--git-user-name and --git-user-email must be provided together")
    if git_user_name is not None and git_user_email is not None:
        run_git(worktree, ["config", "extensions.worktreeConfig", "true"])
        run_git(worktree, ["config", "--worktree", "user.name", git_user_name])
        run_git(worktree, ["config", "--worktree", "user.email", git_user_email])

    missing = [
        key
        for key in ["user.name", "user.email"]
        if not run_git(worktree, ["config", "--get", key], check=False)
    ]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(
            f"Git identity is not configured for this session ({names}). "
            "Configure this developer in .cocomerge/config.json before running cocomerge join."
        )


def _ensure_session_agents_file(
    worktree: Path,
    *,
    session: str,
    branch: str,
    config: CocomergeConfig,
) -> None:
    agents_path = worktree / COCOMERGE_AGENTS_FILE
    if agents_path.exists():
        existing = agents_path.read_text(encoding="utf-8", errors="replace")
        if COCOMERGE_AGENTS_MARKER not in existing:
            return

    _ensure_agents_file_is_ignored(worktree)
    agents_path.write_text(
        _session_agents_content(session=session, branch=branch, config=config),
        encoding="utf-8",
    )


def _ensure_agents_file_is_ignored(worktree: Path) -> None:
    common_dir_raw = Path(run_git(worktree, ["rev-parse", "--git-common-dir"]))
    common_dir = common_dir_raw if common_dir_raw.is_absolute() else worktree / common_dir_raw
    exclude_path = common_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    pattern = f"/{COCOMERGE_AGENTS_FILE}"
    if pattern in {line.strip() for line in existing.splitlines()}:
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    exclude_path.write_text(
        existing
        + separator
        + "# Cocomerge managed session instruction file\n"
        + f"{pattern}\n",
        encoding="utf-8",
    )


def _session_agents_content(*, session: str, branch: str, config: CocomergeConfig) -> str:
    remote = config.remote or "no remote configured"
    return "\n".join(
        [
            COCOMERGE_AGENTS_MARKER,
            "# Cocomerge Session Instructions",
            "",
            f"You are working in Cocomerge session `{session}`.",
            f"The repository main branch is `{config.main_branch}`.",
            f"The configured remote is `{remote}`.",
            "",
            "Cocomerge coordinates this repository's multi-Codex collaboration.",
            "Do not run `git pull`, `git merge`, or `git push` against the main branch directly.",
            "Do not publish `main` yourself. Cocomerge is the only writer to local `main`.",
            "If a remote is configured, `cocomerge sync` best-effort force-syncs server branch refs to that remote.",
            "There is no fixed project-wide test command. For each sync task, design",
            "and run sufficient validation for the semantic merge, then write the",
            "validation report requested by the task file before running sync again.",
            "When this Codex session starts or restarts, handle any Cocomerge restart",
            "notice before accepting or continuing unrelated feature work.",
            "",
            "During normal collaboration, use one Cocomerge command:",
            "",
            "    cocomerge sync",
            "",
            "When you have local work, sync requests an integration task. Wait for Cocomerge",
            "to print a task file path in this Codex terminal. Read that task file, treat",
            "the current worktree as latest `main`, and re-implement or semantically merge",
            "the snapshot described by the task on top of latest `main`.",
            "If the task interrupts another request, pause at a safe point, preserve",
            "the remaining intent, finish the sync task, then resume the paused work.",
            "",
            "After committing the final candidate and making sure the worktree is clean, run sync again:",
            "",
            "    cocomerge sync",
            "",
            "If the integration cannot be completed safely, stop and explain the blocker",
            "in your session output. Do not run sync again until the candidate is ready.",
            "",
            "This file is generated by Cocomerge for this managed worktree and is ignored by Git.",
            "",
        ]
    )


def prepare_join_startup_notice(
    repo: Path,
    config: CocomergeConfig,
    db: sqlite3.Connection,
    session: SessionRecord,
) -> tuple[SessionRecord, str | None]:
    refreshed = get_session(db, session.name)
    if refreshed is None:
        return session, None

    if refreshed.active_task:
        refreshed = _recover_rejoinable_task(repo, db, refreshed)
        return refreshed, _active_task_notice(repo, refreshed)

    if refreshed.state == "queued":
        return refreshed, _queued_notice(refreshed)

    caught_up = _catch_up_clean_join(repo, config, db, refreshed)
    if caught_up is not None:
        return caught_up, _caught_up_notice(config, caught_up)

    if _has_unintegrated_work(refreshed):
        return refreshed, _local_work_notice(refreshed)

    return refreshed, None


def _recover_rejoinable_task(
    repo: Path,
    db: sqlite3.Connection,
    session: SessionRecord,
) -> SessionRecord:
    if session.state != "recovery_required" or session.active_task is None:
        return session
    lock = get_lock(db)
    task_path = task_file_path(repo, session.active_task)
    reason = session.blocked_reason or ""
    if (
        lock == {"owner": session.name, "task_id": session.active_task}
        and task_path.exists()
        and reason.startswith(REJOIN_RECOVERABLE_PREFIXES)
    ):
        transition_session(
            db,
            session.name,
            "fusing",
            reason="session rejoined active task",
            active_task=session.active_task,
        )
        recovered = get_session(db, session.name)
        return recovered or session
    return session


def _active_task_notice(repo: Path, session: SessionRecord) -> str:
    task_id = session.active_task
    if task_id is None:
        return ""
    task_path = task_file_path(repo, task_id)
    validation_path = validation_file_path(repo, task_id)
    reason = session.blocked_reason or "none"
    if not task_path.exists():
        return textwrap.dedent(
            f"""
            Cocomerge restart notice: this session references a missing sync task file.

            Session: {session.name}
            State: {session.state}
            Task file: {task_path}
            Reason: {reason}

            Do not begin new feature work yet. Run `cocomerge status` and
            `cocomerge log`; an operator should inspect or abandon this task.
            """
        ).lstrip()
    if session.state == "recovery_required":
        body = [
            "Cocomerge restart notice: this session has an active sync task in recovery.",
            "",
            f"Session: {session.name}",
            f"State: {session.state}",
            f"Task file: {task_path}",
            f"Validation file: {validation_path}",
            f"Reason: {reason}",
            "",
            "Do not begin new feature work yet. This state may need operator inspection.",
            "Run `cocomerge status` and `cocomerge log`, then either recover this task",
            "or have an operator abandon it.",
            "",
        ]
        return "\n".join(body)

    body = [
        "Cocomerge restart notice: unfinished sync task must be handled first.",
        "",
        f"Session: {session.name}",
        f"State: {session.state}",
        f"Task file: {task_path}",
        f"Validation file: {validation_path}",
    ]
    if session.blocked_reason:
        body.append(f"Blocked reason: {session.blocked_reason}")
    body.extend(
        [
            "",
            "Read the task file now. Treat the current worktree as the latest main branch.",
            "Finish the semantic merge before starting new feature work. If the candidate",
            "is already committed, make sure the validation report exists and run:",
            "",
            "    cocomerge sync",
            "",
        ]
    )
    return "\n".join(body)


def _queued_notice(session: SessionRecord) -> str:
    return textwrap.dedent(
        f"""
        Cocomerge restart notice: this session already has a sync request queued.

        Session: {session.name}
        State: queued

        Do not start unrelated feature work yet. Keep this Codex session running
        and wait for Cocomerge to deliver the sync task. If nothing happens, run
        `cocomerge status` to check whether the daemon and integration lock are healthy.
        """
    ).lstrip()


def _catch_up_clean_join(
    repo: Path,
    config: CocomergeConfig,
    db: sqlite3.Connection,
    session: SessionRecord,
) -> SessionRecord | None:
    if session.state != "clean" or session.last_seen_main is None:
        return None
    worktree = Path(session.worktree)
    if is_dirty(worktree):
        return None
    head = current_head(worktree)
    latest_main = current_head(repo, config.main_branch)
    if head != session.last_seen_main or head == latest_main:
        return None
    fast_forward_ref(worktree, session.branch, latest_main)
    update_last_seen_main(db, session.name, latest_main)
    return get_session(db, session.name) or session


def _caught_up_notice(config: CocomergeConfig, session: SessionRecord) -> str:
    return textwrap.dedent(
        f"""
        Cocomerge restart notice: this session was safely caught up to latest `{config.main_branch}`.

        Session: {session.name}

        You can continue normal development from the managed worktree.
        """
    ).lstrip()


def _has_unintegrated_work(session: SessionRecord) -> bool:
    worktree = Path(session.worktree)
    if is_dirty(worktree):
        return True
    if session.last_seen_main is None:
        return False
    return current_head(worktree) != session.last_seen_main


def _local_work_notice(session: SessionRecord) -> str:
    return textwrap.dedent(
        f"""
        Cocomerge restart notice: this session has local work that is not integrated into main.

        Session: {session.name}
        Worktree: {session.worktree}

        Before starting unrelated new work, review the current changes. When the
        feature is ready to integrate, run:

            cocomerge sync
        """
    ).lstrip()


def register_with_daemon(
    socket_path: Path,
    record: SessionRecord,
    pid: int,
    control_socket: str | None = None,
    *,
    timeout: float | None = 0.5,
) -> dict | None:
    if not socket_path.exists():
        return None
    message = {
        "type": "register",
        "session": record.name,
        "pid": pid,
        "branch": record.branch,
        "worktree": record.worktree,
    }
    if control_socket is not None:
        message["control_socket"] = control_socket
    try:
        raw = send_message(
            socket_path,
            message,
            timeout=timeout,
        )
        return decode_message(raw)
    except (OSError, TimeoutError, ProtocolError):
        return None


def infer_session_from_cwd(db: sqlite3.Connection, cwd: Path | None = None) -> SessionRecord:
    current = cwd or Path.cwd()
    try:
        worktree = Path(run_git(current, ["rev-parse", "--show-toplevel"])).resolve()
    except GitError as exc:
        raise RuntimeError("cocomerge sync must run inside a Git worktree") from exc

    matches = [
        session
        for session in list_sessions(db)
        if Path(session.worktree).resolve() == worktree
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(
            "Cannot infer Cocomerge session from this directory. "
            "Run cocomerge sync inside a managed worktree, or pass a session name from the main repository."
        )
    raise RuntimeError(f"Multiple Cocomerge sessions match this worktree: {worktree}")


def send_completion(
    socket_path: Path,
    session: SessionRecord,
    *,
    blocked_reason: str | None = None,
) -> dict:
    if session.active_task is None:
        raise RuntimeError(f"Session {session.name} has no active task")
    message = {
        "type": "fusion_blocked" if blocked_reason else "fusion_done",
        "session": session.name,
        "task_id": session.active_task,
    }
    if blocked_reason:
        message["reason"] = blocked_reason
    raw = send_message(socket_path, message, timeout=5)
    return decode_message(raw)
