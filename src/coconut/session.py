from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

from .config import CoconutConfig
from .git import GitError, create_worktree, current_head, run_git
from .protocol import ProtocolError, decode_message
from .state import SessionRecord, get_session, list_sessions, register_session
from .transport import send_message


SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
COCONUT_AGENTS_MARKER = "<!-- coconut-managed-session-agents -->"
COCONUT_AGENTS_FILE = "AGENTS.md"


def ensure_session_worktree(
    repo: Path,
    config: CoconutConfig,
    db: sqlite3.Connection,
    session: str,
    *,
    git_user_name: str | None = None,
    git_user_email: str | None = None,
) -> SessionRecord:
    validate_session_name(session)
    branch = f"coconut/{session}"
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
            "Run coconut join with --git-user-name and --git-user-email."
        )


def _ensure_session_agents_file(
    worktree: Path,
    *,
    session: str,
    branch: str,
    config: CoconutConfig,
) -> None:
    agents_path = worktree / COCONUT_AGENTS_FILE
    if agents_path.exists():
        existing = agents_path.read_text(encoding="utf-8", errors="replace")
        if COCONUT_AGENTS_MARKER not in existing:
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
    pattern = f"/{COCONUT_AGENTS_FILE}"
    if pattern in {line.strip() for line in existing.splitlines()}:
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    exclude_path.write_text(
        existing
        + separator
        + "# Coconut managed session instruction file\n"
        + f"{pattern}\n",
        encoding="utf-8",
    )


def _session_agents_content(*, session: str, branch: str, config: CoconutConfig) -> str:
    verify = config.verify or "no verification command configured"
    remote = config.remote or "no remote configured"
    return "\n".join(
        [
            COCONUT_AGENTS_MARKER,
            "# Coconut Session Instructions",
            "",
            f"You are working in Coconut session `{session}`.",
            f"The repository main branch is `{config.main_branch}`.",
            f"The verification command is `{verify}`.",
            f"The configured remote is `{remote}`.",
            "",
            "Coconut coordinates this repository's multi-Codex collaboration.",
            "Do not run `git pull`, `git merge`, or `git push` against the main branch directly.",
            "Do not publish `main` yourself. Coconut is the only writer to local `main`.",
            "",
            "During normal collaboration, use one Coconut command:",
            "",
            "    coconut sync",
            "",
            "When you have local work, sync requests an integration task. Wait for Coconut",
            "to print a task file path in this Codex terminal. Read that task file, treat",
            "the current worktree as latest `main`, and re-implement or semantically merge",
            "the snapshot described by the task on top of latest `main`.",
            "",
            "After committing the final candidate and making sure the worktree is clean, run sync again:",
            "",
            "    coconut sync",
            "",
            "If the integration cannot be completed safely, stop and explain the blocker",
            "in your session output. Do not run sync again until the candidate is ready.",
            "",
            "This file is generated by Coconut for this managed worktree and is ignored by Git.",
            "",
        ]
    )


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
        raise RuntimeError("coconut sync must run inside a Git worktree") from exc

    matches = [
        session
        for session in list_sessions(db)
        if Path(session.worktree).resolve() == worktree
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(
            "Cannot infer Coconut session from this directory. "
            "Run coconut sync inside a managed worktree, or pass a session name from the main repository."
        )
    raise RuntimeError(f"Multiple Coconut sessions match this worktree: {worktree}")


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


def run_session_command(worktree: Path, command: list[str]) -> int:
    if not command:
        raise ValueError("join requires a command after --")
    return subprocess.call(command, cwd=worktree)
