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
                "represented by the snapshot diff. The required result is a candidate",
                "new `main` whose behavior is the union of latest `main` and the",
                "snapshot work. Preserve both sides unless the user explicitly chooses",
                "otherwise.",
                "",
                "Use latest `main` as the architectural baseline. If latest `main`",
                "renamed modules, changed abstractions, replaced APIs, moved files,",
                "changed schemas, or altered tests, adapt the snapshot feature to that",
                "new design instead of mechanically replaying the old patch. Your job",
                "is semantic integration, not text conflict resolution.",
                "",
                "Do not omit a snapshot feature, a latest-main behavior, a migration, a",
                "test expectation, a config change, or a user-visible workflow just",
                "because it is difficult to reconcile. Cocodex will reject this task if",
                "you sync again without committing a candidate. If the snapshot behavior",
                "is already fully covered by latest `main`, create an explicit no-op",
                "commit that explains why.",
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
                "## Required Merge Discipline",
                "",
                "Before editing, inspect the snapshot diff, latest `main`, surrounding",
                "code, tests, docs, configuration, generated files, migrations, and",
                "callers that define the behavior on both sides. Identify what latest",
                "`main` added or changed, what the snapshot added or changed, and what",
                "the final combined behavior must be.",
                "",
                "Prefer small, coherent edits that fit latest `main`'s current patterns.",
                "It is acceptable to rewrite the snapshot implementation completely if",
                "that is the right way to preserve the feature on top of the new main.",
                "It is not acceptable to keep dead code, duplicate old architecture, or",
                "silently remove either side's behavior.",
                "",
                "If you encounter a genuine contradiction between latest `main` and the",
                "snapshot, stop and ask the user for a resolution. A genuine",
                "contradiction includes mutually exclusive product behavior, API",
                "contracts, schemas, data invariants, security rules, or tests where",
                "both sides cannot be true in one product. In that case, do not guess,",
                "do not choose one side arbitrarily, and do not mark the task complete.",
                "Explain the exact files and behaviors involved, describe the plausible",
                "merge options, and wait for the user's decision.",
                "",
                "If the conflict is only an implementation detail, resolve it yourself",
                "by preserving both intended behaviors in the latest-main architecture.",
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
                "- the latest-main behaviors you checked still work;",
                "- the snapshot behaviors you re-implemented or confirmed already exist;",
                "- the combined behavior you intended to preserve or add;",
                "- tests or checks you designed and why they are sufficient;",
                "- exact commands or manual checks run;",
                "- results;",
                "- any contradictions found and the user-approved resolution, or state",
                "  that no unresolved contradiction remains;",
                "- any remaining risk or intentionally untested area.",
                "",
                "## Completion",
                "",
                "After committing the final candidate and confirming the worktree is clean,",
                "write the validation report, then run `cocodex sync` again from this worktree.",
                "Cocodex will require the validation report, publish local `main`, and",
                "best-effort sync local `main`, this session branch, and Cocodex recovery",
                "refs when a remote is configured.",
                "If you cannot complete the integration safely, stop and explain the blocker",
                "in your session output. Keep this task active until the same session can",
                "fix the candidate and run `cocodex sync` again.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path
