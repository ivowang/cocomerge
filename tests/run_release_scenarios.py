#!/usr/bin/env python3
from __future__ import annotations

import configparser
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


def package_version() -> str:
    config = configparser.ConfigParser()
    config.read(SOURCE / "setup.cfg")
    return config["metadata"]["version"]


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
        env["COCODEX_HEADLESS_PROMPT_OK"] = "1"
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

    def popen(
        self,
        name: str,
        cmd: list[str],
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
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
            env=self.env(env),
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

    def wait_for(self, label: str, predicate: Callable[[], bool], timeout: float = 60.0) -> bool:
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


def refs(repo: Path, prefix: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "for-each-ref", "--format=%(refname)", prefix],
        cwd=repo,
        text=True,
    ).strip()
    return [line for line in output.splitlines() if line]


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


def queue_entries(repo: Path) -> list[str]:
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    try:
        rows = db.execute("SELECT session FROM queue ORDER BY position ASC").fetchall()
        return [row[0] for row in rows]
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


def start_join(
    h: Harness,
    repo: Path,
    session: str,
    name: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    proc = h.popen(name or f"{repo.name}-{session}-join", h.cocodex("join", session), repo, env=env)
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


def mark_task_disconnected(repo: Path, session: str) -> None:
    row = session_row(repo, session)
    if row is None or not row["active_task"]:
        raise AssertionError(f"{session} has no active task to disconnect")
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    try:
        db.execute(
            """
            UPDATE sessions
            SET state = 'fusing',
                blocked_reason = NULL,
                connected = 0,
                last_heartbeat = 0
            WHERE name = ?
            """,
            (session,),
        )
        db.commit()
    finally:
        db.close()


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
        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc", "*.egg-info", "build", "dist"),
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
    h.require("Name: cocodex" in metadata and f"Version: {package_version()}" in metadata, "wheel metadata has name/version")
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
    polluted_setup = Path(purelib).parent / "setup.cfg"
    write(polluted_setup, "[metadata]\nversion = 999.0.0\n")
    version_result = h.run(
        "package: installed version ignores unrelated setup.cfg",
        [
            sys.executable,
            "-c",
            "import cocodex; print(cocodex.__version__)",
        ],
        case,
        env={"PYTHONPATH": purelib},
    )
    h.require(
        version_result.stdout.strip() == package_version(),
        "installed package version is not polluted by unrelated parent setup.cfg",
    )


def test_init_status_config_join(h: Harness) -> None:
    repo = make_repo(h, "init-status-config")
    status = h.run("init/status: status after init", h.cocodex("status"), repo)
    h.require(
        "remote: none" in status.stdout
        and "guard: installed" in status.stdout
        and "lock: free" in status.stdout
        and "queue: empty" in status.stdout,
        "status works after init and shows remote config plus guard state",
    )
    exclude = read(repo / ".git" / "info" / "exclude")
    h.require("/.cocodex/" in exclude, "init excludes Cocodex runtime directory from project commits")
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
    old_task = h.run("completion: rejects removed task command", h.cocodex("task", "alice"), repo, check=False)
    h.require(old_task.returncode != 0 and "invalid choice" in old_task.stderr, "task command is removed")
    old_resume = h.run("completion: rejects removed resume command", h.cocodex("resume", "alice"), repo, check=False)
    h.require(old_resume.returncode != 0 and "invalid choice" in old_resume.stderr, "resume command is removed")
    old_abandon = h.run("completion: rejects removed abandon command", h.cocodex("abandon", "alice"), repo, check=False)
    h.require(old_abandon.returncode != 0 and "invalid choice" in old_abandon.stderr, "abandon command is removed")
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
        and "tries a normal Git merge under the lock" in agents
        and "Only if Git cannot merge cleanly" in agents
        and "behavioral union" in agents
        and "stop and ask the user" in agents
        and "server branch refs" not in agents
        and "sync requests an integration task" not in agents,
        "managed AGENTS.md describes scoped sync, semantic union, direct publish, and Git merge fast path",
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
        and "clean Git merge with lightweight checks" in prompt_check.stdout
        and "behavioral union" in prompt_check.stdout
        and "user-approved resolution" in prompt_check.stdout
        and "Cocodex recovery refs" in prompt_check.stdout
        and "does not move or notify other Cocodex session worktrees" in prompt_check.stdout
        and "remote refs" not in prompt_check.stdout,
        "sync prompt describes semantic union, conflict handling, and scoped remote sync",
    )
    tmux_prompt_delivery = h.run(
        "prompt delivery: tmux paste trims newline and does not press enter",
        [
            sys.executable,
            "-c",
            "import json, subprocess\n"
            "from unittest.mock import patch\n"
            "from cocodex.agent import send_prompt_to_tmux\n"
            "calls = []\n"
            "def fake_run(cmd, **kwargs):\n"
            "    calls.append({'cmd': cmd, 'input': kwargs.get('input')})\n"
            "    return subprocess.CompletedProcess(cmd, 0, '', '')\n"
            "with patch('subprocess.run', fake_run):\n"
            "    send_prompt_to_tmux('%1', 'hello\\n\\n', session='alice')\n"
            "print(json.dumps(calls, sort_keys=True))\n"
            "assert len(calls) == 2\n"
            "assert calls[0]['cmd'][:3] == ['tmux', 'load-buffer', '-b']\n"
            "assert calls[0]['input'] == 'hello'\n"
            "assert calls[1]['cmd'][:3] == ['tmux', 'paste-buffer', '-t']\n"
            "assert not any('send-keys' in call['cmd'] for call in calls)\n",
        ],
        repo,
    )
    h.require(
        '"input": "hello"' in tmux_prompt_delivery.stdout
        and "send-keys" not in tmux_prompt_delivery.stdout,
        "tmux prompt delivery trims trailing newlines and does not send Enter",
    )
    socket_check = h.run(
        "join: control socket path stays short",
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "from cocodex.agent import control_socket_path\n"
            "from cocodex.config import load_config\n"
            "path = control_socket_path(Path.cwd(), load_config(Path.cwd()), 'alice')\n"
            "print(path)\n",
        ],
        repo,
    )
    control_socket = socket_check.stdout.strip()
    h.require(len(control_socket) < 100 and "/cocodex-" in control_socket, "control socket uses short runtime path")
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


def test_main_guard_blocks_direct_main_writes(h: Harness) -> None:
    repo = make_repo(h, "main-guard")
    remote_repo = RUN_ROOT / "main-guard-origin.git"
    git(h, "main guard: init bare origin", remote_repo.parent, "init", "--bare", str(remote_repo))
    git(h, "main guard: add origin", repo, "remote", "add", "origin", str(remote_repo))
    start = head(repo, "main")

    write(repo / "direct-main.txt", "direct main write must fail\n")
    git(h, "main guard: stage direct main commit", repo, "add", "direct-main.txt")
    blocked_commit = git(
        h,
        "main guard: direct main commit blocked",
        repo,
        "commit",
        "-m",
        "direct main should be blocked",
        check=False,
    )
    h.require(
        blocked_commit.returncode != 0
        and "Cocodex protects main" in blocked_commit.stderr
        and head(repo, "main") == start,
        "direct commit on main is blocked before ref update",
    )
    h.run(
        "main guard: unstage direct main file",
        ["git", "reset", "--hard"],
        repo,
        env={"COCODEX_INTERNAL_WRITE": "1"},
    )

    git(h, "main guard: create side branch", repo, "checkout", "-b", "side-feature")
    write(repo / "side.txt", "side feature\n")
    git(h, "main guard: side add", repo, "add", "side.txt")
    git(h, "main guard: side commit", repo, "commit", "-m", "side feature")
    git(h, "main guard: back to main", repo, "checkout", "main")
    blocked_cherry_pick = git(
        h,
        "main guard: direct cherry-pick blocked",
        repo,
        "cherry-pick",
        "side-feature",
        check=False,
    )
    h.require(
        blocked_cherry_pick.returncode != 0
        and "Cocodex protects main" in blocked_cherry_pick.stderr
        and head(repo, "main") == start,
        "direct cherry-pick onto main is blocked",
    )
    git(h, "main guard: abort blocked cherry-pick", repo, "cherry-pick", "--abort", check=False)

    push = git(h, "main guard: direct push main blocked", repo, "push", "origin", "main", check=False)
    h.require(
        push.returncode != 0 and "Cocodex protects main" in push.stderr,
        "direct push of main is blocked by pre-push hook",
    )


def test_delete_session_cleanup_and_safety(h: Harness) -> None:
    repo = make_repo(h, "delete-session", remote=True)
    set_developer_command(repo, "alice", ["true"])
    h.run("delete: create alice session", h.cocodex("join", "alice"), repo, timeout=60)
    alice = repo / ".cocodex" / "worktrees" / "alice"
    before_branch = head(repo, "cocodex/alice")
    write(alice / "app.txt", "base\nold alice work\n")
    write(alice / "untracked-feature.txt", "old untracked alice work\n")
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    try:
        db.execute("INSERT OR IGNORE INTO queue(session) VALUES ('alice')")
        db.commit()
    finally:
        db.close()

    deleted = h.run("delete: removes dirty disconnected session", h.cocodex("delete", "alice"), repo, timeout=60)
    h.require("Deleted Cocodex session alice" in deleted.stdout, "delete reports successful session cleanup")
    h.require("Developer config was kept" in deleted.stdout, "delete explains developer config is retained")
    h.require("remote delete cleanup failed and was skipped" in deleted.stderr, "delete remote warning is non-fatal")
    h.require(session_row(repo, "alice") is None, "delete removes alice session row")
    h.require("alice" not in queue_entries(repo), "delete removes legacy queue row for alice")
    h.require(not alice.exists(), "delete removes alice worktree")
    missing_branch = git(
        h,
        "delete: local session branch removed",
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        "refs/heads/cocodex/alice",
        check=False,
    )
    h.require(missing_branch.returncode != 0, "delete removes local cocodex/alice branch")
    manifests = list((repo / ".cocodex" / "deleted").glob("*-alice.json"))
    h.require(len(manifests) == 1, "delete writes one alice manifest")
    if manifests:
        manifest = json.loads(read(manifests[0]))
        deleted_refs = refs(repo, "refs/cocodex/deleted")
        h.require(manifest["head_backup_ref"] in deleted_refs, "delete creates head backup ref")
        h.require(manifest["dirty_backup_ref"] in deleted_refs, "delete creates dirty backup ref")
        h.require(head(repo, manifest["head_backup_ref"]) == before_branch, "head backup points to old session branch")
        git(h, "delete: dirty backup ref is a commit", repo, "cat-file", "-e", f"{manifest['dirty_backup_ref']}^{{commit}}")

    set_developer_command(repo, "bob", ["true"])
    h.run("delete: create bob session", h.cocodex("join", "bob"), repo, timeout=60)
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    try:
        db.execute(
            "UPDATE sessions SET state = 'fusing', active_task = 'delete-test-task' WHERE name = 'bob'"
        )
        db.commit()
    finally:
        db.close()
    active_refused = h.run("delete: active task refused", h.cocodex("delete", "bob"), repo, check=False)
    h.require(
        active_refused.returncode != 0 and "active sync task" in active_refused.stderr,
        "delete refuses sessions with active sync tasks",
    )

    set_developer_command(repo, "charlie", ["true"])
    h.run("delete: create charlie session", h.cocodex("join", "charlie"), repo, timeout=60)
    charlie_managed = repo / ".cocodex" / "worktrees" / "charlie"
    charlie_external = repo / "charlie-external-worktree"
    git(
        h,
        "delete: move charlie branch to external worktree",
        repo,
        "worktree",
        "move",
        str(charlie_managed),
        str(charlie_external),
    )
    checked_out_refused = h.run(
        "delete: branch checked out elsewhere refused",
        h.cocodex("delete", "charlie"),
        repo,
        check=False,
    )
    h.require(
        checked_out_refused.returncode != 0 and "checked out in another worktree" in checked_out_refused.stderr,
        "delete refuses when the session branch is checked out outside the managed worktree",
    )

    live_repo = make_repo(h, "delete-live-session", interval=0.5)
    live_daemon = start_daemon(h, live_repo, "delete-live-session")
    live_join = start_join(h, live_repo, "alice", "delete-live-session-alice")
    connected_refused = h.run(
        "delete: connected session refused",
        h.cocodex("delete", "alice"),
        live_repo,
        check=False,
    )
    h.require(
        connected_refused.returncode != 0 and "still connected" in connected_refused.stderr,
        "delete refuses connected sessions",
    )
    h.terminate(live_join, "delete-live-session-alice")
    h.terminate(live_daemon, "delete-live-session-daemon")


def test_clean_sync_and_dirty_publish(h: Harness) -> None:
    repo = make_repo(h, "dirty-publish", remote=True)
    daemon = start_daemon(h, repo, "dirty-publish")
    join = start_join(h, repo, "alice", "dirty-publish-alice-join")
    bob_join = start_join(h, repo, "bob", "dirty-publish-bob-join")
    charlie_join = start_join(h, repo, "charlie", "dirty-publish-charlie-join")
    worktree = repo / ".cocodex" / "worktrees" / "alice"
    bob = repo / ".cocodex" / "worktrees" / "bob"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"
    bob_initial = head(bob)

    before = head(repo, "main")
    write(charlie / "main-only.txt", "main advanced\n")
    h.run("clean sync: charlie advances main", h.cocodex("sync"), charlie, timeout=60)
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
    write(worktree / "app.txt", "base\nalice dirty feature\n")
    write(bob / "app.txt", "base\nbob advances main first\n")
    bob_direct = h.run("dirty sync: bob advances main directly", h.cocodex("sync"), bob, timeout=60)
    h.require("published directly" in bob_direct.stdout, "bob direct publish advances main before alice sync")
    queued = h.run("dirty sync: start alice semantic task after main advanced", h.cocodex("sync"), worktree, timeout=60)
    h.require("started semantic merge task" in queued.stdout, "dirty session after main advanced starts a semantic task immediately")
    h.wait_for("alice fusing task", lambda: (session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "fusing"), timeout=20)
    task_id = latest_task_id(repo, "alice")
    task_path = repo / ".cocodex" / "tasks" / f"{task_id}.md"
    h.require(task_path.exists(), "dirty sync writes integration task file")
    task_text = read(task_path)
    h.require(
        "other sessions catch up or publish only" in task_text
        and "Required Merge Discipline" in task_text
        and "behavior is the union of latest `main` and the" in task_text
        and "stop and ask the user for a resolution" in task_text
        and "latest-main behaviors you checked still work" in task_text
        and "Cocodex recovery\nrefs" in task_text
        and "best-effort sync the configured remote" not in task_text,
        "integration task file describes semantic union and scoped publish behavior",
    )

    write(worktree / "feature.txt", "alice dirty feature\n")
    git(h, "dirty sync: candidate add", worktree, "add", "-A")
    git(h, "dirty sync: candidate commit", worktree, "commit", "-m", "alice candidate")
    missing = h.run("dirty sync: missing validation rejected", h.cocodex("sync"), worktree, check=False, timeout=60)
    missing_row = session_row(repo, "alice")
    h.require(
        missing.returncode != 0
        and "validation report is missing" in missing.stderr
        and missing_row is not None
        and missing_row["state"] == "fusing"
        and missing_row["active_task"] == task_id,
        "missing validation report is rejected without moving the active task into blocked state",
    )
    write_validation(repo, task_id, "too short")
    short = h.run("dirty sync: short validation rejected", h.cocodex("sync"), worktree, check=False, timeout=60)
    short_row = session_row(repo, "alice")
    h.require(
        short.returncode != 0
        and "validation report is too short" in short.stderr
        and short_row is not None
        and short_row["state"] == "fusing"
        and short_row["active_task"] == task_id,
        "short validation report is rejected without persistent blocked state",
    )
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

    h.terminate(charlie_join, "dirty-publish-charlie-join")
    h.terminate(bob_join, "dirty-publish-bob-join")
    h.terminate(join, "dirty-publish-alice-join")
    h.terminate(daemon, "dirty-publish-daemon")


def test_git_merge_fast_path_and_conflict_fallback(h: Harness) -> None:
    repo = make_repo(h, "git-merge-fast-path", interval=0.5)
    daemon = start_daemon(h, repo, "git-merge-fast-path")
    alice_join = start_join(h, repo, "alice", "git-merge-fast-path-alice-join")
    charlie_join = start_join(h, repo, "charlie", "git-merge-fast-path-charlie-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"

    write(alice / "alice-auto-merge.txt", "alice can merge cleanly\n")
    write(charlie / "charlie-auto-merge.txt", "charlie advances main first\n")
    charlie_publish = h.run(
        "git merge fast path: charlie advances main",
        h.cocodex("sync"),
        charlie,
        timeout=60,
    )
    h.require("published directly" in charlie_publish.stdout, "charlie direct publish advances main")
    task_files_before = set((repo / ".cocodex" / "tasks").glob("*.md"))
    alice_sync = h.run(
        "git merge fast path: alice sync after main advanced",
        h.cocodex("sync"),
        alice,
        timeout=60,
    )
    h.require("published with git merge" in alice_sync.stdout, "alice cleanly merges and publishes within the sync request")
    auto_main = head(repo, "main")
    auto_parents = git(h, "git merge fast path: main has merge parents", repo, "show", "-s", "--format=%P", "main").stdout.split()
    task_files_after = set((repo / ".cocodex" / "tasks").glob("*.md"))
    h.require("published with git merge" in h.run("git merge fast path: log", h.cocodex("log"), repo).stdout, "git merge fast path records publish reason")
    h.require(len(auto_parents) == 2, "clean divergent sync creates a Git merge commit")
    h.require(head(alice) == auto_main, "alice worktree points at auto-merged main")
    h.require((repo / "alice-auto-merge.txt").exists() and (repo / "charlie-auto-merge.txt").exists(), "auto-merged main contains both features")
    h.require(task_files_after == task_files_before, "clean Git merge does not create a Codex task file")
    h.require(status_porcelain(alice) == "", "alice worktree clean after Git merge publish")
    assert_single_lock_owner(h, repo, None, "lock released after clean Git merge publish")

    h.run("git merge fallback: charlie catches up", h.cocodex("sync"), charlie, timeout=60)
    write(alice / "app.txt", "base\nalice conflict feature\n")
    write(charlie / "app.txt", "base\ncharlie conflict feature\n")
    h.run("git merge fallback: charlie advances conflicting main", h.cocodex("sync"), charlie, timeout=60)
    fallback_start = h.run("git merge fallback: alice sync starts semantic task", h.cocodex("sync"), alice, timeout=60)
    h.require("started semantic merge task" in fallback_start.stdout, "conflicting sync starts semantic task within the sync request")
    h.wait_for(
        "git merge fallback enters fusing",
        lambda: session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "fusing",
        timeout=20,
    )
    fallback_task = latest_task_id(repo, "alice")
    h.require(
        (repo / ".cocodex" / "tasks" / f"{fallback_task}.md").exists(),
        "conflicting Git merge falls back to a Codex task",
    )
    assert_single_lock_owner(h, repo, "alice", "conflict fallback keeps lock for Codex task")

    h.terminate(charlie_join, "git-merge-fast-path-charlie-join")
    h.terminate(alice_join, "git-merge-fast-path-alice-join")
    h.terminate(daemon, "git-merge-fast-path-daemon")


def test_configured_remote_pushes_main_and_session_refs(h: Harness) -> None:
    repo = make_repo(h, "remote-push")
    remote_repo = RUN_ROOT / "remote-push-origin.git"
    git(h, "remote push: init bare origin", remote_repo.parent, "init", "--bare", str(remote_repo))
    git(h, "remote push: add origin", repo, "remote", "add", "origin", str(remote_repo))
    set_config_remote(repo, "origin")
    daemon = start_daemon(h, repo, "remote-push")
    join = start_join(h, repo, "alice", "remote-push-alice-join")
    charlie_join = start_join(h, repo, "charlie", "remote-push-charlie-join")
    worktree = repo / ".cocodex" / "worktrees" / "alice"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"

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

    write(worktree / "app.txt", "base\nalice remote snapshot feature\n")
    write(charlie / "app.txt", "base\ncharlie remote snapshot main\n")
    h.run("remote push: charlie advances main for semantic task", h.cocodex("sync"), charlie, timeout=60)
    semantic = h.run("remote push: alice starts semantic task", h.cocodex("sync"), worktree, timeout=60)
    task_id = latest_task_id(repo, "alice")
    local_snapshot = head(repo, f"refs/cocodex/snapshots/{task_id}")
    remote_snapshot = git(
        h,
        "remote push: remote receives cocodex snapshot ref",
        remote_repo,
        "rev-parse",
        f"refs/cocodex/snapshots/{task_id}",
    ).stdout.strip()
    h.require(
        "started semantic merge task" in semantic.stdout and remote_snapshot == local_snapshot,
        "configured remote receives Cocodex snapshot refs needed for recovery",
    )

    h.terminate(charlie_join, "remote-push-charlie-join")
    h.terminate(join, "remote-push-alice-join")
    h.terminate(daemon, "remote-push-daemon")


def test_direct_publish_rejects_dirty_main_without_persistent_block(h: Harness) -> None:
    repo = make_repo(h, "direct-publish-reject")
    daemon = start_daemon(h, repo, "direct-publish-reject")
    join = start_join(h, repo, "alice", "direct-publish-reject-alice-join")
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
    h.require(failed.returncode != 0, "dirty main direct publish is rejected")
    h.require(head(repo, "main") == original_main, "rejected direct publish does not move main")
    row = session_row(repo, "alice")
    h.require(
        row is not None
        and row["state"] == "clean"
        and row["active_task"] is None
        and row["blocked_reason"] is None
        and status_porcelain(worktree) != ""
        and "main worktree is dirty" in failed.stderr,
        "rejected direct publish keeps session out of blocked state and leaves worktree changes intact",
    )
    assert_single_lock_owner(h, repo, None, "lock released after rejected direct publish")

    (repo / ".python-version").unlink()
    retried = h.run("direct publish reject: retry after cleaning main", h.cocodex("sync"), worktree, timeout=60)
    h.require("published directly" in retried.stdout, "retry publishes after the unsafe main worktree is fixed")
    h.require(head(repo, "main") == head(worktree), "retry advances main from preserved session work")

    h.terminate(join, "direct-publish-reject-alice-join")
    h.terminate(daemon, "direct-publish-reject-daemon")


def test_busy_sync_rejects_second_dirty_session(h: Harness) -> None:
    repo = make_repo(h, "busy-lock")
    daemon = start_daemon(h, repo, "busy-lock")
    alice_join = start_join(h, repo, "alice", "busy-lock-alice-join")
    bob_join = start_join(h, repo, "bob", "busy-lock-bob-join")
    charlie_join = start_join(h, repo, "charlie", "busy-lock-charlie-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    bob = repo / ".cocodex" / "worktrees" / "bob"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"
    alice_initial = head(alice)
    bob_initial = head(bob)
    write(alice / "app.txt", "base\nalice queued feature\n")
    write(bob / "bob.txt", "bob queued feature\n")
    write(charlie / "app.txt", "base\ncharlie advances main before queued syncs\n")
    charlie_publish = h.run("busy lock: charlie advances main", h.cocodex("sync"), charlie, timeout=60)
    h.require("published directly" in charlie_publish.stdout, "charlie direct publish advances main before queued syncs")
    h.require(head(alice) == alice_initial and head(bob) == bob_initial, "charlie publish does not move alice or bob")
    h.run("busy lock: start alice sync", h.cocodex("sync"), alice, timeout=60)
    h.wait_for("alice first fusing", lambda: session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "fusing", timeout=20)
    alice_task = latest_task_id(repo, "alice")
    bob_row = session_row(repo, "bob")
    busy = h.run("busy lock: bob sync rejected while alice active", h.cocodex("sync"), bob, check=False, timeout=60)
    bob_row = session_row(repo, "bob")
    h.require(
        busy.returncode != 0
        and "integration busy" in busy.stderr
        and "Cocodex sync refused" in busy.stderr
        and "Retry `cocodex sync` from this worktree after that session finishes" in busy.stderr
        and bob_row is not None
        and bob_row["state"] == "clean"
        and bob_row["active_task"] is None
        and queue_entries(repo) == [],
        "second dirty session is rejected with retry guidance instead of queued while lock is busy",
    )
    assert_single_lock_owner(h, repo, "alice", "only alice holds integration lock while first task is active")

    write(alice / "alice.txt", "alice queued feature\n")
    git(h, "busy lock: alice candidate add", alice, "add", "-A")
    git(h, "busy lock: alice candidate commit", alice, "commit", "-m", "alice queue candidate")
    write_validation(
        repo,
        alice_task,
        "Validation plan: inspect alice.txt after reapplying queued feature.\n"
        "Command: cat alice.txt. Result: content matches. Remaining risk: fixture only.\n",
    )
    h.run("busy lock: publish alice", h.cocodex("sync"), alice, timeout=60)
    h.run("busy lock: bob retries sync after alice publish", h.cocodex("sync"), bob, timeout=60)
    h.wait_for(
        "bob auto-merges after alice publish",
        lambda: session_row(repo, "bob") is not None and session_row(repo, "bob")["state"] == "clean",
        timeout=20,
    )
    assert_single_lock_owner(h, repo, None, "lock free after serial queue drains")
    h.require((repo / "bob.txt").exists() and (repo / "alice.txt").exists(), "main contains both serialized publications")

    h.terminate(charlie_join, "busy-lock-charlie-join")
    h.terminate(bob_join, "busy-lock-bob-join")
    h.terminate(alice_join, "busy-lock-alice-join")
    h.terminate(daemon, "busy-lock-daemon")


def test_transient_active_task_sync_refuses_nonzero(h: Harness) -> None:
    repo = make_repo(h, "transient-active-task")
    daemon = start_daemon(h, repo, "transient-active-task")
    alice_join = start_join(h, repo, "alice", "transient-active-task-alice-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    try:
        db.execute(
            """
            UPDATE sessions
            SET state = 'snapshot', active_task = 'transient-task', blocked_reason = NULL
            WHERE name = 'alice'
            """
        )
        db.commit()
    finally:
        db.close()
    refused = h.run(
        "transient active task: sync refuses nonzero",
        h.cocodex("sync"),
        alice,
        check=False,
        timeout=60,
    )
    h.require(
        refused.returncode != 0
        and "Cocodex cannot sync alice yet." in refused.stderr
        and "This is alice's own unfinished sync task." in refused.stderr,
        "sync in transient active-task states is an actionable same-session refusal, not success",
    )
    h.terminate(alice_join, "transient-active-task-alice-join")
    h.terminate(daemon, "transient-active-task-daemon")


def test_legacy_queue_ghost_rows_are_pruned(h: Harness) -> None:
    repo = make_repo(h, "legacy-queue-prune", interval=0.5)
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    try:
        db.execute("INSERT INTO queue(session) VALUES ('ghost')")
        db.commit()
    finally:
        db.close()
    daemon = start_daemon(h, repo, "legacy-queue-prune")
    h.wait_for("legacy ghost queue pruned", lambda: queue_entries(repo) == [], timeout=20)
    status = h.run("legacy queue prune: status", h.cocodex("status"), repo, timeout=60)
    h.require("ghost" not in status.stdout, "daemon prunes legacy queue rows without matching sessions")
    h.terminate(daemon, "legacy-queue-prune-daemon")


def test_prompt_injection_failure_refuses_with_tmux_guidance(h: Harness) -> None:
    repo = make_repo(h, "prompt-injection-failure", interval=0.5)
    daemon = start_daemon(h, repo, "prompt-injection-failure")
    alice_join = start_join(
        h,
        repo,
        "alice",
        "prompt-injection-failure-alice-join",
        env={"COCODEX_HEADLESS_PROMPT_OK": "0"},
    )
    charlie_join = start_join(h, repo, "charlie", "prompt-injection-failure-charlie-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"
    write(alice / "app.txt", "base\nalice prompt feature\n")
    write(charlie / "app.txt", "base\ncharlie prompt main\n")
    h.run("prompt injection failure: charlie advances main", h.cocodex("sync"), charlie, timeout=60)
    refused = h.run(
        "prompt injection failure: alice semantic task refuses without tmux",
        h.cocodex("sync"),
        alice,
        check=False,
        timeout=60,
    )
    row = session_row(repo, "alice")
    h.require(
        refused.returncode != 0
        and "no tmux target is available" in refused.stderr
        and "Restart this developer from their own tmux pane" in refused.stderr
        and row is not None
        and row["state"] == "clean"
        and row["active_task"] is None
        and lock_row(repo)["owner"] is None
        and "alice prompt feature" in read(alice / "app.txt"),
        "prompt injection failure restores work, releases lock, and gives tmux-specific guidance",
    )
    h.terminate(charlie_join, "prompt-injection-failure-charlie-join")
    h.terminate(alice_join, "prompt-injection-failure-alice-join")
    h.terminate(daemon, "prompt-injection-failure-daemon")


def test_unknown_baseline_ahead_session_is_adopted_and_published(h: Harness) -> None:
    repo = make_repo(h, "unknown-baseline-ahead", interval=0.5)
    daemon = start_daemon(h, repo, "unknown-baseline-ahead")
    alice_join = start_join(h, repo, "alice", "unknown-baseline-ahead-alice-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    write(alice / "legacy.txt", "legacy ahead work must not be hidden\n")
    git(h, "unknown baseline: alice add", alice, "add", "-A")
    git(h, "unknown baseline: alice commit", alice, "commit", "-m", "legacy ahead work")
    candidate = head(alice)
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    try:
        db.execute("UPDATE sessions SET last_seen_main = NULL WHERE name = 'alice'")
        db.commit()
    finally:
        db.close()
    published = h.run("unknown baseline: sync adopts safe ahead branch", h.cocodex("sync"), alice, timeout=60)
    row = session_row(repo, "alice")
    h.require(
        "published directly" in published.stdout
        and head(repo, "main") == candidate
        and row is not None
        and row["last_seen_main"] == candidate,
        "unknown baseline with main ancestor is adopted and published instead of hidden",
    )
    h.terminate(alice_join, "unknown-baseline-ahead-alice-join")
    h.terminate(daemon, "unknown-baseline-ahead-daemon")


def test_active_task_integrity_handles_are_required_to_publish(h: Harness) -> None:
    repo = make_repo(h, "active-task-integrity", interval=0.5)
    daemon = start_daemon(h, repo, "active-task-integrity")
    alice_join = start_join(h, repo, "alice", "active-task-integrity-alice-join")
    charlie_join = start_join(h, repo, "charlie", "active-task-integrity-charlie-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"
    original_main = head(repo, "main")
    write(alice / "app.txt", "base\nalice integrity feature\n")
    write(charlie / "app.txt", "base\ncharlie advances integrity main\n")
    h.run("active task integrity: charlie advances main", h.cocodex("sync"), charlie, timeout=60)
    h.run("active task integrity: alice starts task", h.cocodex("sync"), alice, timeout=60)
    task_id = latest_task_id(repo, "alice")
    task_file = repo / ".cocodex" / "tasks" / f"{task_id}.md"
    task_file.unlink()
    write(alice / "integrity.txt", "candidate must not publish without task file\n")
    git(h, "active task integrity: candidate add", alice, "add", "-A")
    git(h, "active task integrity: candidate commit", alice, "commit", "-m", "alice integrity candidate")
    candidate = head(alice)
    write_validation(
        repo,
        task_id,
        "Validation plan: inspect integrity.txt.\n"
        "Command: cat integrity.txt. Result: candidate exists. Remaining risk: missing task file should block publish.\n",
    )
    refused = h.run(
        "active task integrity: missing task file refuses publish",
        h.cocodex("sync"),
        alice,
        check=False,
        timeout=60,
    )
    row = session_row(repo, "alice")
    h.require(
        refused.returncode != 0
        and "task file is missing" in refused.stderr
        and "cocodex task" not in refused.stderr
        and "cocodex abandon" not in refused.stderr
        and head(repo, "main") != candidate
        and head(repo, "main") != original_main
        and row is not None
        and row["state"] == "fusing"
        and row["active_task"] == task_id
        and lock_row(repo)["owner"] == "alice",
        "active task publish refuses when task file is missing and keeps task/lock active",
    )
    h.terminate(charlie_join, "active-task-integrity-charlie-join")
    h.terminate(alice_join, "active-task-integrity-alice-join")
    h.terminate(daemon, "active-task-integrity-daemon")


def test_startup_normalization_backs_up_before_resetting_incomplete_task(h: Harness) -> None:
    repo = make_repo(h, "startup-backup", interval=0.5)
    daemon = start_daemon(h, repo, "startup-backup")
    alice_join = start_join(h, repo, "alice", "startup-backup-alice-join")
    charlie_join = start_join(h, repo, "charlie", "startup-backup-charlie-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"
    write(alice / "app.txt", "base\nalice startup backup feature\n")
    write(charlie / "app.txt", "base\ncharlie advances startup backup main\n")
    h.run("startup backup: charlie advances main", h.cocodex("sync"), charlie, timeout=60)
    h.run("startup backup: alice starts task", h.cocodex("sync"), alice, timeout=60)
    task_id = latest_task_id(repo, "alice")
    write(alice / "candidate.txt", "committed candidate must be backed up\n")
    git(h, "startup backup: candidate add", alice, "add", "-A")
    git(h, "startup backup: candidate commit", alice, "commit", "-m", "alice startup candidate")
    write(alice / "dirty.txt", "dirty candidate work must also be backed up\n")
    task_file = repo / ".cocodex" / "tasks" / f"{task_id}.md"
    task_file.unlink()
    before_refs = set(refs(repo, "refs/cocodex/backups"))
    h.terminate(alice_join, "startup-backup-alice-join")
    h.terminate(charlie_join, "startup-backup-charlie-join")
    h.terminate(daemon, "startup-backup-daemon")

    restarted = start_daemon(h, repo, "startup-backup-restarted")
    h.wait_for(
        "startup backup normalization",
        lambda: session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "clean",
        timeout=20,
    )
    after_refs = set(refs(repo, "refs/cocodex/backups"))
    new_refs = sorted(after_refs - before_refs)
    h.require(len(new_refs) == 1, "startup normalization creates exactly one backup ref before clearing incomplete task")
    if new_refs:
        committed = git(
            h,
            "startup backup: backup contains committed candidate",
            repo,
            "show",
            f"{new_refs[0]}:candidate.txt",
        )
        dirty = git(
            h,
            "startup backup: backup contains dirty candidate",
            repo,
            "show",
            f"{new_refs[0]}:dirty.txt",
        )
        h.require(
            "committed candidate" in committed.stdout
            and "dirty candidate" in dirty.stdout
            and lock_row(repo)["owner"] is None,
            "startup backup preserves committed and dirty work before releasing lock",
        )
    h.terminate(restarted, "startup-backup-restarted")


def test_startup_normalization_repairs_lock_task_mismatch(h: Harness) -> None:
    repo = make_repo(h, "startup-lock-mismatch", interval=0.5)
    daemon = start_daemon(h, repo, "startup-lock-mismatch")
    alice_join = start_join(h, repo, "alice", "startup-lock-mismatch-alice-join")
    charlie_join = start_join(h, repo, "charlie", "startup-lock-mismatch-charlie-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"
    write(alice / "app.txt", "base\nalice mismatch feature\n")
    write(charlie / "app.txt", "base\ncharlie mismatch main\n")
    h.run("startup mismatch: charlie advances main", h.cocodex("sync"), charlie, timeout=60)
    h.run("startup mismatch: alice starts task", h.cocodex("sync"), alice, timeout=60)
    session_task = latest_task_id(repo, "alice")
    lock_task = "lock-task-mismatch"
    write(repo / ".cocodex" / "tasks" / f"{lock_task}.md", "# replacement lock task\n")
    write(alice / "mismatch-candidate.txt", "candidate at mismatch time\n")
    git(h, "startup mismatch: candidate add", alice, "add", "-A")
    git(h, "startup mismatch: candidate commit", alice, "commit", "-m", "mismatch candidate")
    before_refs = set(refs(repo, "refs/cocodex/backups"))
    db = sqlite3.connect(repo / ".cocodex" / "state.sqlite")
    try:
        db.execute("UPDATE locks SET task_id = ? WHERE name = 'integration'", (lock_task,))
        db.commit()
    finally:
        db.close()
    h.terminate(alice_join, "startup-lock-mismatch-alice-join")
    h.terminate(charlie_join, "startup-lock-mismatch-charlie-join")
    h.terminate(daemon, "startup-lock-mismatch-daemon")

    restarted = start_daemon(h, repo, "startup-lock-mismatch-restarted")
    h.wait_for(
        "startup lock mismatch normalization",
        lambda: session_row(repo, "alice") is not None and session_row(repo, "alice")["active_task"] == lock_task,
        timeout=20,
    )
    row = session_row(repo, "alice")
    after_refs = set(refs(repo, "refs/cocodex/backups"))
    new_refs = sorted(after_refs - before_refs)
    h.require(
        row is not None
        and row["state"] == "fusing"
        and row["active_task"] == lock_task
        and lock_row(repo)["task_id"] == lock_task
        and session_task != lock_task
        and len(new_refs) == 1,
        "startup normalization backs up and repairs session active_task/lock task mismatch",
    )
    h.terminate(restarted, "startup-lock-mismatch-restarted")


def test_active_task_rejoin_and_sync_guidance(h: Harness) -> None:
    repo = make_repo(h, "task-rejoin", interval=0.5)
    daemon = start_daemon(h, repo, "task-rejoin")
    alice_join = start_join(h, repo, "alice", "task-rejoin-alice-join")
    charlie_join = start_join(h, repo, "charlie", "task-rejoin-charlie-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"
    write(alice / "app.txt", "base\nalice recovery feature\n")
    write(charlie / "app.txt", "base\ncharlie advances main first\n")
    h.run("task rejoin: charlie advances main", h.cocodex("sync"), charlie, timeout=60)
    started = h.run("task rejoin: alice starts fusion", h.cocodex("sync"), alice, timeout=60)
    h.require("started semantic merge task" in started.stdout, "active semantic task starts synchronously")
    h.wait_for("task rejoin alice fusing", lambda: session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "fusing", timeout=20)
    task_id = latest_task_id(repo, "alice")

    h.terminate(alice_join, "task-rejoin-alice-join")
    mark_task_disconnected(repo, "alice")
    rejoin_notice = capture_restart_join(h, repo, "alice", "task rejoin: active task notice")
    row = session_row(repo, "alice")
    h.require(
        "unfinished sync task must be handled first" in rejoin_notice.stdout
        and row is not None
        and row["state"] == "fusing"
        and row["active_task"] == task_id
        and lock_row(repo)["owner"] == "alice",
        "rejoining the session preserves the active task under the existing integration lock",
    )
    task_status = h.run("task rejoin: status shows active task handles", h.cocodex("status"), repo, timeout=60)
    h.require(
        task_id in task_status.stdout
        and "Task file:" in task_status.stdout
        and "Snapshot ref:" in task_status.stdout
        and "Base ref:" in task_status.stdout
        and "Next step:" in task_status.stdout,
        "status exposes active task files, refs, and next step without a task command",
    )

    not_ready = h.run("task rejoin: sync explains same-session active task requirements", h.cocodex("sync"), alice, check=False, timeout=60)
    row = session_row(repo, "alice")
    h.require(
        not_ready.returncode != 0
        and "Cocodex cannot sync alice yet." in not_ready.stderr
        and "This is alice's own unfinished sync task." in not_ready.stderr
        and "Task file:" in not_ready.stderr
        and "Validation file:" in not_ready.stderr
        and "write the validation report" in not_ready.stderr
        and row is not None
        and row["state"] == "fusing"
        and row["active_task"] == task_id
        and lock_row(repo)["owner"] == "alice",
        "same-session sync refusal explains how to finish the active task without manual recovery commands",
    )

    write(alice / "app.txt", "base\ncharlie advances main first\nalice recovery feature\n")
    git(h, "task rejoin: candidate add", alice, "add", "-A")
    git(h, "task rejoin: candidate commit", alice, "commit", "-m", "alice recovered candidate")
    write_validation(
        repo,
        task_id,
        "Validation plan: inspect app.txt after rejoin and confirm both lines are present.\n"
        "Command: cat app.txt. Result: charlie and alice feature lines are both present. Remaining risk: fixture only.\n",
    )
    published = h.run("task rejoin: sync publishes recovered active task", h.cocodex("sync"), alice, timeout=60)
    h.require(
        "Published alice" in published.stdout
        and lock_row(repo)["owner"] is None
        and session_row(repo, "alice")["active_task"] is None,
        "same session completes active task through cocodex sync without abandon or resume",
    )

    h.terminate(charlie_join, "task-rejoin-charlie-join")
    h.terminate(daemon, "task-rejoin-daemon")


def test_disconnected_active_task_owner_can_complete_and_others_get_actionable_busy(h: Harness) -> None:
    repo = make_repo(h, "disconnected-active-task", interval=0.5)
    daemon = start_daemon(h, repo, "disconnected-active-task")
    alice_join = start_join(h, repo, "alice", "disconnected-active-task-alice-join")
    bob_join = start_join(h, repo, "bob", "disconnected-active-task-bob-join")
    charlie_join = start_join(h, repo, "charlie", "disconnected-active-task-charlie-join")
    alice = repo / ".cocodex" / "worktrees" / "alice"
    bob = repo / ".cocodex" / "worktrees" / "bob"
    charlie = repo / ".cocodex" / "worktrees" / "charlie"

    write(alice / "app.txt", "base\nalice disconnected active feature\n")
    write(charlie / "app.txt", "base\ncharlie advances main first\n")
    h.run("disconnected active task: charlie advances main", h.cocodex("sync"), charlie, timeout=60)
    started = h.run("disconnected active task: alice starts semantic task", h.cocodex("sync"), alice, timeout=60)
    h.require("started semantic merge task" in started.stdout, "alice semantic task starts before disconnect")
    h.wait_for(
        "disconnected active task alice fusing",
        lambda: session_row(repo, "alice") is not None and session_row(repo, "alice")["state"] == "fusing",
        timeout=20,
    )
    task_id = latest_task_id(repo, "alice")
    h.terminate(alice_join, "disconnected-active-task-alice-join")
    mark_task_disconnected(repo, "alice")
    row = session_row(repo, "alice")
    h.require(
        row is not None
        and row["state"] == "fusing"
        and row["active_task"] == task_id
        and row["blocked_reason"] is None,
        "disconnect keeps active task fusing instead of converting it to recovery_required",
    )
    assert_single_lock_owner(h, repo, "alice", "disconnected active task keeps alice lock")

    write(bob / "bob.txt", "bob waits while alice recovers\n")
    busy = h.run("disconnected active task: bob gets actionable busy message", h.cocodex("sync"), bob, check=False, timeout=60)
    h.require(
        busy.returncode != 0
        and "integration busy: alice is disconnected while syncing" in busy.stderr
        and "cocodex join alice" in busy.stderr
        and "cocodex sync" in busy.stderr
        and "cocodex task" not in busy.stderr
        and "cocodex abandon" not in busy.stderr,
        "busy sync points to lock owner join and sync while owner is disconnected",
    )

    write(alice / "app.txt", "base\ncharlie advances main first\nalice disconnected active feature\n")
    git(h, "disconnected active task: alice candidate add", alice, "add", "-A")
    git(h, "disconnected active task: alice candidate commit", alice, "commit", "-m", "alice recovered candidate")
    write_validation(
        repo,
        task_id,
        "Validation plan: inspect app.txt after recovery and confirm both lines are present.\n"
        "Command: cat app.txt. Result: charlie and alice feature lines are both present. Remaining risk: fixture only.\n",
    )
    published = h.run("disconnected active task: alice publishes recovered candidate", h.cocodex("sync"), alice, timeout=60)
    h.require("Published alice" in published.stdout, "same session can publish committed candidate after disconnect")
    h.require(head(repo, "main") == head(alice), "main equals recovered alice candidate")
    h.require("alice disconnected active feature" in read(repo / "app.txt"), "main includes recovered alice feature")
    assert_single_lock_owner(h, repo, None, "disconnected active task publish releases lock")

    h.terminate(charlie_join, "disconnected-active-task-charlie-join")
    h.terminate(bob_join, "disconnected-active-task-bob-join")
    h.terminate(daemon, "disconnected-active-task-daemon")


def test_version_mismatch_blocks_stale_agent(h: Harness) -> None:
    repo = make_repo(h, "version-mismatch")
    daemon = start_daemon(h, repo, "version-mismatch")
    join = start_join(h, repo, "alice", "version-mismatch-alice-join")
    worktree = repo / ".cocodex" / "worktrees" / "alice"
    h.terminate(join, "version-mismatch-alice-join")
    mismatch = h.run(
        "version mismatch: stale heartbeat blocks session",
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "from cocodex.protocol import decode_message\n"
            "from cocodex.transport import send_message\n"
            "repo = Path.cwd()\n"
            "raw = send_message(repo / '.cocodex' / 'cocodex.sock', {\n"
            "    'type': 'heartbeat',\n"
            "    'session': 'alice',\n"
            "    'agent_version': '0.0-stale',\n"
            "}, timeout=5)\n"
            "print(decode_message(raw))\n",
        ],
        repo,
        timeout=60,
    )
    h.require("'type': 'ack'" in mismatch.stdout, "stale heartbeat receives protocol ack")
    h.wait_for(
        "version mismatch recorded without blocked state",
        lambda: (
            session_row(repo, "alice") is not None
            and session_row(repo, "alice")["state"] == "clean"
            and session_row(repo, "alice")["blocked_reason"] is None
        ),
        timeout=5,
    )
    status = h.run("version mismatch: status reports versions", h.cocodex("status"), repo, timeout=60)
    h.require(
        "daemon_version:" in status.stdout
        and "agent_version=0.0-stale" in status.stdout
        and "version_mismatch=true" in status.stdout,
        "status exposes stale agent version mismatch without blocking the session",
    )
    blocked = h.run("version mismatch: sync refuses stale agent session", h.cocodex("sync"), worktree, check=False, timeout=60)
    h.require(
        blocked.returncode != 0
        and "version mismatch" in blocked.stderr
        and "Restart `cocodex join alice` after upgrading Cocodex" in blocked.stderr,
        "sync refuses stale agent heartbeat with restart guidance but no persistent blocked state",
    )

    h.terminate(daemon, "version-mismatch-daemon")


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

    active_repo = make_repo(h, "restart-active", interval=0.5)
    active_daemon = start_daemon(h, active_repo, "restart-active")
    active_join = start_join(h, active_repo, "alice", "restart-active-alice-long")
    active_charlie_join = start_join(h, active_repo, "charlie", "restart-active-charlie-long")
    active_worktree = active_repo / ".cocodex" / "worktrees" / "alice"
    active_charlie = active_repo / ".cocodex" / "worktrees" / "charlie"
    write(active_worktree / "app.txt", "base\nactive work\n")
    write(active_charlie / "app.txt", "base\ncharlie advances active main\n")
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
        test_main_guard_blocks_direct_main_writes(h)
        test_delete_session_cleanup_and_safety(h)
        test_clean_sync_and_dirty_publish(h)
        test_git_merge_fast_path_and_conflict_fallback(h)
        test_configured_remote_pushes_main_and_session_refs(h)
        test_direct_publish_rejects_dirty_main_without_persistent_block(h)
        test_busy_sync_rejects_second_dirty_session(h)
        test_transient_active_task_sync_refuses_nonzero(h)
        test_legacy_queue_ghost_rows_are_pruned(h)
        test_prompt_injection_failure_refuses_with_tmux_guidance(h)
        test_unknown_baseline_ahead_session_is_adopted_and_published(h)
        test_active_task_integrity_handles_are_required_to_publish(h)
        test_startup_normalization_backs_up_before_resetting_incomplete_task(h)
        test_startup_normalization_repairs_lock_task_mismatch(h)
        test_active_task_rejoin_and_sync_guidance(h)
        test_disconnected_active_task_owner_can_complete_and_others_get_actionable_busy(h)
        test_version_mismatch_blocks_stale_agent(h)
        test_join_restart_notices(h)
    finally:
        h.cleanup()
        cleanup_source_build_artifacts()
        report = h.report()
        print(report)
        if h.failures:
            print(report.read_text(encoding="utf-8", errors="replace"))
    return 1 if h.failures else 0


def cleanup_source_build_artifacts() -> None:
    for path in [SOURCE / "UNKNOWN.egg-info", SOURCE / "src" / "cocodex.egg-info"]:
        if path.exists():
            shutil.rmtree(path)


if __name__ == "__main__":
    raise SystemExit(main())
