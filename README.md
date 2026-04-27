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

The daemon does not automatically integrate dirty sessions. Local work stays in
the developer's managed worktree until that developer or their Codex explicitly
runs `coconut sync` from inside that worktree. In Codex, run it as a shell
command, for example `!coconut sync`.

## Roles

Operator/startup commands:

```bash
coconut init --main main --verify "pytest" --remote origin
coconut daemon
coconut join --name alice \
  --git-user-name "Alice Example" \
  --git-user-email alice@example.com \
  -- codex
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

If Coconut should push a remote after publishing local `main`, add the remote
before initialization:

```bash
git remote add origin <url>
```

Initialize Coconut once:

```bash
coconut init --main main --verify "pytest" --remote origin
```

Use `--remote origin` only if that remote exists. Omit `--remote` for local-only
coordination.

## Starting Codex Sessions

Start one daemon in a long-running terminal from the project repository:

```bash
coconut daemon
```

Start each Codex session through Coconut from that developer's tmux window:

```bash
coconut join --name alice \
  --git-user-name "Alice Example" \
  --git-user-email alice@example.com \
  -- codex

coconut join --name bob \
  --git-user-name "Bob Example" \
  --git-user-email bob@example.com \
  -- codex
```

Each joined session gets:

- a branch named `coconut/<name>`;
- a worktree under `.coconut/worktrees/<name>`;
- a session agent that receives Coconut tasks;
- a Git-ignored `AGENTS.md` in that worktree, unless the project already has
  its own `AGENTS.md`.

`join` writes the supplied Git identity into that worktree's per-worktree Git
config, so Coconut snapshot commits and Codex candidate commits have the right
author. If identity is not supplied, the worktree must already have effective
`user.name` and `user.email` Git config.

When `join` runs inside tmux, Coconut detects the current pane and later pastes
sync prompts directly into the Codex running in that pane. Use `--tmux-target`
to choose a pane explicitly, or `--no-auto-prompt` to disable prompt injection.
Without tmux, Coconut still prints the task and prompt file paths.

The generated `AGENTS.md` tells Codex that it is in a Coconut-managed
collaboration session and that normal synchronization uses only
`coconut sync` from inside the managed worktree.

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
Alice's feature on top of the latest `main`. After it commits the final
candidate and the worktree is clean, it runs the same command again:

```bash
coconut sync
```

Coconut then verifies the candidate, fast-forwards local `main`, pushes the
configured remote if one exists, and notifies other sessions.

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
- verification failures keep the task locked so the same session can fix and
  run `sync` again;
- remote push failures keep the task locked so `sync` can retry after the
  remote issue is fixed;
- unexpected recovery states require operator inspection.

## Command Reference

Normal developer command:

```bash
coconut sync
```

Common operator commands:

```bash
coconut init --main main --verify "pytest" --remote origin
coconut daemon
coconut join --name alice --git-user-name "Alice Example" --git-user-email alice@example.com -- codex
coconut status
coconut log
```

Implementation details are documented in [docs/DEV.md](docs/DEV.md).
