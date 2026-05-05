from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


class GitError(RuntimeError):
    pass


def run_git(
    repo: Path,
    args: list[str],
    *,
    check: bool = True,
    timeout: float | None = None,
    internal_write: bool = False,
) -> str:
    command = ["git", *args]
    env = None
    if internal_write:
        env = os.environ.copy()
        env["COCODEX_INTERNAL_WRITE"] = "1"
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        display = " ".join(command)
        raise GitError(f"{display} timed out after {timeout:g}s") from exc
    if check and result.returncode != 0:
        display = " ".join(command)
        raise GitError(f"{display} failed with {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def is_dirty(repo: Path) -> bool:
    return bool(run_git(repo, ["status", "--porcelain"]))


def branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.returncode == 0


def create_worktree(repo: Path, *, branch: str, worktree: Path, start_point: str) -> None:
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        return
    if branch_exists(repo, branch):
        run_git(repo, ["worktree", "add", str(worktree), branch])
    else:
        run_git(repo, ["worktree", "add", "-b", branch, str(worktree), start_point])


def current_head(repo: Path, ref: str = "HEAD") -> str:
    return run_git(repo, ["rev-parse", ref])


def merge_base_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.returncode == 0


def ensure_fast_forward(repo: Path, ref: str, target: str) -> None:
    current = current_head(repo, ref)
    if not merge_base_is_ancestor(repo, current, target):
        raise GitError(f"{ref} cannot fast-forward from {current} to {target}")


def fast_forward_ref(repo: Path, ref: str, target: str) -> None:
    ensure_fast_forward(repo, ref, target)
    current_branch = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if current_branch == ref:
        run_git(repo, ["merge", "--ff-only", target], internal_write=True)
    else:
        run_git(repo, ["branch", "-f", ref, target], internal_write=True)


def push_ref(repo: Path, remote: str, source: str, dest: str) -> None:
    run_git(repo, ["push", remote, f"{source}:{dest}"])


def push(repo: Path, remote: str, ref: str) -> None:
    push_ref(repo, remote, ref, ref)


def force_push_session_refs(
    repo: Path,
    remote: str,
    *,
    main_branch: str,
    session_branch: str,
    timeout: float = 30.0,
) -> None:
    run_git(
        repo,
        [
            "push",
            "--force",
            remote,
            f"+refs/heads/{main_branch}:refs/heads/{main_branch}",
            f"+refs/heads/{session_branch}:refs/heads/{session_branch}",
        ],
        timeout=timeout,
        internal_write=True,
    )
    if run_git(repo, ["for-each-ref", "--format=%(refname)", "refs/cocodex"]):
        run_git(
            repo,
            ["push", "--force", remote, "+refs/cocodex/*:refs/cocodex/*"],
            timeout=timeout,
            internal_write=True,
        )


def try_force_push_session_refs(
    repo: Path,
    remote: str | None,
    *,
    main_branch: str,
    session_branch: str,
    timeout: float = 30.0,
) -> str | None:
    if remote is None:
        return None
    try:
        force_push_session_refs(
            repo,
            remote,
            main_branch=main_branch,
            session_branch=session_branch,
            timeout=timeout,
        )
    except Exception as exc:
        return str(exc)
    return None


def try_sync_deleted_session_refs(
    repo: Path,
    remote: str | None,
    *,
    session_branch: str,
    backup_refs: list[str],
    timeout: float = 30.0,
) -> str | None:
    if remote is None:
        return None

    errors: list[str] = []
    push_env = os.environ.copy()
    push_env["COCODEX_INTERNAL_WRITE"] = "1"

    if backup_refs:
        result = _remote_git_result(
            [
                "git",
                "push",
                "--force",
                remote,
                *[f"+{ref}:{ref}" for ref in backup_refs],
            ],
            repo=repo,
            timeout=timeout,
            env=push_env,
        )
        if isinstance(result, str):
            errors.append(f"push deleted-session backup refs failed: {result}")
        elif result.returncode != 0:
            errors.append(_compact_git_error("push deleted-session backup refs", result))

    ls_remote = _remote_git_result(
        ["git", "ls-remote", "--heads", remote, session_branch],
        repo=repo,
        timeout=timeout,
        env=push_env,
    )
    if isinstance(ls_remote, str):
        errors.append(f"check remote branch {session_branch} failed: {ls_remote}")
    elif ls_remote.returncode != 0:
        errors.append(_compact_git_error(f"check remote branch {session_branch}", ls_remote))
    elif ls_remote.stdout.strip():
        delete_remote = _remote_git_result(
            ["git", "push", remote, f":refs/heads/{session_branch}"],
            repo=repo,
            timeout=timeout,
            env=push_env,
        )
        if isinstance(delete_remote, str):
            errors.append(f"delete remote branch {session_branch} failed: {delete_remote}")
        elif delete_remote.returncode != 0:
            errors.append(_compact_git_error(f"delete remote branch {session_branch}", delete_remote))

    return "; ".join(errors) if errors else None


def _remote_git_result(
    command: list[str],
    *,
    repo: Path,
    timeout: float,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str] | str:
    try:
        return subprocess.run(
            command,
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            check=False,
        )
    except Exception as exc:
        return str(exc)


def _compact_git_error(action: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout).strip().replace("\n", " ")
    if not detail:
        detail = f"exit {result.returncode}"
    return f"{action} failed: {detail}"


def add_all(repo: Path) -> None:
    run_git(repo, ["add", "-A"])


def commit(repo: Path, message: str) -> str:
    run_git(repo, ["commit", "-m", message])
    return current_head(repo)


def diff(repo: Path, base: str, head: str) -> str:
    return run_git(repo, ["diff", f"{base}..{head}"])


def diff_check(repo: Path, base: str, head: str) -> None:
    run_git(repo, ["diff", "--check", f"{base}..{head}"])


def merge_commit(repo: Path, ref: str, message: str) -> None:
    run_git(repo, ["merge", "--no-ff", "--no-edit", "-m", message, ref])


def merge_abort(repo: Path) -> None:
    run_git(repo, ["merge", "--abort"], check=False)


def checkout(repo: Path, ref: str) -> None:
    run_git(repo, ["checkout", ref])


def reset_hard(repo: Path, ref: str) -> None:
    run_git(repo, ["reset", "--hard", ref])


def update_ref(repo: Path, ref: str, target: str) -> None:
    run_git(repo, ["update-ref", ref, target])


def create_backup_ref(
    worktree: Path,
    *,
    session_name: str,
    task_id: str | None,
    reason: str,
) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_session = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in session_name)
    safe_task = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (task_id or "manual"))
    backup_ref = f"refs/cocodex/backups/{stamp}/{safe_session}/{safe_task}"
    if is_dirty(worktree):
        add_all(worktree)
        snapshot = run_git(worktree, ["stash", "create", f"cocodex backup: {session_name} {reason}"])
        run_git(worktree, ["reset"], check=False)
        target = snapshot or current_head(worktree)
    else:
        target = current_head(worktree)
    update_ref(worktree, backup_ref, target)
    return backup_ref


def git_dir(repo: Path) -> Path:
    raw = Path(run_git(repo, ["rev-parse", "--git-dir"]))
    return raw if raw.is_absolute() else repo / raw


def has_unsafe_git_state(repo: Path) -> str | None:
    directory = git_dir(repo)
    for marker in ["MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "BISECT_LOG", "index.lock"]:
        if (directory / marker).exists():
            return marker
    return None
