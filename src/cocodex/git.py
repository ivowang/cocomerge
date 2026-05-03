from __future__ import annotations

import os
import subprocess
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


def git_dir(repo: Path) -> Path:
    raw = Path(run_git(repo, ["rev-parse", "--git-dir"]))
    return raw if raw.is_absolute() else repo / raw


def has_unsafe_git_state(repo: Path) -> str | None:
    directory = git_dir(repo)
    for marker in ["MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "BISECT_LOG", "index.lock"]:
        if (directory / marker).exists():
            return marker
    return None
