# Cocodex Developer Guide

This document explains Cocodex's implementation model for maintainers. The
user-facing workflow is documented in the root [README.md](../README.md).

## Architecture

Cocodex is a single-machine orchestration layer around Git and Codex. Its main
parts are:

- CLI commands in `src/cocodex/cli.py`.
- Persistent state in `src/cocodex/state.py`, backed by SQLite under
  `.cocodex/state.sqlite`.
- Daemon orchestration in `src/cocodex/daemon.py`.
- Session-side cooperation in `src/cocodex/agent.py`.
- Session worktree setup in `src/cocodex/session.py`, including generated
  Cocodex guidance for Codex and per-worktree Git identity configuration.

The daemon and session agents communicate over Unix domain sockets with JSONL
messages. Git operations are delegated to the Git CLI through helpers in
`src/cocodex/git.py`.

`SessionAgent` can paste sync prompts into tmux, but only when `join` receives
an explicit `--tmux-target`. Cocodex deliberately does not auto-detect
`TMUX_PANE`: tests, wrapper scripts, and nested shells can inherit that
environment variable from the wrong Codex. On `start_fusion`, the agent always
writes a prompt file next to the task file and prints both paths; if a target
was configured, it also uses `tmux load-buffer`, `paste-buffer`, and
`send-keys Enter`.

## Configuration

`cocodex init` writes `.cocodex/config.json` through `init_config()` in
`src/cocodex/config.py`. The public config schema is:

- `main_branch`: local branch Cocodex is allowed to publish.
- `remote`: optional remote name for best-effort server-ref sync.
- `socket_path`: daemon Unix socket path.
- `worktree_root`: root for managed session worktrees.
- `dirty_interval_s`: retained timing knob for daemon polling.
- `developers`: object keyed by developer/session name.

Each developer entry must provide `git_user_name` and `git_user_email` before
that developer can use `cocodex join <name>`. The optional `command` field is a
non-empty JSON string array and defaults to `["codex"]`. `validate_config()`
checks remote existence, main branch existence, developer object shape, and
custom command shape. Identity fields are required by `join`, not at daemon
startup, so operators can add developers incrementally.

`init_config()` refuses to overwrite an existing config unless `force=True`
from `cocodex init --force`. Config writes use a temporary file followed by
atomic replace so a failed write does not leave a partially written JSON file.

`load_config()` accepts only the public config schema above. Unknown keys are
reported as configuration errors, so obsolete or misspelled settings do not
silently affect a session. Cocodex does not store a repo-wide verification
command: the generated sync task requires the owning Codex to design and run
suitable validation for that semantic merge.

## Product Command Model

The normal developer command is `cocodex sync`, run from inside the managed
worktree. The CLI infers the session by matching the current Git worktree root
against registered `SessionRecord.worktree` values. `sync` intentionally takes
no session name, so a developer cannot accidentally request synchronization for
another user's worktree.

Internally, `sync` maps to different protocol actions:

- no active task: request queueing with `ready_to_integrate`;
- active task in `fusing` or retryable `blocked`: report `fusion_done` and let
  the daemon validate/publish the current session `HEAD`;
- retryable remote publish recovery: report `fusion_done` again to retry the
  publish path.

Before the protocol action, and again after successful local catch-up or
publish paths, the CLI attempts a best-effort remote sync when `config.remote`
is set. This force-pushes/prunes local `refs/heads/*` to the remote and also
pushes Cocodex's internal `refs/cocodex/*` namespace when it exists. Failures
or timeouts only produce warnings; they must not change the sync exit status.

`resume` and `abandon` are operator recovery commands. They are intentionally
kept out of the top-level help and are not part of the normal developer
workflow.

The daemon does not automatically queue dirty sessions. Dirty work stays local
until the owning session explicitly runs `sync`.

`join <name>` resolves the developer from `config.developers[name]`. Cocodex
uses that entry's `git_user_name` and `git_user_email`, enables Git
`extensions.worktreeConfig`, and writes `user.name`/`user.email` with
`git config --worktree`, so each developer's managed worktree can commit with a
distinct identity under the shared server account. If the entry has no
`command`, Cocodex starts `codex`; otherwise it uses the configured JSON string
array. The CLI no longer accepts Git identity overrides; configuration is the
single source of truth for per-developer identity and launch command.

On every `join`, Cocodex calls `prepare_join_startup_notice()` before launching
the session command. This makes restart behavior explicit:

- active tasks are re-announced with task and validation paths;
- recoverable `recovery_required` active tasks with an existing task file and
  matching integration lock are moved back to `fusing`;
- queued sync requests produce a wait-for-task notice;
- clean sessions that are only behind `main` are fast-forwarded;
- local unintegrated work produces a review-before-new-work notice.

`SessionAgent` prints this startup notice after the child command starts. If an
explicit tmux target was configured, it also pastes the notice into that pane.

## Generated Session Instructions

`ensure_session_worktree()` writes an `AGENTS.md` file into each managed
worktree. The file tells Codex that it is working inside a Cocodex session and
that normal collaboration uses `cocodex sync` from inside that worktree.

The generated file must not create integration work by itself. Cocodex adds
`/AGENTS.md` to the repository's local `.git/info/exclude` before writing it,
so Git status, snapshots, and `git add -A` ignore the file. If the project
already has its own `AGENTS.md`, Cocodex leaves it untouched.

## State Model

Each session is represented by a `SessionRecord`:

- `name`: stable session id, such as `alice`.
- `branch`: managed session branch, usually `cocodex/<name>`.
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
- `dirty`: local changes or commits need integration. This is entered by
  explicit sync paths, not by daemon auto-queueing.
- `queued`: waiting for the daemon to start integration.
- `snapshot`: the daemon is preparing a snapshot.
- `frozen`: the session acknowledged freeze.
- `fusing`: the owning Codex is applying the snapshot on top of latest `main`.
- `verifying`: Cocodex is validating the candidate.
- `publishing`: Cocodex is moving `main` and optionally pushing remote.
- `blocked`: the active sync task needs the same session to fix and rerun
  `sync`, or an operator to inspect it.
- `recovery_required`: Cocodex stopped because continuing automatically could
  lose work or mis-publish state.
- `abandoned`: the session task was manually abandoned.

## Control Protocol

Session to daemon:

- `register`: attach a session agent and runtime metadata.
- `heartbeat`: keep the session connected.
- `shutdown`: mark the session disconnected.
- `ready_to_integrate`: internal queue request used by `cocodex sync`.
- `fusion_done`: internal candidate-ready signal used by `cocodex sync`.

Daemon to session:

- `freeze`: ask the agent to stop accepting new work for this integration
  window.
- `start_fusion`: tell the agent to show the generated task file path.
- `main_updated`: notify the session that local `main` advanced.

`src/cocodex/protocol.py` validates message shape, and
`src/cocodex/transport.py` implements JSONL socket transport.

## Queue and Integration Flow

The daemon loop performs:

1. heartbeat timeout detection;
2. external `main` movement detection;
3. one queue processing attempt.

`process_queue_once()` only starts a task if the integration lock is free. It
claims the lock and active task together, sends `freeze`, prepares the snapshot,
stores snapshot/base refs under `refs/cocodex/`, resets the session worktree to
latest `main`, writes a task file, then sends `start_fusion`.

The task file is created by `src/cocodex/tasks.py`. It includes the snapshot
commit, latest main, last seen main, diff summary, interruption-handling
guidance, validation-report requirements, and the instruction to run
`cocodex sync` again from the same worktree after committing the candidate.

## Publishing Flow

`publish_candidate()` is called when active-task `sync` reports completion.

It checks:

- session and task id match;
- integration lock is owned by the same session/task;
- recovery retry is limited to remote-push recovery or startup-publishing
  recovery;
- worktree has no unsafe Git operation;
- reported candidate equals session `HEAD`;
- candidate is not the task base commit, unless Codex created an explicit
  no-op commit;
- worktree is clean before validation;
- the task validation report exists and has meaningful content;
- validation did not coincide with `HEAD` changes or dirty the worktree;
- local `main` can fast-forward to the candidate.

After local publish, Cocodex records `last_observed_main`, marks the session
clean, releases the lock, fast-forwards clean idle sessions, and broadcasts the
main update. If a remote is configured, Cocodex then attempts a best-effort
server-ref sync. Remote failure after local publish is non-fatal: Cocodex
records a `remote_sync_failed` event and retries on later `sync` commands.

On successful publish, Cocodex:

1. marks the session clean;
2. updates `last_seen_main`;
3. releases the lock;
4. fast-forwards clean idle sessions;
5. broadcasts `main_updated`.

## Recovery Semantics

Cocodex deliberately stops rather than guessing.

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
- if `main` moved outside Cocodex, dirty/queued/active integration sessions
  become `recovery_required`;
- pending Cocodex-owned local publish recovery is not misclassified as external
  movement when local `main` equals the locked session candidate.

Manual recovery commands are intentionally operator-only.

## Development Notes

The public release tree intentionally excludes the internal implementation test
suite and planning artifacts. Maintainers should validate changes in a
development checkout before producing a clean public release tree.

Useful local checks when the validation suite is present:

```bash
pytest -q
PYTHONPATH=src python3 -m cocodex --help
git diff --check HEAD
```

## PyPI Release

Cocodex uses `setup.cfg` as the single packaging metadata source.
`pyproject.toml` only declares the build backend, and `setup.py` is only a
compatibility shim that calls `setup()`. Do not add version or package metadata
to `pyproject.toml` or `setup.py`.

Publishing is handled by `.github/workflows/release.yml`. The workflow runs when
a `v*.*.*` tag is pushed. It builds the wheel and sdist, checks them with
`twine`, verifies that the tag version matches `metadata.version`, and publishes
to PyPI through Trusted Publishing. No PyPI API token should be stored in GitHub
Secrets for the normal release path.

One-time setup before the first release:

1. In GitHub, create the environment `pypi` under repository
   `Settings -> Environments`.
2. Add `Required reviewers` under the `pypi` environment's deployment
   protection rules. If the repository has only one maintainer, do not enable
   `Prevent self-review`; otherwise the publish job can be left waiting forever.
3. In PyPI, configure a project or pending publisher for project `cocodex`
   with owner `ivowang`, repository `cocodex`, workflow `release.yml`, and
   environment `pypi`.

Release steps:

```bash
# edit setup.cfg metadata.version first
python -m pip install --upgrade build twine
rm -rf dist build *.egg-info src/*.egg-info
python -m build
python -m twine check --strict dist/*
git add setup.cfg
git commit -m "Release 0.1.1"
git tag v0.1.1
git push origin main
git push origin v0.1.1
```

After the tag push, open the GitHub Actions run. The build job should complete
without approval; the publish job waits on the `pypi` environment. Approve the
deployment from `Review deployments` to publish the already-built artifacts to
PyPI. PyPI files are immutable, so never reuse a version after a successful
upload.

Before publishing, verify that the public tree contains only:

- `src/cocodex/`;
- `.github/workflows/release.yml`;
- `MANIFEST.in`;
- `pyproject.toml`;
- `setup.cfg`;
- `setup.py`;
- `README.md`;
- `docs/README_ZH.md`;
- `docs/DEV.md`;
- `docs/DEV_ZH.md`;
- supporting project metadata such as `.gitignore`.

Do not publish `.cocodex/`, `.pytest_cache/`, `__pycache__/`, internal planning
documents, or the implementation test suite unless the release policy changes.
