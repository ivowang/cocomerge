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
- queue a dirty session for integration;
- after Cocodex gives the session a task, publish the committed candidate as
  the new `main`.

Developers and Codex sessions must not run `git pull main`, `git merge main`,
or `git push main` directly. Cocodex is the only writer to local `main`.

If a remote is configured, every `cocodex sync` also tries to force-sync the
server's local branch refs to that remote. The server is treated as the source
of truth; remote branch differences may be overwritten. Remote sync is
best-effort: network or authentication failures are reported as warnings and
retried on later `cocodex sync` commands.

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
`cocodex sync` force-pushes local branch refs to that remote with pruning, so
the server-side repository remains authoritative. Omit `--remote` for
local-only coordination.

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
requests, queue movement, integration lock changes, publish events, remote sync
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

Cocodex does not automatically infer a tmux pane, because `TMUX_PANE` can leak
through scripts, tests, or nested shells and target the wrong Codex. By default,
Cocodex prints the task and prompt file paths when a sync task starts. To paste
sync prompts directly into the Codex pane, opt in explicitly:

```bash
cocodex join --tmux-target "$TMUX_PANE" alice
```

Only pass `--tmux-target "$TMUX_PANE"` when running `join` from the same tmux
pane that will host that developer's Codex.

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
- a queued sync request is reported so Codex waits for the task;
- a clean session that only fell behind `main` is fast-forwarded;
- local unintegrated work is reported so Codex reviews it before starting
  unrelated work.

If a restart notice appears, handle that notice before accepting new feature
work. With an explicit `--tmux-target`, Cocodex also pastes the restart notice
into the Codex pane.

## What `sync` Does

### Clean Session

If Alice has no local work and `main` has advanced, this catches Alice up:

```bash
cocodex sync
```

If Alice is already current, Cocodex reports that the session is already
synced.

### Dirty Session

If Alice has local edits or commits, this requests integration:

```bash
cocodex sync
```

When Alice reaches the front of the queue, Cocodex:

1. freezes Alice's session;
2. snapshots Alice's current work;
3. resets Alice's worktree to the latest `main`;
4. writes a task file under `.cocodex/tasks/`;
5. prints the task file path inside Alice's Codex terminal.

Alice's Codex reads the task file and re-implements or semantically merges
Alice's feature on top of the latest `main`. If the task arrives while Codex is
working on another request, Codex should pause at a safe point, preserve that
request's remaining intent, complete the sync task, and then resume the paused
work after sync succeeds.

For each task, Codex designs and runs sufficient validation for the semantic
merge. That can mean existing tests, new or updated tests, targeted scripts, or
manual checks when the project has no suitable test framework. Before running
sync again, Codex writes the requested validation report under
`.cocodex/tasks/`. After it commits the final candidate and the worktree is
clean, it runs the same command again:

```bash
cocodex sync
```

Cocodex then requires the validation report, fast-forwards local `main`,
best-effort syncs the configured remote if one exists, and notifies other
sessions.

If the task cannot be completed safely, Codex should stop and explain the
blocker in its session output. An operator can inspect `cocodex status` and
`cocodex log` before deciding how to recover.

## Normal Example

Alice and Bob both start Codex through Cocodex. Alice implements feature A; Bob
implements feature B. Neither branch is integrated automatically.

Alice runs:

```bash
!cocodex sync
```

Cocodex gives Alice's Codex a task. Alice's Codex applies feature A on latest
`main`, commits, and runs:

```bash
!cocodex sync
```

Now feature A is the new `main`.

Bob later runs:

```bash
!cocodex sync
```

Bob's task is based on the current `main`, which already includes feature A.
Bob's Codex applies feature B on top of that, commits, and runs:

```bash
!cocodex sync
```

This gives the team a serial mainline even though the Codex sessions worked
asynchronously.

## Safety Behavior

Cocodex prefers stopping over guessing:

- a dirty session is not integrated until its owner runs `sync`;
- only one session owns the integration lock at a time;
- running `sync` before committing a task candidate is rejected;
- missing or insufficient validation reports keep the task locked so the same
  session can write the report and run `sync` again;
- remote sync failures do not block local progress; Cocodex warns and retries
  on the next `sync`;
- unexpected recovery states require operator inspection.

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
that is expected unless the session was started with an explicit
`--tmux-target`. Read the task file in the session worktree and follow it.

Remote sync warnings are non-fatal. Fix the network or Git authentication
problem when convenient; Cocodex retries remote synchronization on later
`cocodex sync` commands.

Implementation details are documented in [docs/DEV.md](docs/DEV.md).
