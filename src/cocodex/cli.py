from __future__ import annotations

import argparse
import sys


SYNC_DAEMON_TIMEOUT = 120.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cocodex")
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{init,daemon,join,sync,status,log,delete}",
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

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("session", metavar="user_name")

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(sys.argv[1:] if argv is None else argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        from .failures import format_failure_handling

        message = str(exc)
        print(f"cocodex: {message}", file=sys.stderr)
        print(format_failure_handling(reason=message), file=sys.stderr, end="")
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
    if args.command == "delete":
        from .config import find_cocodex_root, load_config, validate_config
        from .delete import (
            DeletePartialError,
            delete_session,
            format_delete_partial,
            format_delete_refusal,
            format_delete_result,
        )
        from .state import connect, initialize_schema

        repo = find_cocodex_root()
        config = load_config(repo)
        validate_config(repo, config)
        db = connect(repo)
        initialize_schema(db)
        try:
            result = delete_session(repo, db, config, args.session)
        except DeletePartialError as exc:
            print(format_delete_partial(args.session, str(exc)), file=sys.stderr, end="")
            return 1
        except (RuntimeError, ValueError) as exc:
            print(format_delete_refusal(args.session, str(exc)), file=sys.stderr, end="")
            return 1
        print(format_delete_result(result), end="")
        if result.remote_warning is not None:
            print(
                "cocodex: warning: remote delete cleanup failed and was skipped; "
                f"local delete succeeded: {result.remote_warning}",
                file=sys.stderr,
            )
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
        remote_errors: list[str | None] = []
        if session.active_task is not None:
            socket_path = repo / config.socket_path
            if not socket_path.exists():
                raise RuntimeError("cocodex daemon is not running")
            response = send_completion(
                socket_path,
                session,
                timeout=SYNC_DAEMON_TIMEOUT,
            )
            if response.get("type") == "error":
                _print_sync_refusal(response["message"], repo=repo, session=session)
                _print_remote_sync_errors(remote_errors)
                return 1
            remote_errors.append(_sync_remote_best_effort(repo, config, session))
            print(_format_sync_completion_response(response, session))
            _print_remote_sync_errors(remote_errors)
            return 0 if response.get("type") == "ack" else 1
        socket_path = repo / config.socket_path
        if not socket_path.exists():
            raise RuntimeError("cocodex daemon is not running")
        raw = send_message(
            socket_path,
            {"type": "ready_to_integrate", "session": session.name},
            timeout=SYNC_DAEMON_TIMEOUT,
        )
        response = decode_message(raw)
        if response.get("type") == "error":
            _print_sync_refusal(response["message"], repo=repo, session=session)
            _print_remote_sync_errors(remote_errors)
            return 1
        if response.get("type") == "ack" and response.get("session") == session.name:
            remote_errors.append(_sync_remote_best_effort(repo, config, session))
            message = response.get("message") or "no changes to sync"
            print(f"{session.name}: {message}")
            _print_remote_sync_errors(remote_errors)
            return 0
        raise RuntimeError("Unexpected sync response")
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


def _print_sync_refusal(reason: str, *, repo, session) -> None:
    from .failures import format_failure_handling

    print(f"cocodex: {reason}", file=sys.stderr)
    if session.active_task is not None:
        from .tasks import task_file_path, validation_file_path

        print(
            "\n".join(
                [
                    "",
                    f"Cocodex cannot sync {session.name} yet.",
                    "",
                    f"Reason: {reason}",
                    "",
                    f"This is {session.name}'s own unfinished sync task.",
                    "",
                    f"Task: {session.active_task}",
                    f"Task file: {task_file_path(repo, session.active_task)}",
                    f"Validation file: {validation_file_path(repo, session.active_task)}",
                    "",
                    f"What {session.name} should do:",
                    "- Stay in this managed worktree.",
                    "- Finish the semantic merge described by the task file.",
                    "- Commit the candidate with the configured Git identity.",
                    "- write the validation report.",
                    "- Run `cocodex sync` again from this same worktree.",
                    "",
                    "What not to do:",
                    "- Do not run git pull, git merge main, git reset, or git push main manually.",
                    "- Do not ask another developer to sync this task.",
                    "",
                ]
            ),
            file=sys.stderr,
        )
        return
    print(
        format_failure_handling(
            reason=reason,
            session=session.name,
            state=session.state,
            active_task=session.active_task,
        ),
        file=sys.stderr,
        end="",
    )
