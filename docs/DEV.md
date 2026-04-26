# Coconut Developer Guide

This document explains Coconut's implementation model for maintainers. The
user-facing workflow is documented in the root [README.md](../README.md).

## Architecture

Coconut is a single-machine orchestration layer around Git and Codex. It has
four main parts:

- CLI commands in `src/coconut/cli.py`.
- Persistent state in `src/coconut/state.py`, backed by SQLite under
  `.coconut/state.sqlite`.
- Daemon orchestration in `src/coconut/daemon.py`.
- Session-side cooperation in `src/coconut/agent.py`.
- Session worktree setup in `src/coconut/session.py`, including generated
  Coconut guidance for Codex.

The daemon and session agents communicate over Unix domain sockets with JSONL
messages. Git operations are delegated to the Git CLI through helpers in
`src/coconut/git.py`.

## Generated Session Instructions

`ensure_session_worktree()` writes an `AGENTS.md` file into each managed
worktree. The file tells Codex that it is working inside a Coconut session,
names the session branch and main branch, and explains the `ready`, `done`, and
`block` workflow.

The generated file must not create integration work by itself. Coconut adds
`/AGENTS.md` to the repository's local `.git/info/exclude` before writing it,
so Git status, snapshots, and `git add -A` ignore the file. If the project
already has its own `AGENTS.md`, Coconut leaves it untouched instead of
overwriting project instructions.

## State Model

Each session is represented by a `SessionRecord`:

- `name`: stable session id, such as `alice`.
- `branch`: managed session branch, usually `coconut/<name>`.
- `worktree`: path to the managed Git worktree.
- `state`: lifecycle state.
- `last_seen_main`: last main commit known to be reflected in the session.
- `active_task`: current integration task id, if any.
- `blocked_reason`: human-readable block or recovery reason.
- `pid`, `control_socket`, `last_heartbeat`, `connected`: runtime metadata.

SQLite also stores:

- a FIFO queue of sessions waiting for integration;
- the global integration lock;
- key/value metadata such as `last_observed_main`;
- an event log for status and debugging.

The lock and `active_task` must stay consistent. Queue processing uses
`claim_integration_task()` so the session task id and lock owner are recorded in
one SQLite transaction.

## Session States

Important states:

- `clean`: no pending work relative to the session's known main.
- `dirty`: local changes or commits need integration.
- `queued`: waiting for the daemon to start integration.
- `snapshot`: the daemon is preparing a snapshot.
- `frozen`: the session acknowledged freeze.
- `fusing`: the owning Codex is applying the snapshot on top of latest `main`.
- `verifying`: Coconut is validating the candidate.
- `publishing`: Coconut is moving `main` and optionally pushing remote.
- `blocked`: a semantic or verification failure requires human/Codex action.
- `recovery_required`: Coconut stopped because continuing automatically could
  lose work or mis-publish state.
- `abandoned`: the session task was manually abandoned.

## Control Protocol

Session to daemon:

- `register`: attach a session agent and runtime metadata.
- `heartbeat`: keep the session connected.
- `shutdown`: mark the session disconnected.
- `ready_to_integrate`: request queueing. The public CLI exposes this as
  `coconut ready <session>`. The daemon queues the session only when the
  session has work to integrate.
- `fusion_done`: report that the current candidate is ready to verify/publish.
- `fusion_blocked`: report that the Codex session cannot safely integrate.

Daemon to session:

- `freeze`: ask the agent to stop accepting new work for this integration
  window.
- `start_fusion`: tell the agent to show the generated task file path.
- `main_updated`: notify the session that local `main` advanced.

`src/coconut/protocol.py` validates message shape, and
`src/coconut/transport.py` implements JSONL socket transport.

## Queue and Integration Flow

The daemon loop performs:

1. heartbeat timeout detection;
2. external `main` movement detection;
3. dirty session scanning;
4. one queue processing attempt.

`process_queue_once()` only starts a task if the integration lock is free. It
claims the lock and active task together, sends `freeze`, prepares the snapshot,
resets the session worktree to latest `main`, writes a task file, then sends
`start_fusion`.

The task file is created by `src/coconut/tasks.py`. It includes the snapshot
commit, latest main, last seen main, diff summary, verification command, and
completion instructions. The generated instructions name the concrete CLI
commands, `coconut done <session>` and `coconut block <session> "<reason>"`.

## Publishing Flow

`publish_candidate()` is called after `fusion_done`.

It checks:

- session and task id match;
- integration lock is owned by the same session/task;
- session state allows publishing or retrying a recovery publish;
- worktree has no unsafe Git operation;
- reported candidate equals session `HEAD`;
- worktree is clean before verification;
- verification passes;
- verification did not modify `HEAD` or dirty the worktree;
- local `main` can fast-forward to the candidate.

After local publish, Coconut optionally pushes the configured remote. Remote
failure after local publish is handled conservatively: the session enters
`recovery_required`, the lock stays held, and the same task can retry after the
remote issue is resolved.

On successful publish, Coconut:

1. marks the session clean;
2. updates `last_seen_main`;
3. releases the lock;
4. records `last_observed_main`;
5. fast-forwards clean idle sessions;
6. broadcasts `main_updated`.

## Recovery Semantics

Coconut deliberately stops rather than guessing.

Heartbeat timeout:

- stale connected sessions are marked disconnected;
- if a stale session owns the lock, it becomes `recovery_required` and the lock
  is retained for explicit recovery.

Startup recovery:

- incomplete integration states become `recovery_required`;
- inconsistent owner locks are adopted into the owning session's `active_task`
  so they can be abandoned or inspected instead of orphaned.

External main detection:

- compares local `main` against `last_observed_main`;
- if `main` moved outside Coconut, dirty/queued/active integration sessions
  become `recovery_required`;
- pending Coconut-owned local publish recovery is not misclassified as external
  movement when local `main` equals the locked session candidate.

Manual recovery commands:

- `resume` requeues blocked/recovery sessions only when they do not still own
  the integration lock.
- `abandon` marks a session abandoned, dequeues it, and clears a matching or
  orphaned owner lock.
- `done` can retry a locked recovery publish when the same task remains active.

## Development Notes

The public release tree intentionally excludes the internal implementation test
suite and planning artifacts. Maintainers should validate changes in a
development checkout before producing a clean public release tree.

Useful local checks when the validation suite is present:

```bash
pytest -q
PYTHONPATH=src python3 -m coconut --help
git diff --check HEAD
```

Before publishing, verify that the public tree contains only:

- `src/coconut/`;
- `pyproject.toml`;
- `README.md`;
- `docs/README_ZH.md`;
- `docs/DEV.md`;
- `docs/DEV_ZH.md`;
- supporting project metadata such as `.gitignore`.

Do not publish `.coconut/`, `.pytest_cache/`, `__pycache__/`, internal planning
documents, or the implementation test suite unless the release policy changes.
