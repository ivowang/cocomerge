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
- Main branch protection in `src/cocodex/guard.py`.

The daemon and session agents communicate over Unix domain sockets with JSONL
messages. Git operations are delegated to the Git CLI through helpers in
`src/cocodex/git.py`.

`init` and daemon startup install Cocodex-managed Git hooks in the repository's
common hooks directory. `reference-transaction` blocks ordinary local updates
to `refs/heads/<main>`, `pre-push` blocks direct pushes of `main`, and
pre-commit/rebase/merge hooks give earlier errors for common commands.
Cocodex's own main writes and scoped remote pushes set
`COCODEX_INTERNAL_WRITE=1` through the Git helper. This prevents accidental Git
CLI bypasses; it does not defend against someone deliberately editing `.git`
files directly.

The daemon socket is addressed through the configured `socket_path`. If that
path is too long for Linux `AF_UNIX`, transport writes a small pointer file at
the configured path and binds the real socket under the system temporary
directory. Per-session control sockets always use short runtime paths keyed by
repository hash and session name. This avoids path length failures when a
project lives under a deep worktree or CI temporary directory.

`SessionAgent` can paste sync prompts into tmux. `join` defaults the target to
the current `TMUX_PANE` when that environment variable is present, which
matches the product constraint that developers start Codex through Cocodex from
their own tmux pane. `--tmux-target` can override the detected target for
advanced launchers. On `start_fusion`, the agent always writes a prompt file
next to the task file and prints both paths; if a target is available, it also
uses `tmux load-buffer` and `paste-buffer` to place the prompt in the Codex
input box. It deliberately does not send Enter; the developer reviews the
pasted prompt and presses Enter to start the task. Production semantic task
startup requires successful prompt delivery to the pane. Test harnesses can set
`COCODEX_HEADLESS_PROMPT_OK=1` to treat the prompt file as delivered.

## Configuration

`cocodex init` writes `.cocodex/config.json` through `init_config()` in
`src/cocodex/config.py`. The public config schema is:

- `main_branch`: local branch Cocodex is allowed to publish.
- `remote`: optional remote name for best-effort scoped sync of `main_branch`
  and the current session branch.
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
`init_config()` also adds `/.cocodex/` to `.git/info/exclude` and installs the
main guard hooks. Daemon startup repeats both checks so existing repositories
are upgraded when a newer Cocodex daemon starts.

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

- no active task and no local changes: catch up a clean session to latest
  `main` when possible;
- no active task, local changes, and `main == last_seen_main`: publish the
  current session branch directly under the integration lock;
- no active task, local changes, and `main != last_seen_main`: call
  `ready_to_integrate`, which either publishes through a clean Git merge,
  starts a semantic task in the same request, or refuses with a detailed reason;
- active task in any recoverable same-session state: report `fusion_done`; the
  daemon normalizes legacy/interrupted states when safe, then validates and
  publishes the current session `HEAD` or refuses with same-session next steps.

After a successful local catch-up, publish, or task start, the CLI attempts a
best-effort remote sync when `config.remote` is set. This force-pushes local
`main_branch`, the current session branch, and any `refs/cocodex/*` recovery
refs to the remote. It does not push, prune, or fast-forward other developer
branches. Refused syncs do not mutate remote refs. Remote failures or timeouts
only produce warnings; they must not change the sync exit status.

There are no manual recovery commands. `sync` is the recovery surface for the
owning session. `status` and `log` are read-only diagnostics. `delete <name>`
is an operator maintenance command for retired sessions, not a recovery command
for active sync tasks.

The daemon does not automatically queue dirty sessions. Dirty work stays local
until the owning session explicitly runs `sync`.

Direct publish is deliberately limited to sessions whose recorded
`last_seen_main` still equals current local `main`. If the worktree has
uncommitted changes, Cocodex creates a snapshot commit with the session's
configured Git identity, then fast-forwards local `main`. If another session
publishes first, this condition becomes false. The later session then acquires
the integration lock, snapshots its work, and first attempts a normal Git merge
of latest `main` into that snapshot. A Git-merged candidate is accepted only if
the worktree is clean, the candidate contains both latest `main` and the session
snapshot, and `git diff --check` passes for the candidate diff. If that
lightweight path fails, Cocodex resets the worktree to latest `main` and starts
the normal semantic fusion task. Publish paths preflight the project
repository's main worktree; dirty or unsafe main worktrees cause the current
`sync` request to be refused before Cocodex moves `main`. The session work
remains in the managed worktree or in a snapshot ref for retry.

No publish path fast-forwards clean idle sessions. Other developers' worktrees
move only when those developers run `cocodex sync` from their own managed
worktree.

Concurrent sync requests are fail-fast. If another session owns the integration
lock, `ready_to_integrate` returns an `integration busy` error instead of
queueing the second session. Cocodex does not keep a persistent multi-developer
sync queue.

`join <name>` resolves the developer from `config.developers[name]`. Cocodex
uses that entry's `git_user_name` and `git_user_email`, enables Git
`extensions.worktreeConfig`, and writes `user.name`/`user.email` with
`git config --worktree`, so each developer's managed worktree can commit with a
distinct identity under the shared server account. If the entry has no
`command`, Cocodex starts `codex`; otherwise it uses the configured JSON string
array. The CLI no longer accepts Git identity overrides; configuration is the
single source of truth for per-developer identity and launch command.

When `join` runs inside tmux, `_resolve_tmux_target()` binds the session agent
to the current pane by default. This is intentionally automatic: otherwise the
daemon can create a sync task but the running Codex only sees a printed file
path instead of receiving the full prompt. Launch wrappers that need a
different pane should pass `--tmux-target` explicitly. Non-interactive
maintenance and test harnesses should set `COCODEX_NO_TMUX=1` so inherited
`TMUX_PANE` values do not paste test prompts into the operator's current Codex
session.

On every `join`, Cocodex calls `prepare_join_startup_notice()` before launching
the session command. This makes restart behavior explicit:

- active tasks are re-announced with task and validation paths;
- legacy `blocked`, `recovery_required`, `queued`, or interrupted startup
  states are normalized to `fusing` when an active task can continue, or back
  to `clean` after restoring a snapshot when the task never safely started;
- clean sessions that are only behind `main` produce a catch-up notice without
  moving the worktree;
- local unintegrated work produces a review-before-new-work notice.

`SessionAgent` prints this startup notice after the child command starts. If a
tmux target was detected or configured, it also pastes the notice into that
pane.

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
- `blocked_reason`: legacy field kept for old state records; normal refusal
  paths do not persist a blocked reason.
- `pid`, `control_socket`, `last_heartbeat`, `connected`, `agent_version`:
  runtime metadata.

SQLite also stores:

- the global integration lock;
- key/value metadata such as `last_observed_main`;
- an event log for status and debugging.

The lock and `active_task` must stay consistent. `claim_integration_task()`
records the task id and lock owner in one SQLite transaction before snapshot
work begins. If task startup cannot safely finish, Cocodex restores the
snapshot when available, clears the active task, releases the lock, and refuses
the current `sync`.

## Session States

Important states:

- `clean`: no pending work relative to the session's known main.
- `dirty`: local changes or commits need integration. This is entered by
  explicit sync paths, not by daemon auto-queueing.
- `snapshot`: the daemon is preparing a snapshot.
- `frozen`: the session acknowledged freeze.
- `fusing`: the owning Codex is applying the snapshot on top of latest `main`.
- `verifying`: Cocodex is validating the candidate.
- `publishing`: Cocodex is moving `main` and optionally pushing remote.
- `queued`, `blocked`, `recovery_required`: legacy states normalized on daemon
  startup, join, or sync; they are not normal target states.
- `abandoned`: legacy state from old manual-recovery releases; not a normal
  target state.

## Control Protocol

Session to daemon:

- `register`: attach a session agent and runtime metadata.
- `heartbeat`: keep the session connected and report agent version.
- `shutdown`: mark the session disconnected.
- `ready_to_integrate`: request used by `cocodex sync`; it completes direct
  publish, clean Git merge publish, or semantic task startup before replying.
- `fusion_done`: internal candidate-ready signal used by `cocodex sync`.

Daemon to session:

- `freeze`: ask the agent to stop accepting new work for this integration
  window.
- `start_fusion`: tell the agent to write the prompt file and inject the prompt
  into the session pane.

`src/cocodex/protocol.py` validates message shape, and
`src/cocodex/transport.py` implements JSONL socket transport.

`register` and `heartbeat` include `agent_version`. The daemon compares this
with its own package version. Stale agents are refused at sync/register
boundaries, but sessions are not moved into persistent blocked states. `status`
shows `version_mismatch=true` so operators can restart old `cocodex join`
agents after upgrades.

## Integration Flow

The daemon loop performs:

1. heartbeat timeout detection;
2. external `main` movement detection;
3. event logging.

`ready_to_integrate` performs integration startup inside the request that came
from `cocodex sync`. It claims the lock and active task together, sends
`freeze`, prepares the snapshot, stores snapshot/base refs under
`refs/cocodex/`, tries the clean Git merge fast path, and either publishes or
starts a semantic task before replying. A second session receives
`integration busy` and must retry later.

Semantic task startup is accepted only if `start_fusion` returns an ack with
successful prompt delivery. Delivery means the prompt was written to a prompt
file and pasted into the configured tmux pane; Cocodex does not submit the
prompt with Enter. If prompt delivery fails, Cocodex restores the snapshot
commit into the session worktree, releases the lock, clears the active task,
and refuses the `sync`.

The task file is created by `src/cocodex/tasks.py`. It includes the snapshot
commit, latest main, last seen main, diff summary, interruption-handling
guidance, semantic-union requirements, contradiction handling rules,
validation-report requirements, and the instruction to run `cocodex sync` again
from the same worktree after committing the candidate.

## Publishing Flow

For dirty sessions whose `last_seen_main` is stale, `ready_to_integrate` first
claims the integration lock and freezes the session agent. `prepare_locked_sync()`
then snapshots the session work and calls `publish_with_git_merge_if_clean()`.
That path runs `git merge --no-ff` inside the session worktree and performs the
lightweight structural checks described above. Successful clean merges are
published directly to local `main`, marked as `published with git merge`, and do
not create a task file or prompt the Codex session. Merge conflicts, unsafe Git
state, dirty post-merge worktrees, missing ancestry, or `git diff --check`
failures are treated as semantic fallback conditions: the merge is aborted or
reset away, the snapshot ref is preserved, and a normal Cocodex task is created.

`publish_candidate()` is called when active-task `sync` reports completion.

It checks:

- session and task id match;
- integration lock is owned by the same session/task;
- task file, snapshot ref, and base ref all still exist;
- worktree has no unsafe Git operation;
- reported candidate equals session `HEAD`;
- candidate is not the task base commit, unless Codex created an explicit
  no-op commit;
- worktree is clean before validation;
- the task validation report exists and has meaningful content;
- validation did not coincide with `HEAD` changes or dirty the worktree;
- the project repository's main worktree is clean and has no unsafe Git state;
- local `main` can fast-forward to the candidate.

After local publish, Cocodex records `last_observed_main`, marks the publishing
session clean, and releases the lock. Other session worktrees are not moved or
notified. The CLI then attempts best-effort scoped remote sync for
`main_branch`, the publishing session branch, and `refs/cocodex/*` recovery
refs if a remote is configured. Remote failure after local publish is
non-fatal: Cocodex prints a warning and retries on later successful `sync`
commands.

On successful publish, Cocodex:

1. marks the session clean;
2. updates `last_seen_main`;
3. releases the lock.

## Refusal And Startup Semantics

Cocodex refuses unsafe actions rather than writing a persistent blocked state.

Heartbeat timeout:

- stale connected sessions are marked disconnected;
- if a stale session owns the lock, the lock and `fusing` active task are kept;
  other sessions receive `integration busy` until the owner rejoins and runs
  `cocodex sync` from the owning worktree.

Startup recovery:

- active tasks with a task file and matching lock are normalized to `fusing`;
- if the lock owner and session match but their task ids differ, Cocodex treats
  the lock task as authoritative only after creating a backup ref for the
  current worktree, then rewrites the session `active_task` to the lock task;
- incomplete task startup without a usable task file first creates a backup ref
  under `refs/cocodex/backups/...`, then restores the snapshot ref when
  possible, clears the active task, and releases the lock;
- legacy `queued`, taskless `blocked`, and taskless `recovery_required` states
  become `clean`; work detection still comes from the actual worktree head and
  dirty status.
- legacy queue rows for unknown sessions are pruned; they never schedule work.

Unknown baseline recovery:

- if `last_seen_main` is missing and ancestry proves the session is ahead of
  current `main`, Cocodex adopts current `main` as the baseline and can publish;
- if the session is only behind current `main`, Cocodex adopts the session head
  as the baseline so ordinary catch-up can fast-forward it;
- divergent unknown-baseline sessions are refused for operator inspection.

External main detection:

- compares local `main` against `last_observed_main`;
- records an event and updates the observed value; Cocodex assumes normal main
  movement comes from Cocodex itself, and publish-time fast-forward checks still
  refuse stale candidates.

Refusal output:

- CLI failures are normalized through `src/cocodex/failures.py`.
- `format_failure_handling()` prints a `Cocodex sync refused` block with the
  next safe action: retry after busy lock, same-session task completion,
  version-mismatch restart, daemon startup, or main-guard correction.
- `format_status()` calls `next_step_for_session()` and includes active task
  file, validation file, snapshot ref, base ref, and one explicit next step.
- Transport-level daemon errors still return a short `error` message; the CLI
  appends local failure handling guidance before exiting non-zero.

Maintainers should preserve this rule when adding new refusal states: every
user-visible refusal path must answer three questions in the output or docs:

1. Is this a same-session action or an operator action?
2. Should the worktree be kept unchanged, fixed, rejoined, or retried later?
3. Which Cocodex command should be run next?

Task recovery:

- active task refusals keep the task id, keep the lock, and keep the session in
  `fusing`; the owning Codex fixes the task issue and runs `cocodex sync`
  again;
- disconnected active tasks are same-owner recoverable: `cocodex join <name>`
  re-announces the task, and a later same-session `cocodex sync` may publish
  once the candidate is committed and validated;
- `cocodex status` shows task files, validation file, snapshot/base refs, lock
  ownership, and next-step hints for active sessions;
- legacy taskless states are normalized by `sync`, `join`, or daemon startup;
  unsafe states are refused with owner-specific instructions instead of manual
  recovery commands.

## Session Deletion

`cocodex delete <name>` is implemented in `src/cocodex/delete.py`. It removes a
retired session from local Cocodex state and managed Git resources while keeping
a recovery surface.

The command refuses to run when:

- the session is connected or its recorded process id still appears alive;
- the session owns the integration lock;
- the session has an active task;
- the managed worktree is not on the expected `cocodex/<name>` branch;
- the worktree has an unfinished Git operation;
- the session branch is checked out in another worktree;
- the worktree contains ignored files other than Cocodex's generated
  `AGENTS.md`.

On success it creates `.cocodex/deleted/<timestamp>-<name>.json`, stores the
session branch head under `refs/cocodex/deleted/<timestamp>/<name>/head`, and,
if tracked or untracked work exists, stores a `git stash push
--include-untracked` commit under
`refs/cocodex/deleted/<timestamp>/<name>/dirty`. Only after these refs and the
manifest exist does it remove the worktree, delete the local
`cocodex/<name>` branch, remove `sessions` and `queue` rows, and record a
`session_deleted` event.

The developer entry in `.cocodex/config.json` is deliberately not removed.
Configuration describes who may join; session deletion only removes the current
local session instance. If `config.remote` is set, delete best-effort pushes the
deleted-session refs and removes the remote session branch. Remote cleanup
failure is a warning after local cleanup, matching sync's non-fatal remote
policy.

## Development Notes

The public release tree includes the Cocodex release scenario tests under
`tests/`. They are intentionally part of the source distribution so users and
maintainers can reproduce the same end-to-end checks that guard PyPI releases.
Runtime scratch repositories are created under `COCODEX_TEST_ROOT`, or under
`~/coconut-tests` when that environment variable is not set. In this development
environment, that default is `/root/coconut-tests`.

Useful local checks:

```bash
python tests/run_release_scenarios.py
python -m pytest -q
PYTHONPATH=src python3 -m cocodex --help
git diff --check HEAD
```

When debugging reports that `sync` does not update a remote repository, check
`cocodex status` first. If it shows `remote: none`, `config.remote` is `null`
and `try_force_push_session_refs()` intentionally returns without pushing,
even if the underlying Git repository has an `origin` remote. When a semantic
task has started, also check whether `refs/cocodex/snapshots/<task>` exists on
the remote; those refs are part of the recovery surface.

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
python tests/run_release_scenarios.py
python -m build
python -m twine check --strict dist/*
git add setup.cfg
git commit -m "Release X.Y.Z"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
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
- `tests/`;
- supporting project metadata such as `.gitignore`.

Do not publish `.cocodex/`, `.pytest_cache/`, `__pycache__/`, internal planning
documents, or ad hoc scratch directories.
