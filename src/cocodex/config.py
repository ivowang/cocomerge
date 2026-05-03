from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .guard import ensure_cocodex_excluded, install_main_guard


CONFIG_PATH = Path(".cocodex/config.json")
DEFAULT_DEVELOPER_COMMAND = ["codex"]
CONFIG_KEYS = {
    "main_branch",
    "remote",
    "socket_path",
    "worktree_root",
    "dirty_interval_s",
    "developers",
}


@dataclass(frozen=True)
class CocodexConfig:
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


def find_cocodex_root(start: Path | None = None) -> Path:
    cwd = Path.cwd() if start is None else start
    git_root = find_repo_root(cwd)
    candidates = [git_root, *git_root.parents]

    common_dir = _git_common_dir(cwd)
    if common_dir is not None:
        candidates.insert(0, common_dir.parent)

    for candidate in candidates:
        if (candidate / CONFIG_PATH).exists():
            return candidate.resolve()
    raise FileNotFoundError(f"{git_root / CONFIG_PATH} does not exist; run cocodex init first")


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
    force: bool = False,
) -> CocodexConfig:
    path = repo / CONFIG_PATH
    if path.exists() and not force:
        raise RuntimeError(
            f"{path} already exists. Cocodex init refuses to overwrite existing "
            "developer configuration; pass --force only if you intend to replace it."
        )
    _validate_main_branch(repo, main_branch)
    _validate_remote(repo, remote)
    config = CocodexConfig(
        main_branch=main_branch,
        remote=remote,
        socket_path=".cocodex/cocodex.sock",
        worktree_root=".cocodex/worktrees",
        dirty_interval_s=dirty_interval_s,
        developers={},
    )
    cocodex_dir = repo / ".cocodex"
    cocodex_dir.mkdir(exist_ok=True)
    _write_config_atomic(path, config)
    (cocodex_dir / "tasks").mkdir(exist_ok=True)
    (cocodex_dir / "worktrees").mkdir(exist_ok=True)
    ensure_cocodex_excluded(repo)
    install_main_guard(repo, main_branch=main_branch)
    return config


def load_config(repo: Path) -> CocodexConfig:
    path = repo / CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist; run cocodex init first")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{CONFIG_PATH} must contain a JSON object")
    data.setdefault("developers", {})
    unknown = sorted(set(data) - CONFIG_KEYS)
    if unknown:
        raise RuntimeError(
            f"Unknown key(s) in {CONFIG_PATH}: {', '.join(unknown)}. "
            "Remove obsolete or misspelled configuration keys."
        )
    missing = sorted(CONFIG_KEYS - set(data))
    if missing:
        raise RuntimeError(f"Missing required key(s) in {CONFIG_PATH}: {', '.join(missing)}")
    return CocodexConfig(**data)


def validate_config(repo: Path, config: CocodexConfig) -> None:
    _validate_main_branch(repo, config.main_branch)
    _validate_remote(repo, config.remote)
    _validate_developers(config.developers)


def _write_config_atomic(path: Path, config: CocodexConfig) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(asdict(config), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def get_developer_identity(config: CocodexConfig, name: str) -> tuple[str, str]:
    developer = _developer(config, name)
    user_name = _required_string(developer, "git_user_name", name)
    user_email = _required_string(developer, "git_user_email", name)
    return user_name, user_email


def get_developer_command(config: CocodexConfig, name: str) -> list[str]:
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


def has_developer(config: CocodexConfig, name: str) -> bool:
    return name in config.developers


def _developer(config: CocodexConfig, name: str) -> dict[str, Any]:
    try:
        developer = config.developers[name]
    except KeyError as exc:
        raise RuntimeError(
            f"Developer {name!r} is not configured in {CONFIG_PATH}. "
            "Add it under the 'developers' object before running cocodex join."
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
