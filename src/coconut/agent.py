from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from .config import CoconutConfig
from .protocol import ProtocolError, decode_message
from .state import SessionRecord
from .transport import send_message, serve_forever


def control_socket_path(repo: Path, config: CoconutConfig, session: str) -> Path:
    return repo / ".coconut" / "sessions" / f"{session}.sock"


class SessionAgent:
    def __init__(
        self,
        *,
        repo: Path,
        config: CoconutConfig,
        record: SessionRecord,
        command: list[str],
        tmux_target: str | None = None,
        stop_event: threading.Event | None = None,
        heartbeat_interval: float = 2.0,
    ) -> None:
        self.repo = repo
        self.config = config
        self.record = record
        self.command = command
        self.tmux_target = tmux_target
        self.stop_event = stop_event or threading.Event()
        self.heartbeat_interval = heartbeat_interval
        self.control_socket = control_socket_path(repo, config, record.name)

    def start_control_server(self, *, wait: bool = False, timeout: float = 2.0) -> threading.Thread:
        thread = serve_forever(self.control_socket, self.handle_command, stop_event=self.stop_event)
        thread.start()
        try:
            if wait:
                wait_for_control_socket(self.control_socket, self.record.name, timeout=timeout)
        except Exception:
            self.stop_event.set()
            thread.join(timeout=2)
            raise
        return thread

    def start_heartbeat(self) -> threading.Thread:
        thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        thread.start()
        return thread

    def run(self, *, control_thread: threading.Thread | None = None) -> int:
        control_thread = control_thread or self.start_control_server()
        heartbeat_thread = self.start_heartbeat()
        try:
            if not self.command:
                print(str(Path(self.record.worktree)))
                return 0
            return subprocess.call(self.command, cwd=self.record.worktree)
        finally:
            self.stop_event.set()
            self._send_daemon({"type": "shutdown", "session": self.record.name})
            control_thread.join(timeout=2)
            heartbeat_thread.join(timeout=2)

    def handle_command(self, message: dict) -> dict:
        task_id = message.get("task_id")
        message_type = message.get("type")
        if message_type == "freeze":
            if self.stop_event.is_set():
                return {
                    "type": "freeze_busy",
                    "session": self.record.name,
                    "task_id": task_id,
                    "reason": "agent stopping",
                }
            return {"type": "freeze_ack", "session": self.record.name, "task_id": task_id}
        if message_type == "start_fusion":
            task_file = message["task_file"]
            prompt = build_sync_prompt(self.record.name, Path(task_file))
            prompt_path = write_prompt_file(Path(task_file), prompt)
            print(f"Coconut task for {self.record.name}: {task_file}", flush=True)
            print(f"Coconut prompt for {self.record.name}: {prompt_path}", flush=True)
            response = {"type": "ack", "session": self.record.name, "task_id": task_id}
            if self.tmux_target:
                try:
                    send_prompt_to_tmux(self.tmux_target, prompt, session=self.record.name)
                    response["prompt_injected"] = True
                except RuntimeError as exc:
                    print(f"Coconut prompt injection failed: {exc}", flush=True)
                    response["prompt_injected"] = False
                    response["prompt_error"] = str(exc)
            return response
        if message_type == "main_updated":
            return {
                "type": "ack",
                "session": self.record.name,
                "main_commit": message.get("main_commit"),
            }
        if message_type == "shutdown":
            self.stop_event.set()
            return {"type": "ack", "session": self.record.name}
        return {"type": "ack", "session": self.record.name}

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.heartbeat_interval):
            self._send_daemon({"type": "heartbeat", "session": self.record.name})

    def _send_daemon(self, message: dict) -> dict | None:
        socket_path = self.repo / self.config.socket_path
        if not socket_path.exists():
            return None
        try:
            raw = send_message(socket_path, message, timeout=2)
            return decode_message(raw)
        except (OSError, TimeoutError, ProtocolError):
            return None


def wait_for_control_socket(socket_path: Path, session: str, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    message = {"type": "freeze", "session": session, "task_id": "control-ready"}
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            decode_message(send_message(socket_path, message, timeout=0.05))
            return
        except (OSError, TimeoutError, ProtocolError) as exc:
            last_error = exc
            time.sleep(0.01)
    raise TimeoutError(f"control socket did not become ready: {socket_path}") from last_error


def build_sync_prompt(session: str, task_file: Path) -> str:
    return "\n".join(
        [
            "Coconut sync task is ready.",
            "",
            f"Session: {session}",
            f"Task file: {task_file}",
            "",
            "Read the task file now. Treat the current worktree as the latest main branch.",
            "Re-implement or semantically merge the snapshot feature described there on top",
            "of this latest main. Do not run git pull, git merge main, or git push main.",
            "",
            "When the candidate is complete:",
            "1. run the configured verification or the task's recommended checks;",
            "2. commit the final candidate with this session's configured Git identity;",
            "3. ensure the worktree is clean;",
            "4. run `coconut sync` again from this worktree so Coconut can verify and publish it.",
            "",
            "If you cannot complete the task safely, stop and explain the blocker in this",
            "session output. Do not run sync again until a candidate is actually ready.",
            "",
        ]
    )


def write_prompt_file(task_file: Path, prompt: str) -> Path:
    prompt_path = task_file.with_name(task_file.stem + ".prompt.md")
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def send_prompt_to_tmux(target: str, prompt: str, *, session: str) -> None:
    safe_session = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in session)
    buffer_name = f"coconut-{safe_session}"
    load = subprocess.run(
        ["tmux", "load-buffer", "-b", buffer_name, "-"],
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if load.returncode != 0:
        raise RuntimeError(load.stderr.strip() or "tmux load-buffer failed")
    paste = subprocess.run(
        ["tmux", "paste-buffer", "-t", target, "-b", buffer_name],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if paste.returncode != 0:
        raise RuntimeError(paste.stderr.strip() or "tmux paste-buffer failed")
    enter = subprocess.run(
        ["tmux", "send-keys", "-t", target, "Enter"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if enter.returncode != 0:
        raise RuntimeError(enter.stderr.strip() or "tmux send-keys failed")


def run_agent(
    repo: Path,
    config: CoconutConfig,
    record: SessionRecord,
    command: list[str],
    *,
    agent: SessionAgent | None = None,
    control_thread: threading.Thread | None = None,
) -> int:
    agent = agent or SessionAgent(repo=repo, config=config, record=record, command=command)
    return agent.run(control_thread=control_thread)
