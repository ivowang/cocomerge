from __future__ import annotations

from pathlib import Path

from .git import run_git


GUARD_MARKER = "cocodex-managed-main-guard"
HOOK_NAMES = (
    "pre-commit",
    "pre-merge-commit",
    "pre-rebase",
    "pre-applypatch",
    "pre-push",
    "reference-transaction",
)


def install_main_guard(repo: Path, *, main_branch: str) -> list[str]:
    hooks_dir = _hooks_dir(repo)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for hook_name in HOOK_NAMES:
        hook = hooks_dir / hook_name
        content = _hook_script(main_branch)
        if hook.exists():
            existing = hook.read_text(encoding="utf-8", errors="replace")
            if GUARD_MARKER not in existing:
                continue
            if existing == content:
                installed.append(hook_name)
                continue
        hook.write_text(content, encoding="utf-8")
        hook.chmod(hook.stat().st_mode | 0o755)
        installed.append(hook_name)
    return installed


def main_guard_status(repo: Path, *, main_branch: str) -> str:
    hooks_dir = _hooks_dir(repo)
    missing: list[str] = []
    for hook_name in HOOK_NAMES:
        hook = hooks_dir / hook_name
        if not hook.exists():
            missing.append(hook_name)
            continue
        existing = hook.read_text(encoding="utf-8", errors="replace")
        if GUARD_MARKER not in existing or f"COCODEX_MAIN_BRANCH={_shell_quote(main_branch)}" not in existing:
            missing.append(hook_name)
    if not missing:
        return "installed"
    return "missing " + ", ".join(missing)


def ensure_cocodex_excluded(repo: Path) -> None:
    common_dir = _git_common_dir(repo)
    exclude_path = common_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    if "/.cocodex/" in lines:
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    exclude_path.write_text(
        existing
        + separator
        + "# Cocodex runtime state\n"
        + "/.cocodex/\n",
        encoding="utf-8",
    )


def _hooks_dir(repo: Path) -> Path:
    return _git_common_dir(repo) / "hooks"


def _git_common_dir(repo: Path) -> Path:
    raw = Path(run_git(repo, ["rev-parse", "--git-common-dir"]))
    return raw if raw.is_absolute() else (repo / raw).resolve()


def _hook_script(main_branch: str) -> str:
    quoted_main = _shell_quote(main_branch)
    return f"""#!/bin/sh
# {GUARD_MARKER}
COCODEX_MAIN_BRANCH={quoted_main}

block_main() {{
    echo "Cocodex protects main: do not write or push '$COCODEX_MAIN_BRANCH' directly; use cocodex sync from a managed worktree." >&2
    exit 1
}}

if [ "${{COCODEX_INTERNAL_WRITE:-}}" = "1" ]; then
    exit 0
fi

hook_name=$(basename "$0")

case "$hook_name" in
    reference-transaction)
        state="${{1:-}}"
        [ "$state" = "prepared" ] || exit 0
        while read old new ref
        do
            if [ "$ref" = "refs/heads/$COCODEX_MAIN_BRANCH" ]; then
                block_main
            fi
        done
        ;;
    pre-push)
        while read local_ref local_sha remote_ref remote_sha
        do
            if [ "$local_ref" = "refs/heads/$COCODEX_MAIN_BRANCH" ] || [ "$remote_ref" = "refs/heads/$COCODEX_MAIN_BRANCH" ]; then
                block_main
            fi
        done
        ;;
    pre-commit|pre-merge-commit|pre-rebase|pre-applypatch)
        current_branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
        if [ "$current_branch" = "$COCODEX_MAIN_BRANCH" ]; then
            block_main
        fi
        ;;
esac

exit 0
"""


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
