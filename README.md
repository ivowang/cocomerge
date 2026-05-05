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

If a remote is configured, every successful local `cocodex sync` also tries to
force-sync local `main`, the current session branch, and Cocodex recovery refs
to that remote. It does not push or prune other developers' branches. Remote
sync is best-effort: network or authentication failures are reported as
warnings and retried on later successful `cocodex sync` commands.

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
```

Maintenance command for an operator cleaning up a retired session:

```bash
cocodex delete alice
```

There are no manual recovery commands. `delete` is not a recovery path for a
stuck sync; it is only for a session that the team has decided to retire. If a
sync cannot proceed, run `cocodex status` or `cocodex log` for inspection, then
let the named owning developer run `cocodex join <name>` if needed and
`cocodex sync` from their own managed worktree.

## Installation

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

For normal semantic sync tasks, `join` must be running inside tmux so Cocodex
can paste the prompt into Codex. Cocodex does not press Enter for the
developer; when the prompt appears in the Codex input box, review it and press
Enter to start the task. If no tmux target is available, Cocodex refuses task
startup and restores the session snapshot for retry.

The generated `AGENTS.md` tells Codex that it is in a Cocodex-managed
collaboration session and that normal synchronization uses only
`cocodex sync` from inside the managed worktree.

## Deleting An Old Session

When a developer no longer needs an old session, an operator can remove its
local Cocodex registration and managed resources:

```bash
cocodex delete alice
```

`delete` refuses connected sessions, sessions that own the integration lock,
sessions with an active sync task, worktrees with an unfinished Git operation,
branches checked out in another worktree, and worktrees that contain ignored
files other than Cocodex's generated `AGENTS.md`. This keeps deletion out of
the normal collaboration and recovery flows.

Before removing anything, Cocodex writes a manifest under `.cocodex/deleted/`
and creates recovery refs under `refs/cocodex/deleted/...`. If the worktree has
tracked or untracked changes, Cocodex stores them in a dirty backup ref before
removing the worktree. It then removes the local worktree, deletes the local
`cocodex/<name>` branch, removes the session and queue rows from state, and
records a `session_deleted` event.

The developer entry in `.cocodex/config.json` is intentionally kept. Remove it
manually only if that developer should no longer be able to run
`cocodex join <name>`. If a remote is configured, delete also tries to push the
deleted-session backup refs and remove the remote `cocodex/<name>` branch; remote
failure is reported as a warning and does not roll back local cleanup.

## Restarting A Session

If a developer closes their Codex window, restart with the same session name:

```bash
cocodex join alice
```

Cocodex reuses `.cocodex/worktrees/alice` and `cocodex/alice`. On startup,
`join` checks for unfinished Cocodex responsibilities before normal
development continues:

- an active sync task is re-announced with its task and validation file paths;
- a safely recoverable interrupted task is kept or moved back to `fusing`;
- an interrupted task startup that never safely reached Codex is normalized
  after restoring the snapshot when possible;
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
`.cocodex/tasks/`, and pastes the sync prompt into Alice's Codex input box.
A semantic task is accepted once the prompt has been delivered to the pane, but
Cocodex does not submit it automatically. Alice should press Enter in Codex to
start the task. If Cocodex cannot safely deliver the prompt, it restores Alice's
snapshot, releases the lock, rejects this `sync`, and leaves Alice's work
available for retry. Alice's Codex then reads the task file and re-implements
or semantically merges Alice's feature on top of the latest `main`. The target
candidate is the behavioral union of latest `main` and Alice's snapshot. If
Codex finds a genuine contradiction between the two sides, such as mutually
exclusive product behavior, API contracts, schemas, or data invariants, it must
ask the user how to resolve it instead of silently dropping one side. If the
task arrives while Codex is working on another request, Codex should pause at a
safe point, preserve that request's remaining intent, complete the sync task,
and then resume the paused work after sync succeeds.

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
blocker in its session output. Cocodex keeps the active task in `fusing`, keeps
the lock with that session, and rejects later `sync` attempts until the same
session has a committed candidate and validation report.

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
- remote sync only force-pushes local `main`, the current session branch, and
  Cocodex recovery refs after local sync has succeeded;
- only one session owns the integration lock at a time;
- a second session's `sync` is rejected while another session is already
  syncing;
- clean Git merges are attempted under the same lock before a Codex semantic
  task is created;
- local Git hooks block ordinary direct writes and pushes to `main`;
- running `sync` before committing a task candidate is rejected;
- rejected active-task publishes do not create persistent `blocked` states; the
  task remains active so the same session can fix the issue and run `sync`
  again;
- active-task publish requires the task file, snapshot ref, base ref,
  validation report, and candidate commit to remain intact;
- startup cleanup creates backup refs before clearing interrupted task state;
- remote sync failures do not block local progress; Cocodex warns and retries
  on the next `sync`;
- interrupted active tasks are re-announced by `cocodex join <name>`; legacy
  recovery states are normalized instead of becoming the normal workflow.

## Refusals And Recovery

Use `cocodex status` first. It shows daemon/session versions, guard status,
each session state, active task, branch head, configured remote, and whether
the integration lock is held. When a session has an active task, `status`
also shows its task file, validation file, snapshot ref, base ref, and next
safe action.

When a Cocodex command fails, do not immediately run Git recovery commands by
hand. Keep the affected worktree as-is and follow this order:

1. Read the refusal output. Cocodex prints a `Cocodex sync refused` block with
   the next safe action.
2. Run `cocodex status` from the project repository to identify the affected
   session, state, active task, lock owner, and version mismatch if any.
3. If the session has an active task, read the task file and validation path
   shown by `status` or the refusal output.
4. Let the same developer session continue whenever possible. Most refusals are
   fixed by committing the candidate, cleaning the worktree, adding validation,
   restarting `cocodex join <name>`, or retrying after the current lock owner
   finishes.

Common cases:

- `integration busy`: do nothing to the worktree; retry `cocodex sync` from the
  same worktree after the current lock owner finishes. If the message says the
  owner is disconnected, ask that developer to restart with
  `cocodex join <name>` and then run `cocodex sync` from their managed
  worktree.
- Active task refused because the candidate is missing, dirty, lacks a
  validation report, or is missing its task/snapshot/base handles: the task
  remains `fusing`; the same Codex session fixes the issue and runs
  `cocodex sync` again.
- Main worktree dirty or unsafe: clean the project repository's main worktree,
  then retry `cocodex sync` from the managed session worktree. Cocodex has not
  moved `main` or discarded the session work.
- `version mismatch`: restart that developer's `cocodex join <name>` after the
  installed Cocodex package has been upgraded.
- Remote sync warning: local publish already completed; fix network or Git
  authentication later and let a later `cocodex sync` retry.
- `Cocodex protects main`: the hook blocked direct `main` work. Continue in a
  managed worktree and publish through `cocodex sync`.

If a developer's Codex window was closed during an active task, restart it with:

```bash
cocodex join alice
```

Cocodex re-announces the active task and keeps the lock with Alice until Alice
publishes through `cocodex sync`. If legacy state from an older Cocodex version
is present, `cocodex sync` and `cocodex join <name>` normalize what is safe to
normalize and otherwise print the owning session and next action.

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
cocodex delete alice
cocodex status
cocodex log
```

## Troubleshooting

`Developer 'alice' is not configured in .cocodex/config.json`
means the operator has not added an `alice` entry under `developers`, or the
command is being run from a repository with a different Cocodex config.

`cocodex sync must run inside a Git worktree` or
`Run cocodex sync inside a managed worktree` means the command was not run from
`.cocodex/worktrees/<name>`. Start or re-enter the session with
`cocodex join <name>`, then run `!cocodex sync` from that Codex session.

If Cocodex refuses a semantic task because no tmux target is available, `join`
was probably not started from the developer's tmux pane. Restart the session
from that pane with `cocodex join <name>`, then retry `!cocodex sync`. Cocodex
restores the snapshot before rejecting the task, so the developer's work remains
available for retry.

Remote sync warnings are non-fatal. Fix the network or Git authentication
problem when convenient; Cocodex retries remote synchronization on later
`cocodex sync` commands.

`integration busy: <name> is syncing ...` means another session currently
owns the integration lock or is about to receive a sync task. Keep your
worktree as-is and run `!cocodex sync` again after that sync finishes.

`integration busy: <name> is disconnected while syncing ...` means the lock
owner was interrupted while holding an active sync task. Do not work around the
lock. The owner should run `cocodex join <name>` from the project repository and
then run `cocodex sync` from their managed worktree. Other developers should
keep their own worktrees unchanged and retry after the owner finishes.

`Cocodex protects main` means a Git hook blocked a direct write or push to
`main`. Do developer work in `.cocodex/worktrees/<name>` and publish with
`cocodex sync`.

`version mismatch` means the daemon and a running `cocodex join` agent are from
different Cocodex versions. Stop and restart that developer's `cocodex join`
after upgrading the installed package.

`cocodex delete refused for alice` means the session is not safe to remove yet.
The output names the exact blocker. For active tasks, the owner should rejoin
and finish `cocodex sync`; for connected sessions, close the old `join` process;
for ignored files, inspect or archive the worktree before retrying delete.

If local `main` advances but the Git remote never changes, check
`cocodex status` and `.cocodex/config.json`. Cocodex only pushes when
`remote` is configured, for example `"remote": "origin"`. A repository can have
a Git `origin` remote while Cocodex still shows `remote: none` if it was
initialized without `--remote origin`; edit the config or reinitialize
intentionally before expecting remote sync.

`sync already in progress (publishing)` should be short-lived. If it persists,
restart the daemon and run `cocodex join <name>` for the affected session.
Startup normalization keeps active tasks in `fusing` when they can continue and
creates backup refs before clearing incomplete task startup state. If a legacy
session has no recorded baseline, Cocodex adopts a safe baseline only when Git
ancestry proves the session is merely ahead of or behind `main`; divergent
unknown-baseline sessions are refused for operator inspection.

Implementation details are documented in [docs/DEV.md](docs/DEV.md).
