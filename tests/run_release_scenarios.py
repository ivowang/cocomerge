#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


SOURCE = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(os.environ.get("COCODEX_TEST_ROOT", Path.home() / "coconut-tests"))
RUN_ROOT = TEST_ROOT / time.strftime("run-%Y%m%d-%H%M%S")
PYTHONPATH = str(SOURCE / "src")


@dataclass
class CmdResult:
    name: str
    cwd: Path
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float


class Harness:
    def __init__(self) -> None:
        self.results: list[CmdResult] = []
        self.notes: list[str] = []
        self.failures: list[str] = []
        self.processes: list[tuple[str, subprocess.Popen[str], Path]] = []
        self.cmd_counter = 0
        RUN_ROOT.mkdir(parents=True, exist_ok=True)

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = PYTHONPATH
        env["PYTHONUNBUFFERED"] = "1"
        env["COCODEX_NO_TMUX"] = "1"
        env.setdefault("GIT_AUTHOR_NAME", "Harness")
        env.setdefault("GIT_AUTHOR_EMAIL", "harness@example.test")
        env.setdefault("GIT_COMMITTER_NAME", "Harness")
        env.setdefault("GIT_COMMITTER_EMAIL", "harness@example.test")
        if extra:
            env.update(extra)
        return env

    def cocodex(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "cocodex", *args]

    def run(
        self,
        name: str,
        cmd: list[str],
        cwd: Path,
        *,
        check: bool = True,
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> CmdResult:
        self.cmd_counter += 1
        label = f"{self.cmd_counter:03d}-{safe_name(name)}"
        start = time.monotonic()
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=self.env(env),
            check=False,
        )
        duration = time.monotonic() - start
        result = CmdResult(name, cwd, cmd, proc.returncode, proc.stdout, proc.stderr, duration)
        self.results.append(result)
        (RUN_ROOT / f"{label}.cmd.txt").write_text(
            f"cwd: {cwd}\ncmd: {shlex_join(cmd)}\nreturncode: {proc.returncode}\n"
            f"duration: {duration:.3f}s\n",
            encoding="utf-8",
        )
        (RUN_ROOT / f"{label}.stdout.txt").write_text(proc.stdout, encoding="utf-8")
        (RUN_ROOT / f"{label}.stderr.txt").write_text(proc.stderr, encoding="utf-8")
        if check and proc.returncode != 0:
            self.fail(f"{name} failed with {proc.returncode}: {proc.stderr.strip()}")
        return result

    def popen(self, name: str, cmd: list[str], cwd: Path) -> subprocess.Popen[str]:
        log_dir = RUN_ROOT / "process-logs"
        log_dir.mkdir(exist_ok=True)
        stdout = (log_dir / f"{safe_name(name)}.stdout.txt").open("w", encoding="utf-8")
        stderr = (log_dir / f"{safe_name(name)}.stderr.txt").open("w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            text=True,
            stdout=stdout,
            stderr=stderr,
            env=self.env(),
            start_new_session=True,
        )
        self.processes.append((name, proc, log_dir))
        self.notes.append(f"started {name}: pid={proc.pid}, cwd={cwd}, cmd={shlex_join(cmd)}")
        return proc

    def terminate(self, proc: subprocess.Popen[str], name: str) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        self.notes.append(f"stopped {name}: returncode={proc.returncode}")

    def cleanup(self) -> None:
        for name, proc, _ in reversed(self.processes):
            self.terminate(proc, name)

    def fail(self, message: str) -> None:
        self.failures.append(message)
        self.notes.append(f"FAIL: {message}")

    def require(self, condition: bool, message: str) -> None:
        if condition:
            self.notes.append(f"PASS: {message}")
        else:
            self.fail(message)

    def wait_for(self, label: str, predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if predicate():
                    self.notes.append(f"PASS: waited for {label}")
                    return True
            except Exception as exc:  # noqa: BLE001 - test harness records transient failures.
                last_exc = exc
            time.sleep(0.2)
        detail = f" after exception {last_exc}" if last_exc else ""
        self.fail(f"timed out waiting for {label}{detail}")
        return False

    def report(self) -> Path:
        report = RUN_ROOT / "REPORT.md"
        lines = [
            "# Cocodex Release Test Report",
            "",
            f"Source: `{SOURCE}`",
            f"Run root: `{RUN_ROOT}`",
            f"PYTHONPATH: `{PYTHONPATH}`",
            "",
            "## Summary",
            "",
            f"Commands run: {len(self.results)}",
            f"Failures: {len(self.failures)}",
            "",
        ]
        if self.failures:
            lines.extend(["## Failures", ""])
            lines.extend(f"- {failure}" for failure in self.failures)
            lines.append("")
        lines.extend(["## Notes", ""])
        lines.extend(f"- {note}" for note in self.notes)
        lines.append("")
        lines.extend(["## Commands", ""])
        for result in self.results:
            lines.extend(
                [
                    f"### {result.name}",
                    "",
                    f"- cwd: `{result.cwd}`",
                    f"- cmd: `{shlex_join(result.cmd)}`",
                    f"- returncode: `{result.returncode}`",
                    f"- duration: `{result.duration:.3f}s`",
                    "",
                    "stdout:",
                    "```text",
                    tail(result.stdout),
                    "```",
                    "",
                    "stderr:",
                    "```text",
                    tail(result.stderr),
                    "```",
                    "",
                ]
            )
        report.write_text("\n".join(lines), encoding="utf-8")
        return report


def shlex_join(cmd: list[str]) -> str:
    import shlex

    return shlex.join(cmd)


def safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name).strip("-")[:80] or "cmd"


def tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def git(h: Harness, name: str, repo: Path, *args: str, check: bool = True) -> CmdResult:
    return h.run(name, ["git", *args], repo, check=check)


def head(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.check_output(["git", "rev-parse", ref], cwd=repo, text=True).strip()


def status_porcelain(repo: Path) -> str:
    return subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True).strip()


def session_row(repo: Path, name: str) -> sqlite3.Row | None:
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM sessions WHERE name = ?", (name,)).fetchone()
    finally:
        db.close()


def lock_row(repo: Path) -> sqlite3.Row:
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT owner, task_id FROM locks WHERE name = 'integration'").fetchone()
    finally:
        db.close()


def latest_task_id(repo: Path, session: str) -> str:
    row = session_row(repo, session)
    if row is None or not row["active_task"]:
        raise AssertionError(f"{session} has no active task")
    return row["active_task"]


def configure_developers(repo: Path, *, interval: float = 0.5) -> None:
    path = repo / ".cocodex" / "config.json"
    data = json.loads(read(path))
    data["dirty_interval_s"] = interval
    data["developers"] = {
        "alice": {
            "git_user_name": "Alice Dev",
            "git_user_email": "alice@example.test",
            "command": ["sleep", "600"],
        },
        "bob": {
            "git_user_name": "Bob Dev",
            "git_user_email": "bob@example.test",
            "command": ["sleep", "600"],
        },
        "charlie": {
            "git_user_name": "Charlie Dev",
            "git_user_email": "charlie@example.test",
            "command": ["sleep", "600"],
        },
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def set_developer_command(repo: Path, session: str, command: list[str]) -> None:
    path = repo / ".cocodex" / "config.json"
    data = json.loads(read(path))
    data["developers"][session]["command"] = command
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def set_config_remote(repo: Path, remote: str | None) -> None:
    path = repo / ".cocodex" / "config.json"
    data = json.loads(read(path))
    data["remote"] = remote
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def make_repo(h: Harness, name: str, *, remote: bool = False, interval: float = 0.5) -> Path:
    repo = RUN_ROOT / name
    repo.mkdir(parents=True)
    git(h, f"{name}: git init", repo, "init", "-b", "main")
    git(h, f"{name}: set user.name", repo, "config", "user.name", "Main User")
    git(h, f"{name}: set user.email", repo, "config", "user.email", "main@example.test")
    write(repo / "README.md", f"# {name}\n")
    write(repo / "app.txt", "base\n")
    git(h, f"{name}: initial add", repo, "add", "-A")
    git(h, f"{name}: initial commit", repo, "commit", "-m", "initial")
    if remote:
        missing_remote = RUN_ROOT / f"{name}-missing-remote.git"
        git(h, f"{name}: add unavailable remote", repo, "remote", "add", "origin", str(missing_remote))
        h.run(f"{name}: cocodex init remote", h.cocodex("init", "--main", "main", "--remote", "origin"), repo)
    else:
        h.run(f"{name}: cocodex init", h.cocodex("init", "--main", "main"), repo)
    configure_developers(repo, interval=interval)
    return repo


def start_daemon(h: Harness, repo: Path, name: str) -> subprocess.Popen[str]:
    proc = h.popen(f"{name}-daemon", h.cocodex("daemon"), repo)
    h.wait_for(f"{name} daemon socket", lambda: (repo / ".cocodex" / "cocodex.sock").exists())
    return proc


def start_join(h: Harness, repo: Path, session: str, name: str | None = None) -> subprocess.Popen[str]:
    proc = h.popen(name or f"{repo.name}-{session}-join", h.cocodex("join", session), repo)
    worktree = repo / ".cocodex" / "worktrees" / session
    if (repo / ".cocodex" / "cocodex.sock").exists():
        h.wait_for(
            f"{repo.name} {session} connected",
            lambda: session_row(repo, session) is not None and bool(session_row(repo, session)["connected"]),
        )
    else:
        h.wait_for(f"{repo.name} {session} registered", lambda: session_row(repo, session) is not None)
    h.require(worktree.exists(), f"{repo.name} {session} worktree exists at {worktree}")
    return proc


def write_validation(repo: Path, task_id: str, body: str) -> Path:
    path = repo / ".cocodex" / "tasks" / f"{task_id}.validation.md"
    write(path, body)
    return path


def assert_single_lock_owner(h: Harness, repo: Path, expected: str | None, label: str) -> None:
    row = lock_row(repo)
    owner = row["owner"] if row else None
    task_id = row["task_id"] if row else None
    h.require(owner == expected and ((owner is None and task_id is None) or (owner and task_id)), label)


def test_package_metadata(h: Harness) -> None:
    case = RUN_ROOT / "package"
    build_source = case / "source-copy"
    dist = case / "dist"
    install_prefix = case / "install-prefix"
    dist.mkdir(parents=True)
    shutil.copytree(
        SOURCE,
        build_source,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.egg-info", "build", "dist"),
    )
    sdist_dir = case / "sdist"
    sdist_dir.mkdir()
    h.run(
        "package: sdist includes tests",
        [sys.executable, "setup.py", "sdist", "--dist-dir", str(sdist_dir)],
        build_source,
        timeout=120,
    )
    sdists = list(sdist_dir.glob("cocodex-*.tar.gz"))
    h.require(len(sdists) == 1, f"one cocodex sdist produced in {sdist_dir}")
    if sdists:
        with tarfile.open(sdists[0], "r:gz") as archive:
            sdist_names = archive.getnames()
        h.require(
            any(name.endswith("/tests/run_release_scenarios.py") for name in sdist_names)
            and any(name.endswith("/tests/test_release_scenarios.py") for name in sdist_names),
            "source distribution includes release tests",
        )

    h.run("package: pip wheel", [sys.executable, "-m", "pip", "wheel", "--use-pep517", "--no-deps", "--wheel-dir", str(dist), str(build_source)], case, timeout=120)
    wheels = list(dist.glob("cocodex-*.whl"))
    h.require(len(wheels) == 1, f"one cocodex wheel produced in {dist}")
    if len(wheels) != 1:
        all_wheels = ", ".join(path.name for path in dist.glob("*.whl")) or "none"
        h.fail(f"package build produced unexpected wheels: {all_wheels}")
        return
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        entry_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        metadata = zf.read(metadata_name).decode("utf-8")
        entries = zf.read(entry_name).decode("utf-8")
    h.require("Name: cocodex" in metadata and "Version: 0.1.0" in metadata, "wheel metadata has name/version")
    h.require("cocodex = cocodex.cli:main" in entries, "wheel exposes cocodex console script")
    h.run(
        "package: install wheel with prefix",
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "--ignore-installed",
            "--no-deps",
            "--prefix",
            str(install_prefix),
            str(wheel),
        ],
        case,
        timeout=120,
    )
    purelib = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import sysconfig,sys; print(sysconfig.get_path('purelib', vars={'base': sys.argv[1], 'platbase': sys.argv[1]}))",
            str(install_prefix),
        ],
        text=True,
    ).strip()
    script = install_prefix / "bin" / "cocodex"
    if not script.exists():
        script = install_prefix / "local" / "bin" / "cocodex"
    help_result = h.run(
        "package: console script help",
        [str(script), "--help"],
        case,
        env={"PYTHONPATH": purelib},
    )
    h.require("usage: cocodex" in help_result.stdout, "installed console script runs")


def test_init_status_config_join(h: Harness) -> None:
    repo = make_repo(h, "init-status-config")
    status = h.run("init/status: status after init", h.cocodex("status"), repo)
    h.require(
        "remote: none" in status.stdout and "lock: free" in status.stdout and "queue: empty" in status.stdout,
        "status works after init and shows remote config",
    )
    help_result = h.run("join: help hides legacy flags", h.cocodex("join", "--help"), repo)
    hidden = ["--name", "--git-user-name", "--git-user-email", "--no-auto-prompt"]
    h.require(all(flag not in help_result.stdout for flag in hidden), "join help hides legacy flags")
    old_identity = h.run(
        "join: rejects old git identity flag",
        h.cocodex("join", "--git-user-email", "old@example.test", "alice"),
        repo,
        check=False,
    )
    h.require(old_identity.returncode != 0 and "--git-user-email" in old_identity.stderr, "join rejects old Git identity flags")
    old_command = h.run(
        "join: rejects old command override",
        h.cocodex("join", "alice", "--", sys.executable, "-c", "print('old command')"),
        repo,
        check=False,
    )
    h.require(old_command.returncode != 0 and "unrecognized arguments" in old_command.stderr, "join rejects old command override")
    old_done = h.run("completion: rejects removed done command", h.cocodex("done", "alice"), repo, check=False)
    h.require(old_done.returncode != 0 and "invalid choice" in old_done.stderr, "done command is removed")
    old_block = h.run("completion: rejects removed block command", h.cocodex("block", "alice", "reason"), repo, check=False)
    h.require(old_block.returncode != 0 and "invalid choice" in old_block.stderr, "block command is removed")
    sync_arg = h.run("sync: rejects session argument", h.cocodex("sync", "alice"), repo, check=False)
    h.require(sync_arg.returncode != 0 and "unrecognized arguments" in sync_arg.stderr, "sync rejects session arguments")
    old_protocol = h.run(
        "protocol: rejects removed fusion_blocked type",
        [
            sys.executable,
            "-c",
            "from cocodex.protocol import decode_message, ProtocolError\n"
            "import sys\n"
            "try:\n"
            "    decode_message(b'{\"type\":\"fusion_blocked\",\"session\":\"alice\",\"task_id\":\"t\"}\\n')\n"
            "except ProtocolError as exc:\n"
            "    print(exc)\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n",
        ],
        repo,
    )
    h.require("unknown message type: fusion_blocked" in old_protocol.stdout, "protocol rejects removed fusion_blocked type")

    proc = start_join(h, repo, "alice", "init-status-config-alice-join")
    worktree = repo / ".cocodex" / "worktrees" / "alice"
    user_name = git(h, "join: worktree user.name", worktree, "config", "--get", "user.name").stdout.strip()
    user_email = git(h, "join: worktree user.email", worktree, "config", "--get", "user.email").stdout.strip()
    h.require(user_name == "Alice Dev" and user_email == "alice@example.test", "join writes configured Git identity")
    agents = read(worktree / "AGENTS.md")
    h.require("Cocodex Session Instructions" in agents and "cocodex sync" in agents, "join generates managed AGENTS.md")
    h.require(
        "affects only this managed worktree and local `main`" in agents
        and "sync may publish it directly" in agents
        and "server branch refs" not in agents
        and "sync requests an integration task" not in agents,
        "managed AGENTS.md describes scoped sync and direct publish",
    )
    prompt_check = h.run(
        "prompt text: scoped sync guidance",
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "from cocodex.agent import build_sync_prompt\n"
            "prompt = build_sync_prompt('alice', Path('/tmp/task.md'))\n"
            "print(prompt)\n",
        ],
        repo,
    )
    h.require(
        "This task exists because this session has local work" in prompt_check.stdout
        and "does not move or notify other Cocodex session worktrees" in prompt_check.stdout
        and "remote refs" not in prompt_check.stdout,
        "sync prompt describes semantic task scope and scoped remote sync",
    )
    exclude = read(Path(subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=worktree, text=True).strip()) / "info" / "exclude")
    h.require("/AGENTS.md" in exclude, "AGENTS.md is ignored through worktree git exclude")
    h.terminate(proc, "init-status-config-alice-join")

    invalid = RUN_ROOT / "invalid-main"
    invalid.mkdir()
    git(h, "invalid-main: init git", invalid, "init", "-b", "main")
    bad = h.run("config validation: init rejects missing main branch", h.cocodex("init", "--main", "main"), invalid, check=False)
    h.require(bad.returncode != 0 and "Main branch 'main' does not exist" in bad.stderr, "init validates main branch")

    config_path = repo / ".cocodex" / "config.json"
    data = json.loads(read(config_path))
    data["verify"] = "pytest"
    config_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    unknown_status = h.run("config validation: status rejects unknown verify key", h.cocodex("status"), repo, check=False)
    h.require(unknown_status.returncode != 0 and "Unknown key(s)" in unknown_status.stderr, "status rejects obsolete config keys")
    data.pop("verify")
    data["developers"]["bad"] = {"command": []}
    config_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    bad_status = h.run("config validation: status rejects bad developer command", h.cocodex("status"), repo, check=False)
    h.require(bad_status.returncode != 0 and "invalid command" in bad_status.stderr, "status validates developer command")


def test_clean_sync_and_dirty_publish(h: Harness) -> None:
    repo = make_repo(h, "dirty-publish", remote=True)
    daemon = start_daemon(h, repo, "dirty-publish")
    join = start_join(h, repo, "alice", "dirty-publish-alice-join")
    bob_join = start_join(h, repo, "bob", "dirty-publish-bob-join")
    worktree = repo / ".cocodex" / "worktrees" / "alice"
    bob = repo / ".cocodex" / "worktrees" / "bob"
    bob_initial = head(bob)

    before = head(repo, "main")
    write(repo / "main-only.txt", "main advanced\n")
    git(h, "clean sync: main add", repo, "add", "main-only.txt")
    git(h, "clean sync: main commit", repo, "commit", "-m", "advance main")
    advanced = head(repo, "main")
    h.require(before != advanced, "main advanced for clean catch-up")
    clean_sync = h.run("clean sync: alice catches up", h.cocodex("sync"), worktree, timeout=60)
    h.require("synced to" in clean_sync.stdout or "already synced" in clean_sync.stdout, "clean session sync returns catch-up message")
    h.require(head(worktree) == advanced, "clean session worktree fast-forwarded to latest main")

    write(worktree / "direct.txt", "alice direct feature\n")
    direct = h.run("direct publish: alice current main", h.cocodex("sync"), worktree, timeout=60)
    direct_main = head(repo, "main")
    h.require("published directly" in direct.stdout, "dirty session based on current main publishes directly")
    h.require(direct_main == head(worktree), "direct publish main equals alice candidate")
    h.require(status_porcelain(worktree) == "", "alice worktree clean after direct publish")
    h.require(head(bob) == bob_initial, "direct publish does not move another clean session")
    h.require("remote sync to origin failed and was skipped" in direct.stderr, "direct publish remote warning is non-fatal")
    assert_single_lock_owner(h, repo, None, "integration lock released after direct publish")

    bob_catchup = h.run("dirty sync: bob explicitly catches up", h.cocodex("sync"), bob, timeout=60)
    h.require("synced to" in bob_catchup.stdout or "already synced" in bob_catchup.stdout, "bob catches up only after own sync")
    h.require(head(bob) == direct_main, "bob worktree moves only after bob sync")
    write(worktree / "feature.txt", "alice dirty feature\n")
    write(bob / "bob-main.txt", "bob advances main first\n")
    bob_direct = h.run("dirty sync: bob advances main directly", h.cocodex("sync"), bob, timeout=60)
    h.require("published directly" in bob_direct.stdout, "bob direct publish advances main before alice sync")
    queued = h.run("dirty sync: queue alice after main advanced", h.cocodex("sync"), worktree, timeout=60)
    h.require("Queued alice for sync" in queued.stdout, "dirty session after main advanced enters fusion queue")
    h.wait_for("alice fusing task", lambda: (session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "fusing"), timeout=20)
    task_id = latest_task_id(repo, "alice")
    task_path = repo / ".cocodex" / "tasks" / f"{task_id}.md"
    h.require(task_path.exists(), "dirty sync writes integration task file")
    task_text = read(task_path)
    h.require(
        "other sessions catch up or publish only" in task_text
        and "local `main` plus this session branch" in task_text
        and "best-effort sync the configured remote" not in task_text,
        "integration task file describes scoped publish behavior",
    )

    write(worktree / "feature.txt", "alice dirty feature\n")
    git(h, "dirty sync: candidate add", worktree, "add", "-A")
    git(h, "dirty sync: candidate commit", worktree, "commit", "-m", "alice candidate")
    missing = h.run("dirty sync: missing validation rejected", h.cocodex("sync"), worktree, check=False, timeout=60)
    h.require("Blocked alice" in missing.stdout and "validation report is missing" in missing.stdout, "missing validation report is rejected")
    write_validation(repo, task_id, "too short")
    short = h.run("dirty sync: short validation rejected", h.cocodex("sync"), worktree, check=False, timeout=60)
    h.require("Blocked alice" in short.stdout and "validation report is too short" in short.stdout, "short validation report is rejected")
    write_validation(
        repo,
        task_id,
        "Validation plan: confirm the feature file exists and contains alice dirty feature.\n"
        "Command: cat feature.txt. Result: expected content observed. Remaining risk: none for this fixture.\n",
    )
    published = h.run("dirty sync: publish candidate", h.cocodex("sync"), worktree, timeout=60)
    new_main = head(repo, "main")
    h.require("Published alice" in published.stdout, "candidate publishes after sufficient validation")
    h.require(new_main == head(worktree), "published main equals alice candidate")
    h.require(status_porcelain(worktree) == "", "alice worktree clean after publish")
    h.require(head(bob) != new_main, "fusion publish does not move another clean session")
    h.require("remote sync to origin failed and was skipped" in published.stderr, "unavailable remote warns without blocking publish")
    assert_single_lock_owner(h, repo, None, "integration lock released after publish")

    h.terminate(bob_join, "dirty-publish-bob-join")
    h.terminate(join, "dirty-publish-alice-join")
    h.terminate(daemon, "dirty-publish-daemon")


def test_configured_remote_pushes_main_and_session_refs(h: Harness) -> None:
    repo = make_repo(h, "remote-push")
    remote_repo = RUN_ROOT / "remote-push-origin.git"
    git(h, "remote push: init bare origin", remote_repo.parent, "init", "--bare", str(remote_repo))
    git(h, "remote push: add origin", repo, "remote", "add", "origin", str(remote_repo))
    set_config_remote(repo, "origin")
    daemon = start_daemon(h, repo, "remote-push")
    join = start_join(h, repo, "alice", "remote-push-alice-join")
    worktree = repo / ".cocodex" / "worktrees" / "alice"

    status = h.run("remote push: status shows configured remote", h.cocodex("status"), repo)
    h.require("remote: origin" in status.stdout, "status shows configured remote")
    write(worktree / "remote.txt", "remote publish feature\n")
    published = h.run("remote push: direct publish pushes refs", h.cocodex("sync"), worktree, timeout=60)
    h.require("published directly" in published.stdout, "direct publish with configured remote succeeds")
    main_head = head(repo, "main")
    session_head = head(worktree)
    remote_main = git(h, "remote push: remote main ref", remote_repo, "rev-parse", "refs/heads/main").stdout.strip()
    remote_session = git(h, "remote push: remote session ref", remote_repo, "rev-parse", "refs/heads/cocodex/alice").stdout.strip()
    h.require(remote_main == main_head, "configured remote receives local main")
    h.require(remote_session == session_head, "configured remote receives current session branch")

    h.terminate(join, "remote-push-alice-join")
    h.terminate(daemon, "remote-push-daemon")


def test_direct_publish_blocked_by_dirty_main_can_resume(h: Harness) -> None:
    repo = make_repo(h, "direct-publish-recovery")
    daemon = start_daemon(h, repo, "direct-publish-recovery")
    join = start_join(h, repo, "alice", "direct-publish-recovery-alice-join")
    worktree = repo / ".cocodex" / "worktrees" / "alice"
    original_main = head(repo, "main")

    write(repo / ".python-version", "dirty-main\n")
    write(worktree / ".python-version", "3.12\n")
    failed = h.run(
        "direct publish recovery: dirty main blocks publish",
        h.cocodex("sync"),
        worktree,
        check=False,
        timeout=60,
    )
    h.require(failed.returncode != 0, "dirty main direct publish reports failure")
    h.require(head(repo, "main") == original_main, "blocked direct publish does not move main")
    row = session_row(repo, "alice")
    h.require(
        row is not None
        and row["state"] == "blocked"
        and row["active_task"] is None
        and "direct publish failed" in (row["blocked_reason"] or ""),
        "blocked direct publish leaves session resumable instead of stuck publishing",
    )
    assert_single_lock_owner(h, repo, None, "lock released after blocked direct publish")
    blocked_sync = h.run(
        "direct publish recovery: blocked sync explains resume",
        h.cocodex("sync"),
        worktree,
        check=False,
        timeout=60,
    )
    h.require(
        blocked_sync.returncode != 0 and "cocodex resume alice" in blocked_sync.stdout,
        "blocked session sync explains operator resume command",
    )
    h.run("direct publish recovery: premature resume while still blocked", h.cocodex("resume", "alice"), repo, timeout=60)
    h.wait_for(
        "direct publish recovery premature resume re-blocks",
        lambda: session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "blocked",
        timeout=5,
    )
    h.require(daemon.poll() is None, "premature resume does not stop daemon")

    (repo / ".python-version").unlink()
    h.run("direct publish recovery: resume blocked session", h.cocodex("resume", "alice"), repo, timeout=60)
    h.wait_for("direct publish recovery clean", lambda: session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "clean", timeout=20)
    h.require(head(repo, "main") == head(worktree), "resumed direct publish advances main")

    h.terminate(join, "direct-publish-recovery-alice-join")
    h.terminate(daemon, "direct-publish-recovery-daemon")


def test_two_dirty_sessions_queue_lock(h: Harness) -> None:
    repo = make_repo(h, "queue-lock")
    daemon = start_daemon(h, repo, "queue-lock")
    alice_join = start_join(h, repo, "alice", "queue-lock-alice-join")
    bob_join = start_join(h, repo, "bob", "queue-lock-bob-join")
    charlie_join = start_join(h, repo, "charlie", "queue-lock-charlie-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    bob = repo / ".cocodex" / "worktrees" / "bob"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"
    alice_initial = head(alice)
    bob_initial = head(bob)
    write(alice / "alice.txt", "alice queued feature\n")
    write(bob / "bob.txt", "bob queued feature\n")
    write(charlie / "main-queue.txt", "main advanced before queued syncs\n")
    charlie_publish = h.run("queue lock: charlie advances main", h.cocodex("sync"), charlie, timeout=60)
    h.require("published directly" in charlie_publish.stdout, "charlie direct publish advances main before queued syncs")
    h.require(head(alice) == alice_initial and head(bob) == bob_initial, "charlie publish does not move alice or bob")
    h.run("queue lock: queue alice", h.cocodex("sync"), alice, timeout=60)
    h.run("queue lock: queue bob", h.cocodex("sync"), bob, timeout=60)
    h.wait_for("alice first fusing", lambda: session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "fusing", timeout=20)
    alice_task = latest_task_id(repo, "alice")
    bob_row = session_row(repo, "bob")
    h.require(bob_row is not None and bob_row["state"] == "queued" and bob_row["active_task"] is None, "second dirty session remains queued without active task")
    assert_single_lock_owner(h, repo, "alice", "only alice holds integration lock while first task is active")

    write(alice / "alice.txt", "alice queued feature\n")
    git(h, "queue lock: alice candidate add", alice, "add", "-A")
    git(h, "queue lock: alice candidate commit", alice, "commit", "-m", "alice queue candidate")
    write_validation(
        repo,
        alice_task,
        "Validation plan: inspect alice.txt after reapplying queued feature.\n"
        "Command: cat alice.txt. Result: content matches. Remaining risk: fixture only.\n",
    )
    h.run("queue lock: publish alice", h.cocodex("sync"), alice, timeout=60)
    h.wait_for("bob fusing after alice publish", lambda: session_row(repo, "bob") is not None and session_row(repo, "bob")["state"] == "fusing", timeout=20)
    bob_task = latest_task_id(repo, "bob")
    assert_single_lock_owner(h, repo, "bob", "lock moved to bob only after alice publish")
    h.require(alice_task != bob_task, "two dirty sessions have distinct task ids")

    write(bob / "bob.txt", "bob queued feature\n")
    git(h, "queue lock: bob candidate add", bob, "add", "-A")
    git(h, "queue lock: bob candidate commit", bob, "commit", "-m", "bob queue candidate")
    write_validation(
        repo,
        bob_task,
        "Validation plan: inspect bob.txt after reapplying queued feature.\n"
        "Command: cat bob.txt. Result: content matches. Remaining risk: fixture only.\n",
    )
    h.run("queue lock: publish bob", h.cocodex("sync"), bob, timeout=60)
    assert_single_lock_owner(h, repo, None, "lock free after serial queue drains")
    h.require((repo / "bob.txt").exists() and (repo / "alice.txt").exists(), "main contains both serialized publications")

    h.terminate(charlie_join, "queue-lock-charlie-join")
    h.terminate(bob_join, "queue-lock-bob-join")
    h.terminate(alice_join, "queue-lock-alice-join")
    h.terminate(daemon, "queue-lock-daemon")


def capture_restart_join(h: Harness, repo: Path, session: str, label: str) -> CmdResult:
    set_developer_command(repo, session, [sys.executable, "-c", "import time; time.sleep(2)"])
    return h.run(
        label,
        h.cocodex("join", session),
        repo,
        check=False,
        timeout=8,
    )


def test_join_restart_notices(h: Harness) -> None:
    local_repo = make_repo(h, "restart-local")
    local_join = capture_restart_join(h, local_repo, "alice", "restart notice: initial local join")
    h.require(local_join.returncode == 0, "initial local notice setup join exits")
    local_worktree = local_repo / ".cocodex" / "worktrees" / "alice"
    write(local_worktree / "local.txt", "unintegrated local work\n")
    local_notice = capture_restart_join(h, local_repo, "alice", "restart notice: local work")
    h.require("has local work that is not integrated into main" in local_notice.stdout, "join restart reports local work")

    behind_repo = make_repo(h, "restart-behind", interval=0.5)
    behind_daemon = start_daemon(h, behind_repo, "restart-behind")
    behind_alice_join = start_join(h, behind_repo, "alice", "restart-behind-alice-long")
    behind_charlie_join = start_join(h, behind_repo, "charlie", "restart-behind-charlie-long")
    behind_alice = behind_repo / ".cocodex" / "worktrees" / "alice"
    behind_charlie = behind_repo / ".cocodex" / "worktrees" / "charlie"
    behind_initial = head(behind_alice)
    write(behind_charlie / "charlie.txt", "charlie advances behind main\n")
    h.run("restart notice: charlie advances behind main", h.cocodex("sync"), behind_charlie, timeout=60)
    h.require(head(behind_alice) == behind_initial, "join-time clean behind session is not auto-fast-forwarded")
    h.terminate(behind_alice_join, "restart-behind-alice-long")
    behind_notice = capture_restart_join(h, behind_repo, "alice", "restart notice: clean behind")
    h.require("clean session is behind latest" in behind_notice.stdout, "join restart reports clean behind main")
    h.require(head(behind_alice) == behind_initial, "restart notice does not move clean behind worktree")
    h.run("restart notice: clean behind explicit sync", h.cocodex("sync"), behind_alice, timeout=60)
    h.require(head(behind_alice) == head(behind_repo, "main"), "clean behind session catches up only after own sync")
    h.terminate(behind_charlie_join, "restart-behind-charlie-long")
    h.terminate(behind_daemon, "restart-behind-daemon")

    queued_repo = make_repo(h, "restart-queued", interval=100.0)
    queued_daemon = start_daemon(h, queued_repo, "restart-queued")
    queued_join = start_join(h, queued_repo, "alice", "restart-queued-alice-long")
    queued_charlie_join = start_join(h, queued_repo, "charlie", "restart-queued-charlie-long")
    queued_worktree = queued_repo / ".cocodex" / "worktrees" / "alice"
    queued_charlie = queued_repo / ".cocodex" / "worktrees" / "charlie"
    write(queued_worktree / "queued.txt", "queued work\n")
    write(queued_charlie / "charlie.txt", "charlie advances queued main\n")
    h.run("restart notice: charlie advances queued main", h.cocodex("sync"), queued_charlie, timeout=60)
    h.run("restart notice: queue session", h.cocodex("sync"), queued_worktree, timeout=60)
    h.wait_for("restart queued state", lambda: session_row(queued_repo, "alice") is not None and session_row(queued_repo, "alice")["state"] == "queued", timeout=5)
    h.terminate(queued_join, "restart-queued-alice-long")
    queued_notice = capture_restart_join(h, queued_repo, "alice", "restart notice: queued")
    h.require("already has a sync request queued" in queued_notice.stdout, "join restart reports queued work")
    h.terminate(queued_charlie_join, "restart-queued-charlie-long")
    h.terminate(queued_daemon, "restart-queued-daemon")

    active_repo = make_repo(h, "restart-active", interval=0.5)
    active_daemon = start_daemon(h, active_repo, "restart-active")
    active_join = start_join(h, active_repo, "alice", "restart-active-alice-long")
    active_charlie_join = start_join(h, active_repo, "charlie", "restart-active-charlie-long")
    active_worktree = active_repo / ".cocodex" / "worktrees" / "alice"
    active_charlie = active_repo / ".cocodex" / "worktrees" / "charlie"
    write(active_worktree / "active.txt", "active work\n")
    write(active_charlie / "charlie.txt", "charlie advances active main\n")
    h.run("restart notice: charlie advances active main", h.cocodex("sync"), active_charlie, timeout=60)
    h.run("restart notice: queue active session", h.cocodex("sync"), active_worktree, timeout=60)
    h.wait_for("restart active fusing", lambda: session_row(active_repo, "alice") is not None and session_row(active_repo, "alice")["state"] == "fusing", timeout=20)
    h.terminate(active_join, "restart-active-alice-long")
    active_notice = capture_restart_join(h, active_repo, "alice", "restart notice: active task")
    h.require("unfinished sync task must be handled first" in active_notice.stdout, "join restart reports active task")
    h.terminate(active_charlie_join, "restart-active-charlie-long")
    h.terminate(active_daemon, "restart-active-daemon")


def main() -> int:
    h = Harness()
    try:
        test_package_metadata(h)
        test_init_status_config_join(h)
        test_clean_sync_and_dirty_publish(h)
        test_configured_remote_pushes_main_and_session_refs(h)
        test_direct_publish_blocked_by_dirty_main_can_resume(h)
        test_two_dirty_sessions_queue_lock(h)
        test_join_restart_notices(h)
    finally:
        h.cleanup()
        cleanup_source_build_artifacts()
        report = h.report()
        print(report)
    return 1 if h.failures else 0


def cleanup_source_build_artifacts() -> None:
    for path in [SOURCE / "UNKNOWN.egg-info", SOURCE / "src" / "cocodex.egg-info"]:
        if path.exists():
            shutil.rmtree(path)


if __name__ == "__main__":
    raise SystemExit(main())
