# cocodex

[中文说明](docs/README_ZH.md)

cocodex coordinates multiple Codex sessions that share one Git repository on one
server account. It gives every developer an isolated managed worktree, then
serializes the moment when a developer's work becomes the next `main`.

## Core Rule

For day-to-day collaboration, a developer only needs one Cocodex command:

```bash
cocodex sync
```

`sync` means "move this Cocodex session to the next safe synchronization
state." Depending on the session state, it can:

- fast-forward a clean session to the latest `main`;
- publish a dirty session directly when it is already based on latest `main`;
- when both the dirty session and `main` advanced, try a locked Git merge with
  lightweight structural checks and publish it if clean;
- start a dirty session's semantic integration when Git cannot merge cleanly or
  the lightweight checks fail;
- after Cocodex gives the session a task, publish the committed candidate as
  the new `main`.

Developers and Codex sessions must not run `git pull main`, `git merge main`,
or `git push main` directly. Cocodex is the only writer to local `main`.
Cocodex installs local Git hooks that block ordinary direct writes and pushes
to `main`; intentional maintenance must be done outside the developer workflow.

If a remote is configured, every `cocodex sync` also tries to force-sync local
`main` and the current session branch to that remote. It does not push or prune
other developers' branches. Remote sync is best-effort: network or
authentication failures are reported as warnings and retried on later
`cocodex sync` commands.

The daemon does not automatically integrate dirty sessions. Local work stays in
the developer's managed worktree until that developer or their Codex explicitly
runs `cocodex sync` from inside that worktree. In Codex, run it as a shell
command, for example `!cocodex sync`.

## Roles

Operator/startup commands, run from the project repository:

```bash
cocodex init --main main --remote origin
cocodex daemon
cocodex join alice
```

Developer collaboration command, run from inside that developer's managed
worktree, usually through Codex as `!cocodex sync`:

```bash
cocodex sync
```

Inspection commands:

```bash
cocodex status
cocodex log
cocodex task alice
```

`resume` and `abandon` recovery commands exist for operators, but they are not
part of the normal developer workflow.

## Installation

After the first PyPI release:

```bash
pip install cocodex
```

For development from a local checkout:

```bash
pip install -e .
```

This installs the `cocodex` console command.

## Repository Setup

Run these commands in the project repository that the team wants to develop
with Cocodex.

The configured main branch must already exist and have an initial commit:

```bash
git switch -c main
git add .
git commit -m "initial commit"
```

If Cocodex should keep a remote copy of the server's local branches, add the
remote before initialization:

```bash
git remote add origin <url>
```

Initialize Cocodex once:

```bash
cocodex init --main main --remote origin
```

`init` refuses to overwrite an existing `.cocodex/config.json`, because that
file contains developer identities and launch commands. Use `cocodex init
--force` only when you intentionally want to replace the existing Cocodex
configuration.

Use `--remote origin` only if that remote exists. With a remote configured,
`cocodex sync` force-pushes local `main` and the current session branch to that
remote. Omit `--remote` for local-only coordination.

`init` also installs Cocodex-managed Git hooks and adds `/.cocodex/` to the
repository-local `.git/info/exclude`. The hooks block normal direct commits,
cherry-picks, rebases, ref updates, and pushes of `main` unless the operation
comes from Cocodex itself.

Before developers join, edit `.cocodex/config.json` and fill in the top-level
`developers` object. Keep the other keys that `cocodex init` wrote; do not
replace the whole file with only the developer fragment. A typical config looks
like this:

```json
{
  "developers": {
    "alice": {
      "git_user_name": "Alice Example",
      "git_user_email": "alice@example.com"
    },
    "bob": {
      "git_user_name": "Bob Example",
      "git_user_email": "bob@example.com"
    }
  },
  "dirty_interval_s": 2.0,
  "main_branch": "main",
  "remote": "origin",
  "socket_path": ".cocodex/cocodex.sock",
  "worktree_root": ".cocodex/worktrees"
}
```

Use `"remote": null` if the repository is local-only. The keys under
`developers` are the only names accepted by `cocodex join <user_name>`, so
`cocodex join alice` requires an `alice` entry.

`command` is optional per developer; when omitted, Cocodex starts `codex`. For
a custom Codex launch, use a JSON string array such as
`"command": ["codex", "--model", "gpt-5.5"]`.

## Starting Codex Sessions

Start one daemon in a long-running terminal from the project repository:

```bash
cocodex daemon
```

The daemon prints an operational log in that terminal: session joins, sync
requests, task starts, busy sync rejections, integration lock changes, publish events, remote sync
failures, and recovery transitions.

Start each Codex session through Cocodex from that developer's tmux window:

```bash
cocodex join alice
cocodex join bob
```

`join` has the same form for first-time use and restart. The developer name
comes from `.cocodex/config.json`; Git identity and the Codex launch command
come from the matching config entry.

Each joined session gets:

- a branch named `cocodex/<name>`;
- a worktree under `.cocodex/worktrees/<name>`;
- a session agent that receives Cocodex tasks;
- a Git-ignored `AGENTS.md` in that worktree, unless the project already has
  its own `AGENTS.md`.

`join` reads the developer's Git identity from `.cocodex/config.json` and writes
it into that worktree's per-worktree Git config, so Cocodex snapshot commits
and Codex candidate commits have the right author.

Cocodex assumes `join` is run from the developer's own tmux pane. When
`TMUX_PANE` is present, `join` automatically binds the session agent to that
pane, so sync tasks and restart notices are pasted into the running Codex as
ordinary user prompts.

For advanced setups, override the target explicitly:

```bash
cocodex join --tmux-target "$TMUX_PANE" alice
```

If `join` is not running inside tmux, Cocodex prints the task and prompt file
paths instead. The developer then needs to open the task file from the session
worktree and follow it manually.

The generated `AGENTS.md` tells Codex that it is in a Cocodex-managed
collaboration session and that normal synchronization uses only
`cocodex sync` from inside the managed worktree.

## Restarting A Session

If a developer closes their Codex window, restart with the same session name:

```bash
cocodex join alice
```

Cocodex reuses `.cocodex/worktrees/alice` and `cocodex/alice`. On startup,
`join` checks for unfinished Cocodex responsibilities before normal
development continues:

- an active sync task is re-announced with its task and validation file paths;
- a safely recoverable interrupted task is moved back to `fusing`;
- a queued sync request from an interrupted startup window is reported so Codex
  waits for the task;
- a clean session that only fell behind `main` is reported, but not moved;
- local unintegrated work is reported so Codex reviews it before starting
  unrelated work.

If a restart notice appears, handle that notice before accepting new feature
work. In the normal tmux workflow, Cocodex pastes the notice into the Codex
pane automatically.

## What `sync` Does

### Clean Session

If Alice has no local work and `main` has advanced, this catches Alice up:

```bash
cocodex sync
```

If Alice is already current, Cocodex reports that the session is already
synced.

### Dirty Session Based On Current Main

If Alice has local edits or commits and `main` has not advanced since Alice
last synced, Cocodex publishes Alice's current worktree directly:

```bash
cocodex sync
```

If the worktree has uncommitted changes, Cocodex creates a snapshot commit with
Alice's configured Git identity, fast-forwards local `main` to that commit, and
best-effort syncs the remote. This path does not create a Codex fusion task,
because there is no newer main work to merge with.

### Dirty Session After Main Advanced

If Alice has local edits or commits and `main` has advanced since Alice last
synced, `cocodex sync` starts integration only if no other session is already
syncing. If another session owns the integration lock, the command fails with
`integration busy`; Alice should retry after that session finishes. When the
lock is free, Cocodex:

1. freezes Alice's session;
2. snapshots Alice's current work;
3. tries a normal Git merge of latest `main` into Alice's snapshot;
4. runs lightweight checks: the worktree must be clean, the candidate must
   contain both latest `main` and Alice's snapshot, and `git diff --check` must
   pass for the candidate diff;
5. publishes the merge commit directly if those checks pass.

If Git reports a conflict, leaves an unsafe state, or the lightweight checks
fail, Cocodex resets Alice's worktree to latest `main`, writes a task file under
`.cocodex/tasks/`, and pastes the sync prompt into Alice's Codex terminal when
tmux is available. Alice's Codex then reads the task file and re-implements or
semantically merges Alice's feature on top of the latest `main`. If the task
arrives while Codex is working on another request, Codex should pause at a safe
point, preserve that request's remaining intent, complete the sync task, and
then resume the paused work after sync succeeds.

For each task, Codex designs and runs sufficient validation for the semantic
merge. That can mean existing tests, new or updated tests, targeted scripts, or
manual checks when the project has no suitable test framework. Before running
sync again, Codex writes the requested validation report under
`.cocodex/tasks/`. After it commits the final candidate and the worktree is
clean, it runs the same command again:

```bash
cocodex sync
```

Cocodex then requires the validation report, fast-forwards local `main`, and
best-effort syncs the configured remote if one exists. Other session worktrees
are not moved or notified as part of this publish.

If the task cannot be completed safely, Codex should stop and explain the
blocker in its session output. An operator can inspect `cocodex status` and
`cocodex task <name>` before deciding how to recover.

## Normal Example

Alice and Bob both start Codex through Cocodex. Alice implements feature A; Bob
implements feature B. Neither branch is integrated automatically.

Alice runs:

```bash
!cocodex sync
```

If no one else has advanced `main` since Alice last synced, Cocodex publishes
Alice directly. If `main` has advanced, Cocodex first tries a normal Git merge
under the integration lock. If that merge and the lightweight checks pass,
Cocodex publishes without involving Codex. Only when Git cannot merge cleanly
or the checks fail does Cocodex give Alice's Codex a task; Alice's Codex applies
feature A on latest `main`, commits, and runs:

```bash
!cocodex sync
```

Now feature A is the new `main`.

Bob later runs:

```bash
!cocodex sync
```

Because Alice has now advanced `main`, Bob receives a task based on the current
`main`, which already includes feature A. Bob's Codex applies feature B on top
of that, commits, and runs:

```bash
!cocodex sync
```

This gives the team a serial mainline even though the Codex sessions worked
asynchronously.

If Bob runs `!cocodex sync` while Alice's task is still active, Cocodex rejects
Bob's command with `integration busy`. Bob keeps working in his own worktree and
runs `!cocodex sync` again after Alice's sync finishes.

## Safety Behavior

Cocodex prefers stopping over guessing:

- a dirty session is not integrated until its owner runs `sync`;
- one session's `sync` never fast-forwards another session's worktree;
- remote sync only force-pushes local `main` and the current session branch;
- only one session owns the integration lock at a time;
- a second session's `sync` is rejected while another session is already
  syncing;
- clean Git merges are attempted under the same lock before a Codex semantic
  task is created;
- local Git hooks block ordinary direct writes and pushes to `main`;
- running `sync` before committing a task candidate is rejected;
- missing or insufficient validation reports keep the task locked so the same
  session can write the report and run `sync` again;
- remote sync failures do not block local progress; Cocodex warns and retries
  on the next `sync`;
- unexpected recovery states require operator inspection.

## Recovery And Resume

Use `cocodex status` first. It shows daemon/session versions, guard status,
each session state, active task, blocked reason, branch head, configured
remote, and whether the integration lock is held. Use `cocodex task <name>` to
inspect one session's active task file, validation file, snapshot ref, and base
ref.

## Failure Handling Flow

When a Cocodex command fails, do not immediately run Git recovery commands by
hand. Keep the affected worktree as-is and follow this order:

1. Read the failure output. Recent Cocodex versions print a `Cocodex failure
   handling` block with the next safe action.
2. Run `cocodex status` from the project repository to identify the affected
   session, state, active task, lock owner, and version mismatch if any.
3. If the session has an active task, run `cocodex task <name>` and inspect the
   task file, validation file, snapshot ref, and base ref.
4. Decide whether the same developer session can continue, or whether an
   operator must intervene.
5. Only after the next action is clear, run `cocodex sync`, `cocodex resume
   <name>`, or `cocodex abandon <name>`.

Common cases:

- `integration busy`: do nothing to the worktree; retry `cocodex sync` from the
  same worktree after the current lock owner finishes.
- Active task blocked because the candidate is missing, dirty, or lacks a
  validation report: the same Codex session fixes the task and runs
  `cocodex sync` again.
- Taskless `blocked`: an operator fixes the external blocker, then runs
  `cocodex resume <name>` from the project repository.
- `recovery_required`: an operator inspects `cocodex status`, `cocodex log`,
  and `cocodex task <name>` before resuming or abandoning.
- `version mismatch`: restart that developer's `cocodex join <name>` after the
  installed Cocodex package has been upgraded.
- Remote sync warning: local publish already completed; fix network or Git
  authentication later and let a later `cocodex sync` retry.
- `Cocodex protects main`: the hook blocked direct `main` work. Continue in a
  managed worktree and publish through `cocodex sync`.

Do not use `abandon` as the first response to a failure. `abandon` is for tasks
that should be discarded or recovered manually; it creates a backup ref before
clearing Cocodex bookkeeping, but it is still an operator decision.

For normal task blocks, do not use `resume` immediately. If a session is
`blocked` with a task id because the candidate was not committed, the worktree
is dirty before validation, or the validation report is missing, the owning
Codex should fix that issue in the same managed worktree and run
`cocodex sync` again. The integration lock remains with that task so no other
session can publish over it.

Use `cocodex resume <name>` when `status` shows a blocked or recovery session
that needs operator help. This is an operator action from the project
repository, not a normal developer command. If the session has an active task,
`resume` restores that task under the integration lock and re-announces the task
to the session agent when it is connected. If the session has no active task,
fix the underlying blocker first, then resume. For example, if direct publish
failed because the project repository's main worktree had local files that
would be overwritten, clean or move those files in the main worktree, then run:

```bash
cocodex resume alice
```

After taskless resume, Cocodex retries that session when the daemon can process
it. If the session's Codex window was closed, restart it with:

```bash
cocodex join alice
```

Use `cocodex abandon <name>` only when the active Cocodex task should be
discarded or manually recovered outside Cocodex. `abandon` clears Cocodex's
queue/task/lock bookkeeping for that session; it does not revert files or
commits in the session worktree. Before clearing state, it creates a backup ref
under `refs/cocodex/backups/...` and prints it.

Keep the project repository's main worktree clean during normal operation.
Developer edits belong in `.cocodex/worktrees/<name>`. Uncommitted files in the
main worktree can block Cocodex from fast-forwarding local `main`.

## Command Reference

Normal developer command:

```bash
cocodex sync
```

Common operator commands:

```bash
cocodex init --main main --remote origin
cocodex daemon
cocodex join alice
cocodex status
cocodex log
cocodex task alice
cocodex resume alice
cocodex abandon alice
```

## Troubleshooting

`Developer 'alice' is not configured in .cocodex/config.json`
means the operator has not added an `alice` entry under `developers`, or the
command is being run from a repository with a different Cocodex config.

`cocodex sync must run inside a Git worktree` or
`Run cocodex sync inside a managed worktree` means the command was not run from
`.cocodex/worktrees/<name>`. Start or re-enter the session with
`cocodex join <name>`, then run `!cocodex sync` from that Codex session.

If Cocodex prints task and prompt file paths instead of pasting into Codex,
`join` probably was not started from a tmux pane, or tmux prompt injection
failed. Read the task file in the session worktree and follow it manually, or
restart the session from the developer's tmux pane with `cocodex join <name>`.

Remote sync warnings are non-fatal. Fix the network or Git authentication
problem when convenient; Cocodex retries remote synchronization on later
`cocodex sync` commands.

`integration busy: <name> is syncing task ...` means another session currently
owns the integration lock or is about to receive a sync task. Keep your
worktree as-is and run `!cocodex sync` again after that sync finishes.

`Cocodex protects main` means a Git hook blocked a direct write or push to
`main`. Do developer work in `.cocodex/worktrees/<name>` and publish with
`cocodex sync`.

`version mismatch` means the daemon and a running `cocodex join` agent are from
different Cocodex versions. Stop and restart that developer's `cocodex join`
after upgrading the installed package.

If local `main` advances but the Git remote never changes, check
`cocodex status` and `.cocodex/config.json`. Cocodex only pushes when
`remote` is configured, for example `"remote": "origin"`. A repository can have
a Git `origin` remote while Cocodex still shows `remote: none` if it was
initialized without `--remote origin`; edit the config or reinitialize
intentionally before expecting remote sync.

`sync already in progress (publishing)` should be short-lived. If it persists,
run `cocodex status` and `cocodex log`. A blocked session with no lock usually
means the operator should fix the logged blocker and run `cocodex resume
<name>`. A `publishing` session with no lock after a daemon crash should be
handled by restarting the daemon so startup recovery can move it to
`recovery_required`.

Implementation details are documented in [docs/DEV.md](docs/DEV.md).
