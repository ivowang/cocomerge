# coconut

[中文说明](docs/README_ZH.md)

coconut coordinates multiple Codex sessions working on the same Git repository
from one shared server account. It turns asynchronous Codex-based development
into a serialized integration workflow: every developer keeps an isolated
worktree, while one daemon freezes dirty sessions, creates integration tasks,
publishes verified results to `main`, and keeps clean sessions synchronized.

## Why Coconut Exists

Vibe coding changes how teams collaborate. Several developers may each run
their own Codex session at the same time, but a shared repository still needs a
single coherent mainline. Coconut provides that coordination layer for a
single-machine team setup:

- each developer works in a managed session worktree;
- Coconut detects dirty sessions and queues them;
- only one session owns the integration lock at a time;
- that session's Codex receives a task file describing what to re-implement or
  semantically merge on top of the latest `main`;
- Coconut verifies the final candidate, fast-forwards `main`, optionally pushes
  a remote, and notifies the other sessions.

## Current Model

Coconut is cooperative. It does not replace Codex and it does not resolve
semantic conflicts by itself. The owning Codex session is still responsible for
understanding its feature and producing the final candidate commit.

The intended deployment model is:

- one shared server account;
- one Git repository on that server;
- one long-running `coconut daemon`;
- all participating Codex sessions started through `coconut join`;
- Coconut is the only writer to local `main`.

Coconut currently uses local Git state, SQLite, and Unix domain sockets. It is
not a distributed lock manager and does not coordinate multiple machines.

## Installation

From the repository root:

```bash
pip install -e .
```

The package exposes a `coconut` console command.

## Team Workflow

Initialize Coconut once in the Git repository:

```bash
coconut init --main main --verify "pytest" --remote origin
```

The configured main branch must already exist and have an initial commit. If
you use `--remote origin`, that remote must already exist as well. For a fresh
repository:

```bash
git switch -c main
git add .
git commit -m "initial commit"
git remote add origin <url>  # only when you want Coconut to push a remote
```

Start the daemon in a long-running terminal:

```bash
coconut daemon
```

Each developer starts Codex through Coconut:

```bash
coconut join --name alice -- codex
coconut join --name bob -- codex
```

Each `join` command creates or reuses a managed worktree under `.coconut/` and
runs the requested command inside that worktree.

When a session becomes dirty, Coconut queues it. When it reaches the front of
the queue, the daemon sends that session a freeze command, snapshots its work,
resets the worktree to the latest `main`, and prints an integration task path
inside the owning Codex session.

The task file tells Codex:

- the latest `main` commit;
- the session's last seen main;
- the snapshot commit;
- the diff that must be re-implemented or semantically merged;
- the verification command;
- how to report completion or blocking.

After Codex finishes the integration and commits the candidate:

```bash
coconut done alice
```

If Codex cannot complete the integration safely:

```bash
coconut block alice "semantic conflict with auth refactor"
```

On success, Coconut verifies the candidate, fast-forwards local `main`, pushes
the configured remote if one is set, broadcasts `main_updated`, and starts the
next queued integration.

## Commands

```bash
coconut init --main main --verify "pytest" --remote origin
coconut daemon
coconut join --name alice -- codex
coconut status
coconut log
coconut resume alice
coconut abandon alice
coconut done alice
coconut block alice "reason"
```

Command summary:

- `init`: create `.coconut/config.json` and initialize daemon state.
- `daemon`: run the queue processor, socket server, heartbeat checks, recovery
  checks, publishing path, and main-update broadcasts.
- `join`: create or reuse a session worktree, start the session agent, register
  with the daemon, and run the requested command.
- `status`: show repository main, session state, queue, lock, and connection
  metadata.
- `log`: print recent Coconut state events.
- `resume`: requeue a blocked or recovery-required session when it no longer
  owns the integration lock.
- `abandon`: abandon a session task and clear the matching lock when safe.
- `done`: ask the daemon to verify and publish the session's current candidate.
- `block`: mark an active integration blocked and release the lock.

## Recovery Behavior

Coconut prefers stopping over guessing:

- if a session holding the integration lock disconnects, it enters
  `recovery_required`;
- if the daemon restarts during an integration, incomplete states are recovered
  conservatively;
- if local `main` moves outside Coconut, queued or active dirty sessions enter
  `recovery_required`;
- if local `main` has advanced but the remote push failed, Coconut keeps the
  lock so the same task can retry publishing after the remote issue is fixed.

Use `coconut status` and `coconut log` to inspect the state before choosing
between `resume`, `done`, `block`, or `abandon`.

## Repository Contents

This public tree contains the Coconut runtime package and user/developer
documentation. Internal planning artifacts and implementation test suites are
not part of the published tree.

Implementation details are documented in [docs/DEV.md](docs/DEV.md).
