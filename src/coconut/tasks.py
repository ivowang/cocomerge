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
    verify: str | None
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


def write_task_file(repo: Path, task: IntegrationTask) -> Path:
    _validate_task_id(task.task_id)
    tasks_dir = repo / ".coconut" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / f"{task.task_id}.md"
    verify_text = task.verify if task.verify else "No verification command configured"
    last_seen = task.last_seen_main if task.last_seen_main else "unknown"
    diff_fence = _diff_fence(task.diff_summary)
    path.write_text(
        "\n".join(
            [
                "# Coconut Integration Task",
                "",
                f"Session: {task.session}",
                f"Task: {task.task_id}",
                f"Latest main: {task.latest_main}",
                f"Last seen main: {last_seen}",
                f"Snapshot commit: {task.snapshot_commit}",
                "",
                "## Goal",
                "",
                "Based on latest `main`, re-implement or semantically merge the feature",
                "represented by the snapshot commit. The final worktree must be a",
                "candidate new `main`. If the snapshot behavior is already covered,",
                "record an explicit no-op result.",
                "",
                "Do not push `main` directly.",
                "",
                "## Snapshot Diff",
                "",
                f"{diff_fence}diff",
                task.diff_summary.rstrip(),
                diff_fence,
                "",
                "## Verification",
                "",
                f"Run: {verify_text}",
                "",
                "## Completion",
                "",
                f"After committing the final candidate, run `coconut done {task.session}`.",
                "Coconut will verify, publish local `main`, and push the configured remote.",
                f"If you cannot complete the integration safely, run `coconut block {task.session} \"<reason>\"`.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path
