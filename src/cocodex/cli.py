from __future__ import annotations

import argparse
import sys


RECOVERY_COMMANDS = {"resume", "abandon"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cocodex")
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{init,daemon,join,sync,status,log}",
        required=True,
    )

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--main", default="main")
    init_parser.add_argument("--remote")
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing .cocodex/config.json",
    )

    subparsers.add_parser("daemon")

    join_parser = subparsers.add_parser("join")
    join_parser.add_argument("session", nargs="?", metavar="user_name")
    join_parser.add_argument(
        "--tmux-target",
        help="override the tmux pane that should receive Cocodex prompts",
    )

    subparsers.add_parser("status")

    subparsers.add_parser("log")

    subparsers.add_parser("sync")

    return parser


def build_recovery_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cocodex")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("session")

    abandon_parser = subparsers.add_parser("abandon")
    abandon_parser.add_argument("session")

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw and raw[0] in RECOVERY_COMMANDS:
        return build_recovery_parser().parse_args(raw)
    return build_parser().parse_args(raw)


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"cocodex: {exc}", file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.command == "init":
        from .config import find_repo_root, init_config
        from .state import connect, initialize_schema

        repo = find_repo_root()
        init_config(repo, main_branch=args.main, remote=args.remote, force=args.force)
        db = connect(repo)
        initialize_schema(db)
        print(f"Initialized cocodex in {repo / '.cocodex'}")
        return 0
    if args.command == "daemon":
        from .config import find_cocodex_root, load_config, validate_config
        from .daemon import run_daemon
        from .state import connect, initialize_schema

        repo = find_cocodex_root()
        config = load_config(repo)
        validate_config(repo, config)
        db = connect(repo)
        initialize_schema(db)
        return run_daemon(repo, db, config)
    if args.command == "join":
        import os

        from .agent import SessionAgent
        from .config import (
            find_cocodex_root,
            get_developer_command,
            get_developer_identity,
            has_developer,
            load_config,
            validate_config,
        )
        from .session import ensure_session_worktree, prepare_join_startup_notice, register_with_daemon
        from .state import connect, initialize_schema

        repo = find_cocodex_root()
        config = load_config(repo)
        validate_config(repo, config)
        db = connect(repo)
        initialize_schema(db)
        session_name = args.session
        if session_name is None:
            raise RuntimeError("Usage: cocodex join <user_name>")
        if not has_developer(config, session_name):
            raise RuntimeError(
                f"Developer {session_name!r} is not configured in .cocodex/config.json"
            )
        git_user_name, git_user_email = get_developer_identity(config, session_name)
        command = get_developer_command(config, session_name)
        record = ensure_session_worktree(
            repo,
            config,
            db,
            session_name,
            git_user_name=git_user_name,
            git_user_email=git_user_email,
        )
        record, startup_prompt = prepare_join_startup_notice(repo, config, db, record)
        agent = SessionAgent(
            repo=repo,
            config=config,
            record=record,
            command=command,
            tmux_target=_resolve_tmux_target(args.tmux_target),
            startup_prompt=startup_prompt,
        )
        control_thread = agent.start_control_server(wait=True)
        try:
            response = register_with_daemon(
                repo / config.socket_path,
                record,
                os.getpid(),
                control_socket=str(agent.control_socket),
            )
            if response is not None and response.get("type") == "error":
                raise RuntimeError(response["message"])
            return agent.run(control_thread=control_thread)
        except Exception:
            agent.stop_event.set()
            control_thread.join(timeout=2)
            raise
    if args.command == "status":
        from .config import find_cocodex_root, load_config, validate_config
        from .state import connect, initialize_schema
        from .status import format_status

        repo = find_cocodex_root()
        config = load_config(repo)
        validate_config(repo, config)
        db = connect(repo)
        initialize_schema(db)
        print(format_status(repo, db, config), end="")
        return 0
    if args.command == "log":
        from .config import find_cocodex_root, load_config, validate_config
        from .state import connect, initialize_schema
        from .status import format_events

        repo = find_cocodex_root()
        validate_config(repo, load_config(repo))
        db = connect(repo)
        initialize_schema(db)
        print(format_events(db), end="")
        return 0
    if args.command == "sync":
        from .config import find_cocodex_root, load_config, validate_config
        from .protocol import decode_message
        from .session import infer_session_from_cwd, send_completion
        from .state import connect, initialize_schema
        from .transport import send_message

        repo = find_cocodex_root()
        config = load_config(repo)
        validate_config(repo, config)
        db = connect(repo)
        initialize_schema(db)
        session = infer_session_from_cwd(db)
        remote_errors = [_sync_remote_best_effort(repo, config, session)]
        if session.state == "blocked" and session.active_task is None:
            reason = f": {session.blocked_reason}" if session.blocked_reason else ""
            print(
                f"{session.name}: blocked{reason}\n"
                f"Fix the blocker, then run `cocodex resume {session.name}` from the project repository."
            )
            _print_remote_sync_errors(remote_errors)
            return 1
        if session.active_task is not None:
            if session.state in {"fusing", "blocked", "recovery_required"}:
                response = send_completion(repo / config.socket_path, session)
                remote_errors.append(_sync_remote_best_effort(repo, config, session))
                print(_format_sync_completion_response(response, session))
                _print_remote_sync_errors(remote_errors)
                return 0 if response.get("type") == "ack" else 1
            print(f"{session.name}: sync already in progress ({session.state})")
            _print_remote_sync_errors(remote_errors)
            return 0
        socket_path = repo / config.socket_path
        if not socket_path.exists():
            raise RuntimeError("cocodex daemon is not running")
        raw = send_message(
            socket_path,
            {"type": "ready_to_integrate", "session": session.name},
            timeout=5,
        )
        response = decode_message(raw)
        if response.get("type") == "error":
            raise RuntimeError(response["message"])
        if response.get("type") == "queued" and response.get("session") == session.name:
            print(f"Queued {session.name} for sync")
            _print_remote_sync_errors(remote_errors)
            return 0
        if response.get("type") == "ack" and response.get("session") == session.name:
            remote_errors.append(_sync_remote_best_effort(repo, config, session))
            message = response.get("message") or "no changes to sync"
            print(f"{session.name}: {message}")
            _print_remote_sync_errors(remote_errors)
            return 0
        raise RuntimeError("Unexpected sync response")
    if args.command == "resume":
        from .config import find_cocodex_root, load_config
        from .state import (
            connect,
            enqueue_session,
            get_lock,
            get_session,
            initialize_schema,
            transition_session,
        )

        repo = find_cocodex_root()
        load_config(repo)
        db = connect(repo)
        initialize_schema(db)
        session = get_session(db, args.session)
        if session is None:
            raise RuntimeError(f"Unknown session: {args.session}")
        if session.state not in {"blocked", "recovery_required"}:
            raise RuntimeError(f"Cannot resume {args.session} from {session.state}")
        lock = get_lock(db)
        if session.active_task is not None and lock == {
            "owner": args.session,
            "task_id": session.active_task,
        }:
            raise RuntimeError(
                f"Cannot resume {args.session} while integration lock is held; "
                "retry sync after resolving the task or abandon it"
            )
        transition_session(db, args.session, "queued", reason="manual resume", active_task=None)
        enqueue_session(db, args.session)
        print(f"Resumed {args.session}")
        return 0
    if args.command == "abandon":
        from .config import find_cocodex_root, load_config
        from .state import (
            connect,
            dequeue_session,
            get_lock,
            get_session,
            initialize_schema,
            set_lock,
            transition_session,
        )

        repo = find_cocodex_root()
        load_config(repo)
        db = connect(repo)
        initialize_schema(db)
        session = get_session(db, args.session)
        if session is None:
            raise RuntimeError(f"Unknown session: {args.session}")
        old_active_task = session.active_task
        transition_session(db, args.session, "abandoned", reason="manual abandon", active_task=None)
        dequeue_session(db, args.session)
        lock = get_lock(db)
        if lock is not None and lock["owner"] == args.session and (
            old_active_task is None or lock["task_id"] == old_active_task
        ):
            set_lock(db, owner=None, task_id=None)
        print(f"Abandoned {args.session}")
        return 0
    return 0


def _resolve_tmux_target(explicit_target: str | None) -> str | None:
    if explicit_target is not None:
        return explicit_target or None
    import os

    if _truthy_env(os.environ.get("COCODEX_NO_TMUX")):
        return None
    return os.environ.get("TMUX_PANE") or None


def _truthy_env(value: str | None) -> bool:
    return value is not None and value.lower() not in {"", "0", "false", "no", "off"}


def _format_sync_completion_response(response: dict, session) -> str:
    if response.get("type") == "error":
        raise RuntimeError(response["message"])
    response_session = response.get("session")
    task_id = response.get("task_id")
    if response_session != session.name or task_id != session.active_task:
        raise RuntimeError("Unexpected sync completion response")
    if response.get("type") == "ack":
        return f"Published {response_session} {task_id}"
    if response.get("type") == "blocked":
        reason = response.get("reason") or "blocked"
        return f"Blocked {response_session} {task_id}: {reason}"
    raise RuntimeError("Unexpected sync completion response")


def _sync_remote_best_effort(repo, config, session) -> str | None:
    from .git import try_force_push_session_refs

    error = try_force_push_session_refs(
        repo,
        config.remote,
        main_branch=config.main_branch,
        session_branch=session.branch,
    )
    if error is None:
        return None
    return (
        f"remote sync to {config.remote} failed and was skipped; "
        f"will retry on the next cocodex sync: {error}"
    )


def _print_remote_sync_errors(errors: list[str | None]) -> None:
    seen: set[str] = set()
    for error in errors:
        if error is None or error in seen:
            continue
        seen.add(error)
        print(f"cocodex: warning: {error}", file=sys.stderr)
