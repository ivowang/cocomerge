from __future__ import annotations


def format_failure_handling(
    *,
    reason: str,
    session: str | None = None,
    state: str | None = None,
    active_task: str | None = None,
) -> str:
    lines = ["", "Cocodex failure handling:"]
    if session:
        lines.append(f"- Session: {session}")
    if state:
        lines.append(f"- State: {state}")
    if active_task:
        lines.append(f"- Task: {active_task}")

    reason_lower = reason.lower()
    if "integration busy" in reason_lower:
        lines.extend(
            [
                "- Retry `cocodex sync` from this worktree after that session finishes.",
                "- Do not reset this worktree or manually merge main; your local work is still protected here.",
                "- To see the current owner, run `cocodex status` from the project repository.",
            ]
        )
    elif "version mismatch" in reason_lower:
        name = session or "<name>"
        lines.extend(
            [
                f"- Restart `cocodex join {name}` after upgrading Cocodex.",
                "- Do not resume the session with the stale agent still running.",
                "- After restart, run `cocodex status` to confirm the agent version matches the daemon.",
            ]
        )
    elif "daemon is not running" in reason_lower:
        lines.extend(
            [
                "- Start the coordinator from the project repository: `cocodex daemon`.",
                "- Keep the daemon terminal open so failures and recovery events are visible.",
                "- Then retry `cocodex sync` from the managed worktree.",
            ]
        )
    elif active_task:
        name = session or "<name>"
        lines.extend(
            [
                "- Same session: fix the task blocker, then run `cocodex sync` again from this worktree.",
                f"- Inspect the task details with `cocodex task {name}` from the project repository.",
                "- Do not abandon the task unless an operator has decided to recover it manually.",
            ]
        )
    elif state in {"blocked", "recovery_required"} or "blocked" in reason_lower:
        name = session or "<name>"
        lines.extend(
            [
                f"- Operator: fix the blocker, then run `cocodex resume {name}` from the project repository.",
                "- Inspect `cocodex status` and `cocodex log` before resuming.",
                f"- If the task must be discarded, run `cocodex abandon {name}` only after confirming backups.",
            ]
        )
    elif "cocodex protects main" in reason_lower:
        lines.extend(
            [
                "- Do not commit, merge, cherry-pick, rebase, or push main directly.",
                "- Do developer work inside `.cocodex/worktrees/<name>`.",
                "- Publish through `cocodex sync` from the managed worktree.",
            ]
        )
    else:
        lines.extend(
            [
                "- Run `cocodex status` from the project repository.",
                "- Run `cocodex log` and inspect the most recent event for the affected session.",
                "- Preserve the worktree as-is until the next action is clear.",
            ]
        )
    return "\n".join(lines) + "\n"


def next_step_for_session(
    *,
    session: str,
    state: str,
    active_task: str | None,
    blocked_reason: str | None,
) -> str:
    reason = (blocked_reason or "").lower()
    if "version mismatch" in reason:
        return f"Next step: restart `cocodex join {session}` after upgrading Cocodex."
    if active_task and state in {"fusing", "verifying", "publishing"}:
        return (
            "Next step: the same session completes the task, commits the candidate, "
            "writes validation, then runs `cocodex sync`."
        )
    if active_task and state == "blocked":
        return "Next step: the same session fixes the task blocker, then runs `cocodex sync` again."
    if active_task and state == "recovery_required":
        return f"Next step: operator inspects this task, then runs `cocodex resume {session}` or `cocodex abandon {session}`."
    if state == "blocked":
        return f"Next step: operator fixes the blocker, then runs `cocodex resume {session}`."
    if state == "recovery_required":
        return f"Next step: operator inspects `cocodex status` and `cocodex log`, then resumes or abandons {session}."
    if state == "queued":
        return "Next step: keep the session running until Cocodex starts the task."
    if state == "clean":
        return "Next step: no failure is recorded for this session."
    return "Next step: inspect `cocodex status` and `cocodex log` before changing this worktree."
