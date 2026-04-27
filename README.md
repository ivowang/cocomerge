# coconut

[中文说明](docs/README_ZH.md)

coconut coordinates multiple Codex sessions that share one Git repository on one
server account. It gives every developer an isolated managed worktree, then
serializes the moment when a developer's work becomes the next `main`.

## Core Rule

For day-to-day collaboration, a developer only needs one Coconut command:

```bash
coconut sync
```

`sync` means "move this Coconut session to the next safe synchronization
state." Depending on the session state, it can:

- fast-forward a clean session to the latest `main`;
- queue a dirty session for integration;
- after Coconut gives the session a task, publish the committed candidate as
  the new `main`.

Developers and Codex sessions must not run `git pull main`, `git merge main`,
or `git push main` directly. Coconut is the only writer to local `main`.

If a remote is configured, every `coconut sync` also tries to force-sync the
server's local branch refs to that remote. The server is treated as the source
of truth; remote branch differences may be overwritten. Remote sync is
best-effort: network or authentication failures are reported as warnings and
retried on later `coconut sync` commands.

The daemon does not automatically integrate dirty sessions. Local work stays in
the developer's managed worktree until that developer or their Codex explicitly
runs `coconut sync` from inside that worktree. In Codex, run it as a shell
command, for example `!coconut sync`.

## Roles

Operator/startup commands:

```bash
coconut init --main main --remote origin
coconut daemon
coconut join alice
```

Developer collaboration command:

```bash
coconut sync
```

Inspection commands:

```bash
coconut status
coconut log
```

Recovery commands exist for operators, but they are not part of the normal
developer workflow.

## Installation

From the Coconut repository root:

```bash
pip install -e .
```

This installs the `coconut` console command.

## Repository Setup

Run these commands in the project repository that the team wants to develop
with Coconut.

The configured main branch must already exist and have an initial commit:

```bash
git switch -c main
git add .
git commit -m "initial commit"
```

If Coconut should keep a remote copy of the server's local branches, add the
remote before initialization:

```bash
git remote add origin <url>
```

Initialize Coconut once:

```bash
coconut init --main main --remote origin
```

Use `--remote origin` only if that remote exists. With a remote configured,
`coconut sync` force-pushes local branch refs to that remote with pruning, so
the server-side repository remains authoritative. Omit `--remote` for
local-only coordination.

Before developers join, add a top-level `developers` object to
`.coconut/config.json` while keeping the other keys that `coconut init` wrote:

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
  }
}
```

`command` is optional per developer; when omitted, Coconut starts `codex`. For
a custom Codex launch, use a JSON string array such as
`"command": ["codex", "--model", "gpt-5.5"]`.

## Starting Codex Sessions

Start one daemon in a long-running terminal from the project repository:

```bash
coconut daemon
```

Start each Codex session through Coconut from that developer's tmux window:

```bash
coconut join alice
coconut join bob
```

Each joined session gets:

- a branch named `coconut/<name>`;
- a worktree under `.coconut/worktrees/<name>`;
- a session agent that receives Coconut tasks;
- a Git-ignored `AGENTS.md` in that worktree, unless the project already has
  its own `AGENTS.md`.

`join` reads the developer's Git identity from `.coconut/config.json` and writes
it into that worktree's per-worktree Git config, so Coconut snapshot commits
and Codex candidate commits have the right author.

Coconut does not automatically infer a tmux pane, because `TMUX_PANE` can leak
through scripts, tests, or nested shells and target the wrong Codex. By default,
Coconut prints the task and prompt file paths when a sync task starts. To paste
sync prompts directly into the Codex pane, opt in explicitly:

```bash
coconut join --tmux-target "$TMUX_PANE" alice
```

Only pass `--tmux-target "$TMUX_PANE"` when running `join` from the same tmux
pane that will host that developer's Codex.

The generated `AGENTS.md` tells Codex that it is in a Coconut-managed
collaboration session and that normal synchronization uses only
`coconut sync` from inside the managed worktree.

## Restarting A Session

If a developer closes their Codex window, restart with the same session name:

```bash
coconut join alice
```

Coconut reuses `.coconut/worktrees/alice` and `coconut/alice`. On startup,
`join` checks for unfinished Coconut responsibilities before normal
development continues:

- an active sync task is re-announced with its task and validation file paths;
- a safely recoverable interrupted task is moved back to `fusing`;
- a queued sync request is reported so Codex waits for the task;
- a clean session that only fell behind `main` is fast-forwarded;
- local unintegrated work is reported so Codex reviews it before starting
  unrelated work.

If a restart notice appears, handle that notice before accepting new feature
work. With an explicit `--tmux-target`, Coconut also pastes the restart notice
into the Codex pane.

## What `sync` Does

### Clean Session

If Alice has no local work and `main` has advanced, this catches Alice up:

```bash
coconut sync
```

If Alice is already current, Coconut reports that the session is already
synced.

### Dirty Session

If Alice has local edits or commits, this requests integration:

```bash
coconut sync
```

When Alice reaches the front of the queue, Coconut:

1. freezes Alice's session;
2. snapshots Alice's current work;
3. resets Alice's worktree to the latest `main`;
4. writes a task file under `.coconut/tasks/`;
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
`.coconut/tasks/`. After it commits the final candidate and the worktree is
clean, it runs the same command again:

```bash
coconut sync
```

Coconut then requires the validation report, fast-forwards local `main`,
best-effort syncs the configured remote if one exists, and notifies other
sessions.

If the task cannot be completed safely, Codex should stop and explain the
blocker in its session output. An operator can inspect `coconut status` and
`coconut log` before deciding how to recover.

## Normal Example

Alice and Bob both start Codex through Coconut. Alice implements feature A; Bob
implements feature B. Neither branch is integrated automatically.

Alice runs:

```bash
!coconut sync
```

Coconut gives Alice's Codex a task. Alice's Codex applies feature A on latest
`main`, commits, and runs:

```bash
!coconut sync
```

Now feature A is the new `main`.

Bob later runs:

```bash
!coconut sync
```

Bob's task is based on the current `main`, which already includes feature A.
Bob's Codex applies feature B on top of that, commits, and runs:

```bash
!coconut sync
```

This gives the team a serial mainline even though the Codex sessions worked
asynchronously.

## Safety Behavior

Coconut prefers stopping over guessing:

- a dirty session is not integrated until its owner runs `sync`;
- only one session owns the integration lock at a time;
- running `sync` before committing a task candidate is rejected;
- missing or insufficient validation reports keep the task locked so the same
  session can write the report and run `sync` again;
- remote sync failures do not block local progress; Coconut warns and retries
  on the next `sync`;
- unexpected recovery states require operator inspection.

## Command Reference

Normal developer command:

```bash
coconut sync
```

Common operator commands:

```bash
coconut init --main main --remote origin
coconut daemon
coconut join alice
coconut status
coconut log
```

Implementation details are documented in [docs/DEV.md](docs/DEV.md).
