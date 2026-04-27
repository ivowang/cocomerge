from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(".cocomerge/config.json")
DEFAULT_DEVELOPER_COMMAND = ["codex"]


@dataclass(frozen=True)
class CocomergeConfig:
    main_branch: str
    remote: str | None
    socket_path: str
    worktree_root: str
    dirty_interval_s: float
    developers: dict[str, dict[str, Any]] = field(default_factory=dict)


def find_repo_root(start: Path | None = None) -> Path:
    cwd = Path.cwd() if start is None else start
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "not inside a Git repository")
    return Path(result.stdout.strip()).resolve()


def find_cocomerge_root(start: Path | None = None) -> Path:
    cwd = Path.cwd() if start is None else start
    git_root = find_repo_root(cwd)
    candidates = [git_root, *git_root.parents]

    common_dir = _git_common_dir(cwd)
    if common_dir is not None:
        candidates.insert(0, common_dir.parent)

    for candidate in candidates:
        if (candidate / CONFIG_PATH).exists():
            return candidate.resolve()
    raise FileNotFoundError(f"{git_root / CONFIG_PATH} does not exist; run cocomerge init first")


def _git_common_dir(cwd: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    common_dir = Path(result.stdout.strip())
    return common_dir if common_dir.is_absolute() else (cwd / common_dir).resolve()


def init_config(
    repo: Path,
    *,
    main_branch: str,
    remote: str | None,
    dirty_interval_s: float = 2.0,
) -> CocomergeConfig:
    _validate_main_branch(repo, main_branch)
    _validate_remote(repo, remote)
    config = CocomergeConfig(
        main_branch=main_branch,
        remote=remote,
        socket_path=".cocomerge/cocomerge.sock",
        worktree_root=".cocomerge/worktrees",
        dirty_interval_s=dirty_interval_s,
        developers={},
    )
    cocomerge_dir = repo / ".cocomerge"
    cocomerge_dir.mkdir(exist_ok=True)
    (repo / CONFIG_PATH).write_text(
        json.dumps(asdict(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (cocomerge_dir / "tasks").mkdir(exist_ok=True)
    (cocomerge_dir / "worktrees").mkdir(exist_ok=True)
    return config


def load_config(repo: Path) -> CocomergeConfig:
    path = repo / CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist; run cocomerge init first")
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("verify", None)
    data.setdefault("developers", {})
    return CocomergeConfig(**data)


def validate_config(repo: Path, config: CocomergeConfig) -> None:
    _validate_main_branch(repo, config.main_branch)
    _validate_remote(repo, config.remote)
    _validate_developers(config.developers)


def get_developer_identity(config: CocomergeConfig, name: str) -> tuple[str, str]:
    developer = _developer(config, name)
    user_name = _required_string(developer, "git_user_name", name)
    user_email = _required_string(developer, "git_user_email", name)
    return user_name, user_email


def get_developer_command(config: CocomergeConfig, name: str) -> list[str]:
    developer = _developer(config, name)
    command = developer.get("command", DEFAULT_DEVELOPER_COMMAND)
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        raise RuntimeError(
            f"Developer {name!r} has invalid command in {CONFIG_PATH}; "
            "use a non-empty JSON string array such as [\"codex\"]."
        )
    return list(command)


def has_developer(config: CocomergeConfig, name: str) -> bool:
    return name in config.developers


def _developer(config: CocomergeConfig, name: str) -> dict[str, Any]:
    try:
        developer = config.developers[name]
    except KeyError as exc:
        raise RuntimeError(
            f"Developer {name!r} is not configured in {CONFIG_PATH}. "
            "Add it under the 'developers' object before running cocomerge join."
        ) from exc
    if not isinstance(developer, dict):
        raise RuntimeError(f"Developer {name!r} in {CONFIG_PATH} must be a JSON object")
    return developer


def _required_string(developer: dict[str, Any], key: str, name: str) -> str:
    value = developer.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"Developer {name!r} is missing required field {key!r} in {CONFIG_PATH}"
        )
    return value


def _validate_developers(developers: dict[str, dict[str, Any]]) -> None:
    if not isinstance(developers, dict):
        raise RuntimeError(f"'developers' in {CONFIG_PATH} must be a JSON object")
    for name, developer in developers.items():
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"Developer names in {CONFIG_PATH} must be non-empty strings")
        if not isinstance(developer, dict):
            raise RuntimeError(f"Developer {name!r} in {CONFIG_PATH} must be a JSON object")
        command = developer.get("command")
        if command is not None and (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise RuntimeError(
                f"Developer {name!r} has invalid command in {CONFIG_PATH}; "
                "use a non-empty JSON string array."
            )


def _validate_main_branch(repo: Path, branch: str) -> None:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    ).stdout.strip()
    current_text = f" Current branch is '{current}'." if current else ""
    raise RuntimeError(
        f"Main branch '{branch}' does not exist.{current_text} "
        "Create an initial commit on that branch or pass --main <existing-branch>."
    )


def _validate_remote(repo: Path, remote: str | None) -> None:
    if remote is None:
        return
    result = subprocess.run(
        ["git", "remote", "get-url", remote],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Remote '{remote}' does not exist. Add it with "
            f"`git remote add {remote} <url>` or omit --remote."
        )
