# coconut

[中文说明](docs/README_ZH.md)

coconut coordinates multiple Codex sessions working on the same Git repository
from one shared server account. Each developer gets an isolated managed
worktree. Coconut serializes the moments where those worktrees become the new
`main`.

## Read This First

Coconut is not a background auto-merge bot that understands code by itself. It
is a coordinator around Codex:

- The daemon watches managed worktrees, owns the integration lock, verifies
  candidates, moves local `main`, and optionally pushes the configured remote.
- Each developer works through a Codex process started by `coconut join`.
- When a session has changes, Coconut creates an integration task for that same
  session's Codex. That Codex must re-implement or semantically merge its work
  on top of the latest `main`.
- Publishing to `main` happens only after that session reports completion with
  `coconut done <session>`.

The important rule is simple: developers and Codex sessions do not pull, merge,
or push `main` directly. They edit their own Coconut worktree. Coconut is the
only process that writes local `main`.

## Who Runs Which Command

| Command | Usually run by | What it means |
| --- | --- | --- |
| `coconut init ...` | repo owner or team operator | Configure Coconut once for this repository. |
| `coconut daemon` | repo owner or team operator | Start the long-running coordinator. Keep one daemon running. |
| `coconut join --name alice -- codex` | developer Alice | Start Alice's Codex inside Alice's managed worktree. |
| `coconut ready alice` | Alice's Codex or Alice | Optional: tell the daemon Alice's current work should be queued now. |
| `coconut done alice` | Alice's Codex, after an active task is complete | Ask the daemon to verify and publish Alice's current candidate. |
| `coconut block alice "reason"` | Alice's Codex or Alice, after an active task cannot be completed | Mark the active task blocked and release the integration lock. |
| `coconut status` / `coconut log` | anyone on the shared account | Inspect state and recent events. |
| `coconut resume alice` / `coconut abandon alice` | operator or someone doing recovery | Explicitly recover from blocked or recovery states. |

`done` and `block` are not daemon-internal actions. They are explicit signals
sent by the owning Codex session or by the developer responsible for that
session. Running `done` means "the current HEAD of this session worktree is the
candidate new `main`; Coconut may verify and publish it."

## Installation

From the Coconut repository root:

```bash
pip install -e .
```

This installs the `coconut` console command.

## Repository Setup

Run these commands in the project repository that the team wants to develop
with Coconut, not in the Coconut source repository.

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

Use `--remote origin` only if that remote exists. If you only want local
coordination, omit `--remote`.

## Starting Work

Start one daemon in a long-running terminal from the project repository:

```bash
coconut daemon
```

Each developer starts Codex through Coconut:

```bash
coconut join --name alice -- codex
coconut join --name bob -- codex
```

Each `join` command creates or reuses:

- a branch named `coconut/<name>`;
- a worktree under `.coconut/worktrees/<name>`;
- a session agent that talks to the daemon.

The Codex process runs inside that managed worktree. Developers should ask
Codex to edit files there as usual.

## How Integration Is Triggered

There are two ways for a session to enter the integration queue:

1. Automatic trigger: the daemon scans managed worktrees every few seconds. If
   a session has uncommitted changes, staged changes, untracked files, or commits
   after its last seen `main`, Coconut marks it dirty and queues it.
2. Manual trigger: the owning Codex or developer can run:

   ```bash
   coconut ready alice
   ```

   This is only a queue request. It does not publish anything. If Alice has no
   changes, Coconut prints that there is nothing to integrate.

When the integration lock is free, the daemon picks the next queued session,
freezes it, snapshots its current work, resets that worktree to the latest
`main`, and prints a task file path inside that same Codex terminal:

```text
Coconut task for alice: /path/to/repo/.coconut/tasks/<task>.md
```

That task file is the handoff from Coconut to Codex. It contains:

- latest `main`;
- the session's last seen `main`;
- the snapshot commit;
- the diff that must be re-implemented or semantically merged;
- the verification command;
- the exact completion commands.

## What The Owning Codex Does With A Task

When Alice's Codex receives a task:

1. Read the task file.
2. Treat the current worktree as latest `main`.
3. Re-implement or semantically merge Alice's snapshot work on top of it.
4. Run the relevant checks.
5. Commit the final candidate.
6. Make sure the worktree is clean.
7. Run:

   ```bash
   coconut done alice
   ```

Coconut then verifies the candidate with the configured command, fast-forwards
local `main`, pushes `origin/main` if `--remote origin` was configured,
fast-forwards clean idle sessions, broadcasts `main_updated`, and starts the
next queued task.

If Alice's Codex cannot complete the task safely, it should run:

```bash
coconut block alice "semantic conflict with auth refactor"
```

That releases the integration lock and records the reason. A human can inspect
the state with `coconut status` and `coconut log`.

## Can I Ask Codex To Sync With Main?

Yes, but the instruction must use Coconut's workflow.

Good instruction inside Alice's joined Codex:

```text
Use Coconut to sync with main. Do not run git pull, git merge main, or git push
main directly. If we have local work, run coconut ready alice, wait for the
Coconut task, apply the task on latest main, commit the final candidate, and
then run coconut done alice.
```

For a clean session, there is usually nothing to do. When Coconut publishes a
new `main`, it automatically fast-forwards clean idle sessions.

For a dirty session, syncing with `main` means integration. Coconut will not
blindly pull `main` into the dirty worktree. It snapshots the dirty work,
resets the worktree to latest `main`, and asks that same Codex to recreate the
feature correctly on top of latest `main`.

## Normal Example

Alice and Bob both start Codex through Coconut:

```bash
coconut join --name alice -- codex
coconut join --name bob -- codex
```

Alice asks Codex to implement feature A. Bob asks Codex to implement feature B.
Both sessions become dirty.

Coconut queues both sessions. Suppose Alice gets the lock first. Alice's Codex
receives a task file, applies feature A on latest `main`, commits, and runs:

```bash
coconut done alice
```

Coconut verifies and publishes Alice's candidate as the new `main`.

Bob is still dirty, so Bob does not simply push his earlier branch. When Bob
gets the lock, Coconut snapshots Bob's work, resets Bob's worktree to the new
`main` that already contains feature A, and asks Bob's Codex to implement
feature B on top of that. Bob's Codex commits and runs:

```bash
coconut done bob
```

This is the serialization guarantee: feature B is integrated after feature A,
using the actual post-A mainline as its base.

## Recovery Behavior

Coconut prefers stopping over guessing:

- if the lock owner disconnects, the session enters `recovery_required`;
- if the daemon restarts during integration, incomplete states are recovered
  conservatively;
- if local `main` moves outside Coconut, dirty or active sessions enter
  `recovery_required`;
- if local `main` advanced but remote push failed, Coconut keeps the lock so the
  same task can retry `coconut done <session>` after the remote issue is fixed;
- if verification fails, the task becomes `blocked`.

Use:

```bash
coconut status
coconut log
```

Then choose one explicit action:

```bash
coconut done alice
coconut block alice "reason"
coconut resume alice
coconut abandon alice
```

## Command Reference

```bash
coconut init --main main --verify "pytest" --remote origin
coconut daemon
coconut join --name alice -- codex
coconut ready alice
coconut status
coconut log
coconut done alice
coconut block alice "reason"
coconut resume alice
coconut abandon alice
```

Implementation details are documented in [docs/DEV.md](docs/DEV.md).
