from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CocodexConfig
from .git import (
    GitError,
    branch_exists,
    current_head,
    has_unsafe_git_state,
    is_dirty,
    run_git,
    try_sync_deleted_session_refs,
    update_ref,
)
from .session import validate_session_name
from .state import (
    SessionRecord,
    claim_session_deletion,
    delete_session_record,
    get_lock,
    get_session,
    mark_session_disconnected,
    transition_session,
)


@dataclass(frozen=True)
class DeleteResult:
    session: str
    worktree: Path
    branch: str
    manifest: Path
    backup_refs: list[str]
    worktree_removed: bool
    branch_deleted: bool
    session_record_removed: bool
    remote_warning: str | None


class DeletePartialError(RuntimeError):
    pass


def delete_session(
    repo: Path,
    db: sqlite3.Connection,
    config: CocodexConfig,
    session_name: str,
) -> DeleteResult:
    validate_session_name(session_name)
    session = get_session(db, session_name)
    expected_branch = f"cocodex/{session_name}"
    expected_worktree = repo / config.worktree_root / session_name
    branch = session.branch if session is not None else expected_branch
    worktree = Path(session.worktree) if session is not None else expected_worktree
    if not worktree.is_absolute():
        worktree = repo / worktree

    _validate_managed_targets(repo, config, session_name, branch, worktree)
    session = _refresh_stale_runtime(db, session)
    _refuse_unsafe_delete(db, session_name, session)

    resource_exists = bool(
        session is not None or worktree.exists() or branch_exists(repo, branch)
    )
    if not resource_exists:
        raise RuntimeError(
            f"session {session_name!r} is not registered and has no managed "
            f"worktree or branch to delete"
        )

    worktree_is_present = worktree.exists()
    if worktree_is_present:
        _validate_worktree_for_delete(worktree, branch)
    _refuse_branch_checked_out_elsewhere(repo, branch, allowed_worktree=worktree)

    claimed = False
    mutation_started = False
    if session is not None:
        claim_session_deletion(
            db,
            session_name,
            expected_branch=branch,
            expected_worktree=str(worktree),
        )
        claimed = True
        _refuse_branch_checked_out_elsewhere(repo, branch, allowed_worktree=worktree)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    safe_name = _safe_ref_part(session_name)
    backup_refs: list[str] = []
    head_ref = f"refs/cocodex/deleted/{stamp}/{safe_name}/head"
    dirty_ref = f"refs/cocodex/deleted/{stamp}/{safe_name}/dirty"

    try:
        head_target = _head_target(repo, worktree, branch, worktree_is_present)
        if head_target is not None:
            update_ref(repo, head_ref, head_target)
            backup_refs.append(head_ref)

        if worktree_is_present and is_dirty(worktree):
            _stash_dirty_backup(worktree, dirty_ref, session_name=session_name)
            mutation_started = True
            backup_refs.append(dirty_ref)

        manifest = _write_manifest(
            repo,
            stamp=stamp,
            session_name=session_name,
            session=session,
            branch=branch,
            worktree=worktree,
            backup_refs=backup_refs,
            head_ref=head_ref if head_target is not None else None,
            dirty_ref=dirty_ref if dirty_ref in backup_refs else None,
        )

        worktree_removed = False
        if worktree_is_present:
            try:
                run_git(repo, ["worktree", "remove", "--force", str(worktree)], internal_write=True)
            except Exception:
                if claimed and not mutation_started:
                    _restore_claimed_session(db, session, reason="delete failed before worktree removal")
                raise
            worktree_removed = True
            mutation_started = True

        branch_deleted = False
        if branch_exists(repo, branch):
            run_git(repo, ["branch", "-D", branch], internal_write=True)
            branch_deleted = True

        delete_session_record(
            db,
            session_name,
            backup_refs=backup_refs,
            manifest=str(manifest),
            worktree_removed=worktree_removed,
            branch_deleted=branch_deleted,
        )
    except Exception as exc:
        if claimed and not mutation_started and session is not None:
            _restore_claimed_session(db, session, reason=str(exc))
        if mutation_started:
            raise DeletePartialError(
                f"delete partially completed for {session_name!r}; backup refs are {backup_refs}. "
                f"Inspect `cocodex status`, `git worktree list`, and the manifest under "
                f"{repo / '.cocodex' / 'deleted'} before retrying. Original error: {exc}"
            ) from exc
        raise

    remote_warning = try_sync_deleted_session_refs(
        repo,
        config.remote,
        session_branch=branch,
        backup_refs=backup_refs,
    )

    return DeleteResult(
        session=session_name,
        worktree=worktree,
        branch=branch,
        manifest=manifest,
        backup_refs=backup_refs,
        worktree_removed=worktree_removed,
        branch_deleted=branch_deleted,
        session_record_removed=session is not None,
        remote_warning=remote_warning,
    )


def format_delete_result(result: DeleteResult) -> str:
    lines = [
        f"Deleted Cocodex session {result.session}.",
        f"Worktree: {'removed ' + str(result.worktree) if result.worktree_removed else 'already absent'}",
        f"Branch: {'deleted ' + result.branch if result.branch_deleted else 'already absent'}",
        f"Session record: {'removed' if result.session_record_removed else 'already absent'}",
        f"Manifest: {result.manifest}",
    ]
    if result.backup_refs:
        lines.append("Backup refs:")
        lines.extend(f"  {ref}" for ref in result.backup_refs)
    else:
        lines.append("Backup refs: none; no local branch or worktree HEAD existed")
    lines.append(
        "Developer config was kept in .cocodex/config.json; remove that entry manually "
        "only if this developer should no longer be able to rejoin."
    )
    return "\n".join(lines) + "\n"


def format_delete_refusal(session_name: str, reason: str) -> str:
    return "\n".join(
        [
            f"cocodex delete refused for {session_name}.",
            "",
            f"Reason: {reason}",
            "",
            "What to do next:",
            "- If this developer is still working, do not delete the session.",
            "- If the session owns an active sync task, have that developer run `cocodex join "
            f"{session_name}` and then `cocodex sync` from that worktree until the task finishes.",
            "- If the join process is still running, close it first and retry.",
            "- If the worktree has an unsafe Git operation or ignored files, inspect that worktree "
            "and clean or archive those files before retrying.",
            "",
        ]
    )


def format_delete_partial(session_name: str, reason: str) -> str:
    return "\n".join(
        [
            f"cocodex delete partially completed for {session_name}.",
            "",
            f"Reason: {reason}",
            "",
            "What to do next:",
            "- Do not assume the old worktree or branch still exists.",
            "- Inspect `cocodex status`, `git worktree list`, and `git for-each-ref refs/cocodex/deleted`.",
            "- Use the printed backup refs and `.cocodex/deleted/` manifest to recover any needed work.",
            "- Retry `cocodex delete <user_name>` only after confirming the remaining local state.",
            "",
        ]
    )


def _validate_managed_targets(
    repo: Path,
    config: CocodexConfig,
    session_name: str,
    branch: str,
    worktree: Path,
) -> None:
    expected_branch = f"cocodex/{session_name}"
    if branch != expected_branch:
        raise RuntimeError(
            f"session {session_name!r} uses unexpected branch {branch!r}; "
            f"expected {expected_branch!r}. Refusing automatic delete."
        )

    root = (repo / config.worktree_root).resolve()
    candidate = worktree.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            f"session {session_name!r} uses worktree outside managed root: {worktree}"
        ) from exc


def _refresh_stale_runtime(
    db: sqlite3.Connection,
    session: SessionRecord | None,
) -> SessionRecord | None:
    if session is None:
        return None
    if session.connected and not _pid_is_alive(session.pid):
        mark_session_disconnected(db, session.name, "delete found stale runtime")
        return get_session(db, session.name) or session
    return session


def _refuse_unsafe_delete(
    db: sqlite3.Connection,
    session_name: str,
    session: SessionRecord | None,
) -> None:
    lock = get_lock(db)
    if lock is not None and lock["owner"] == session_name:
        raise RuntimeError(
            f"session owns the integration lock for task {lock['task_id']}; "
            "finish that sync before deleting it"
        )
    if session is None:
        return
    if session.active_task is not None:
        raise RuntimeError(
            f"session has active sync task {session.active_task}; finish that sync before deleting it"
        )
    pid_alive = _pid_is_alive(session.pid)
    if session.connected and pid_alive:
        raise RuntimeError(
            "session is still connected; close its `cocodex join` process before deleting it"
        )
    if pid_alive:
        raise RuntimeError(
            f"session process pid={session.pid} still appears to be running; close it before deleting"
        )


def _restore_claimed_session(db: sqlite3.Connection, session: SessionRecord, *, reason: str) -> None:
    transition_session(
        db,
        session.name,
        session.state,
        reason=f"delete claim restored after failure: {reason}",
        active_task=session.active_task,
        blocked_reason=session.blocked_reason,
    )


def _validate_worktree_for_delete(worktree: Path, branch: str) -> None:
    try:
        top_level = Path(run_git(worktree, ["rev-parse", "--show-toplevel"])).resolve()
        actual_branch = run_git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
    except GitError as exc:
        raise RuntimeError(f"{worktree} is not a valid Git worktree") from exc
    if top_level != worktree.resolve():
        raise RuntimeError(f"{worktree} is not a top-level Git worktree")
    if actual_branch != branch:
        raise RuntimeError(
            f"{worktree} is on branch {actual_branch!r}, expected {branch!r}; "
            "refusing to delete an unexpected worktree"
        )
    unsafe = has_unsafe_git_state(worktree)
    if unsafe:
        raise RuntimeError(
            f"{worktree} has an unfinished Git operation ({unsafe}); resolve or abort it first"
        )
    ignored = _significant_ignored_paths(worktree)
    if ignored:
        sample = ", ".join(ignored[:5])
        more = f", and {len(ignored) - 5} more" if len(ignored) > 5 else ""
        raise RuntimeError(
            f"{worktree} contains ignored files that Cocodex will not delete automatically: "
            f"{sample}{more}"
        )


def _refuse_branch_checked_out_elsewhere(
    repo: Path,
    branch: str,
    *,
    allowed_worktree: Path,
) -> None:
    output = run_git(repo, ["worktree", "list", "--porcelain"])
    current_worktree: Path | None = None
    expected_branch = f"refs/heads/{branch}"
    allowed = allowed_worktree.resolve()
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_worktree = Path(line.removeprefix("worktree ")).resolve()
            continue
        if line == f"branch {expected_branch}" and current_worktree != allowed:
            raise RuntimeError(
                f"branch {branch!r} is checked out in another worktree: {current_worktree}. "
                "Remove that worktree before deleting this Cocodex session."
            )


def _head_target(repo: Path, worktree: Path, branch: str, worktree_is_present: bool) -> str | None:
    if branch_exists(repo, branch):
        return current_head(repo, branch)
    if worktree_is_present:
        return current_head(worktree)
    return None


def _stash_dirty_backup(worktree: Path, dirty_ref: str, *, session_name: str) -> None:
    run_git(
        worktree,
        [
            "stash",
            "push",
            "--include-untracked",
            "-m",
            f"cocodex delete backup: {session_name}",
        ],
        internal_write=True,
    )
    stash_commit = current_head(worktree, "refs/stash")
    update_ref(worktree, dirty_ref, stash_commit)
    run_git(worktree, ["stash", "drop", "--quiet", "stash@{0}"], check=False, internal_write=True)


def _write_manifest(
    repo: Path,
    *,
    stamp: str,
    session_name: str,
    session: SessionRecord | None,
    branch: str,
    worktree: Path,
    backup_refs: list[str],
    head_ref: str | None,
    dirty_ref: str | None,
) -> Path:
    deleted_dir = repo / ".cocodex" / "deleted"
    deleted_dir.mkdir(parents=True, exist_ok=True)
    manifest = deleted_dir / f"{stamp}-{_safe_ref_part(session_name)}.json"
    payload = {
        "deleted_at": stamp,
        "session": session_name,
        "branch": branch,
        "worktree": str(worktree),
        "backup_refs": backup_refs,
        "head_backup_ref": head_ref,
        "dirty_backup_ref": dirty_ref,
        "session_record": asdict(session) if session is not None else None,
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _significant_ignored_paths(worktree: Path) -> list[str]:
    output = run_git(worktree, ["status", "--porcelain", "--ignored"])
    ignored: list[str] = []
    for line in output.splitlines():
        if not line.startswith("!! "):
            continue
        path = line[3:]
        if path == "AGENTS.md":
            continue
        ignored.append(path)
    return ignored


def _pid_is_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _safe_ref_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)
