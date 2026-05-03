from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class IntegrationTask:
    task_id: str
    session: str
    latest_main: str
    last_seen_main: str | None
    snapshot_commit: str
    diff_summary: str


def create_task_id(session: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    safe_session = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in session)
    return f"{stamp}-{safe_session}"


def _validate_task_id(task_id: str) -> None:
    if (
        not task_id
        or task_id in {".", ".."}
        or ".." in task_id
        or not re.fullmatch(r"[A-Za-z0-9._-]+", task_id)
    ):
        raise ValueError(f"Invalid task id: {task_id!r}")


def _diff_fence(diff_summary: str) -> str:
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", diff_summary)),
        default=0,
    )
    return "`" * max(3, longest_run + 1)


def validation_file_path(repo: Path, task_id: str) -> Path:
    _validate_task_id(task_id)
    return repo / ".cocodex" / "tasks" / f"{task_id}.validation.md"


def task_file_path(repo: Path, task_id: str) -> Path:
    _validate_task_id(task_id)
    return repo / ".cocodex" / "tasks" / f"{task_id}.md"


def validate_task_report(repo: Path, task_id: str) -> str | None:
    path = validation_file_path(repo, task_id)
    if not path.exists():
        return (
            f"validation report is missing: {path}. "
            "Describe the tests you designed, the checks you ran, and the results, "
            "then run cocodex sync again."
        )
    content = path.read_text(encoding="utf-8", errors="replace").strip()
    if len(content) < 40:
        return (
            f"validation report is too short: {path}. "
            "Record the validation plan, commands or checks run, results, and known risks."
        )
    return None


def write_task_file(repo: Path, task: IntegrationTask) -> Path:
    _validate_task_id(task.task_id)
    tasks_dir = repo / ".cocodex" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = task_file_path(repo, task.task_id)
    validation_path = validation_file_path(repo, task.task_id)
    last_seen = task.last_seen_main if task.last_seen_main else "unknown"
    diff_fence = _diff_fence(task.diff_summary)
    path.write_text(
        "\n".join(
            [
                "# Cocodex Integration Task",
                "",
                f"Session: {task.session}",
                f"Task: {task.task_id}",
                f"Latest main: {task.latest_main}",
                f"Last seen main: {last_seen}",
                f"Snapshot commit: {task.snapshot_commit}",
                f"Validation file: {validation_path}",
                "",
                "## Goal",
                "",
                "Based on latest `main`, re-implement or semantically merge the feature",
                "represented by the snapshot diff. The final worktree must be a",
                "candidate new `main`. Cocodex will reject this task if you sync again",
                "without committing a candidate. If the snapshot behavior is already",
                "covered, create an explicit no-op commit that explains why.",
                "",
                "Cocodex creates this task only after direct publish is not possible and",
                "a normal Git merge either cannot complete cleanly or fails Cocodex's",
                "lightweight structural checks. Treat the task as a semantic integration",
                "problem, not as a request to retry `git merge main` manually.",
                "",
                "Do not push `main` directly. This task affects only this session",
                "worktree and local `main`; other sessions catch up or publish only",
                "when they run `cocodex sync` from their own managed worktrees.",
                "",
                "Safe pause point: if this task interrupts another development request,",
                "first choose a safe pause point. Preserve the previous request's",
                "remaining intent in your session output or notes, complete this Cocodex",
                "task, and then continue the paused development work after sync succeeds.",
                "",
                "## Snapshot Diff",
                "",
                f"{diff_fence}diff",
                task.diff_summary.rstrip(),
                diff_fence,
                "",
                "## Validation",
                "",
                "There is no fixed project-wide test command. You are responsible",
                "for designing sufficient validation for this semantic merge. Use the",
                "project's existing tests when relevant, add or update tests when useful,",
                "and use targeted scripts or manual checks when the repository has no",
                "suitable automated coverage.",
                "",
                "Before running `cocodex sync` again, write a validation report to:",
                f"{validation_path}",
                "",
                "The report must summarize:",
                "",
                "- the behavior you intended to preserve or add;",
                "- tests or checks you designed and why they are sufficient;",
                "- exact commands or manual checks run;",
                "- results;",
                "- any remaining risk or intentionally untested area.",
                "",
                "## Completion",
                "",
                "After committing the final candidate and confirming the worktree is clean,",
                "write the validation report, then run `cocodex sync` again from this worktree.",
                "Cocodex will require the validation report, publish local `main`, and",
                "best-effort sync local `main` plus this session branch when a remote is",
                "configured.",
                "If you cannot complete the integration safely, stop and explain the blocker",
                "in your session output. An operator can inspect Cocodex state and recover.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path
