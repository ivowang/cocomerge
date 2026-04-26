from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coconut")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--main", default="main")
    init_parser.add_argument("--verify")
    init_parser.add_argument("--remote")

    subparsers.add_parser("daemon")

    join_parser = subparsers.add_parser("join")
    join_parser.add_argument("--name", required=True)
    join_parser.add_argument("session_command", nargs=argparse.REMAINDER)

    subparsers.add_parser("status")

    subparsers.add_parser("log")

    ready_parser = subparsers.add_parser("ready")
    ready_parser.add_argument("session")

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("session")

    abandon_parser = subparsers.add_parser("abandon")
    abandon_parser.add_argument("session")

    done_parser = subparsers.add_parser("done")
    done_parser.add_argument("session")

    block_parser = subparsers.add_parser("block")
    block_parser.add_argument("session")
    block_parser.add_argument("reason")

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"coconut: {exc}", file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        from .config import find_repo_root, init_config
        from .state import connect, initialize_schema

        repo = find_repo_root()
        init_config(repo, main_branch=args.main, verify=args.verify, remote=args.remote)
        db = connect(repo)
        initialize_schema(db)
        print(f"Initialized coconut in {repo / '.coconut'}")
        return 0
    if args.command == "daemon":
        from .config import find_repo_root, load_config, validate_config
        from .daemon import run_daemon
        from .state import connect, initialize_schema

        repo = find_repo_root()
        config = load_config(repo)
        validate_config(repo, config)
        db = connect(repo)
        initialize_schema(db)
        return run_daemon(repo, db, config)
    if args.command == "join":
        import os

        from .agent import SessionAgent
        from .config import find_repo_root, load_config, validate_config
        from .session import ensure_session_worktree, register_with_daemon
        from .state import connect, initialize_schema

        repo = find_repo_root()
        config = load_config(repo)
        validate_config(repo, config)
        db = connect(repo)
        initialize_schema(db)
        record = ensure_session_worktree(repo, config, db, args.name)
        command = args.session_command
        if command and command[0] == "--":
            command = command[1:]
        agent = SessionAgent(repo=repo, config=config, record=record, command=command)
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
        from .config import find_repo_root, load_config, validate_config
        from .state import connect, initialize_schema
        from .status import format_status

        repo = find_repo_root()
        config = load_config(repo)
        validate_config(repo, config)
        db = connect(repo)
        initialize_schema(db)
        print(format_status(repo, db, config), end="")
        return 0
    if args.command == "log":
        from .config import find_repo_root, load_config, validate_config
        from .state import connect, initialize_schema
        from .status import format_events

        repo = find_repo_root()
        validate_config(repo, load_config(repo))
        db = connect(repo)
        initialize_schema(db)
        print(format_events(db), end="")
        return 0
    if args.command == "ready":
        from .config import find_repo_root, load_config, validate_config
        from .protocol import decode_message
        from .state import connect, get_session, initialize_schema
        from .transport import send_message

        repo = find_repo_root()
        config = load_config(repo)
        validate_config(repo, config)
        db = connect(repo)
        initialize_schema(db)
        session = get_session(db, args.session)
        if session is None:
            raise RuntimeError(f"Unknown session: {args.session}")
        socket_path = repo / config.socket_path
        if not socket_path.exists():
            raise RuntimeError("coconut daemon is not running")
        raw = send_message(
            socket_path,
            {"type": "ready_to_integrate", "session": args.session},
            timeout=5,
        )
        response = decode_message(raw)
        if response.get("type") == "error":
            raise RuntimeError(response["message"])
        if response.get("type") == "queued" and response.get("session") == args.session:
            print(f"Queued {args.session}")
            return 0
        if response.get("type") == "ack" and response.get("session") == args.session:
            message = response.get("message") or "no changes to integrate"
            print(f"{args.session}: {message}")
            return 0
        raise RuntimeError("Unexpected ready response")
    if args.command == "resume":
        from .config import find_repo_root, load_config
        from .state import (
            connect,
            enqueue_session,
            get_lock,
            get_session,
            initialize_schema,
            transition_session,
        )

        repo = find_repo_root()
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
                "retry done after resolving the task or abandon it"
            )
        transition_session(db, args.session, "queued", reason="manual resume", active_task=None)
        enqueue_session(db, args.session)
        print(f"Resumed {args.session}")
        return 0
    if args.command == "abandon":
        from .config import find_repo_root, load_config
        from .state import (
            connect,
            dequeue_session,
            get_lock,
            get_session,
            initialize_schema,
            set_lock,
            transition_session,
        )

        repo = find_repo_root()
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
    if args.command == "done":
        from .config import find_repo_root, load_config
        from .session import send_completion
        from .state import connect, get_session, initialize_schema

        repo = find_repo_root()
        config = load_config(repo)
        db = connect(repo)
        initialize_schema(db)
        session = get_session(db, args.session)
        if session is None:
            raise RuntimeError(f"Unknown session: {args.session}")
        response = send_completion(repo / config.socket_path, session)
        print(_format_completion_response(response, session, expected_type="ack"))
        return 0
    if args.command == "block":
        from .config import find_repo_root, load_config
        from .session import send_completion
        from .state import connect, get_session, initialize_schema

        repo = find_repo_root()
        config = load_config(repo)
        db = connect(repo)
        initialize_schema(db)
        session = get_session(db, args.session)
        if session is None:
            raise RuntimeError(f"Unknown session: {args.session}")
        response = send_completion(
            repo / config.socket_path,
            session,
            blocked_reason=args.reason,
        )
        print(_format_completion_response(response, session, expected_type="blocked"))
        return 0

    return 0


def _format_completion_response(response: dict, session, *, expected_type: str) -> str:
    if response.get("type") == "error":
        raise RuntimeError(response["message"])
    message_type = response.get("type", "response")
    response_session = response.get("session")
    task_id = response.get("task_id")
    if (
        message_type != expected_type
        or response_session != session.name
        or task_id != session.active_task
    ):
        raise RuntimeError("Unexpected completion response")
    return f"{message_type} {response_session} {task_id}"
