from __future__ import annotations

import errno
import socket
import stat
import threading
from pathlib import Path
from typing import Callable

from .protocol import decode_message, encode_message


Handler = Callable[[dict], dict]
ACCEPTED_CONNECTION_TIMEOUT = 0.1


def _read_line(conn: socket.socket) -> bytes:
    chunks = bytearray()
    while True:
        try:
            chunk = conn.recv(4096)
        except socket.timeout as exc:
            raise TimeoutError("timed out waiting for socket response") from exc
        if not chunk:
            return bytes(chunks)
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.extend(chunk[: newline + 1])
            return bytes(chunks)
        chunks.extend(chunk)


def send_message(socket_path: Path, message: dict, *, timeout: float | None = None) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        if timeout is not None:
            client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(encode_message(message))
        client.shutdown(socket.SHUT_WR)
        return _read_line(client)


def serve_once(socket_path: Path, handler: Handler) -> threading.Thread:
    server = _listening_socket(socket_path)
    return threading.Thread(target=_serve_once, args=(server, socket_path, handler), daemon=True)


def serve_forever(
    socket_path: Path,
    handler: Handler,
    *,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    event = threading.Event() if stop_event is None else stop_event
    server = _listening_socket(socket_path)
    return threading.Thread(
        target=_serve_forever,
        args=(server, socket_path, handler, event),
        daemon=True,
    )


def _serve_once(server: socket.socket, socket_path: Path, handler: Handler) -> None:
    with server:
        try:
            conn, _ = server.accept()
            with conn:
                _handle_connection(conn, handler)
        finally:
            _unlink_socket(socket_path)


def _serve_forever(
    server: socket.socket,
    socket_path: Path,
    handler: Handler,
    stop_event: threading.Event,
) -> None:
    with server:
        server.settimeout(0.1)
        try:
            while not stop_event.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                with conn:
                    conn.settimeout(ACCEPTED_CONNECTION_TIMEOUT)
                    try:
                        _handle_connection(conn, handler)
                    except Exception:
                        continue
        finally:
            _unlink_socket(socket_path)


def _listening_socket(socket_path: Path) -> socket.socket:
    prepare_socket_path(socket_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        server.listen()
        return server
    except Exception:
        server.close()
        raise


def _handle_connection(conn: socket.socket, handler: Handler) -> None:
    try:
        message = decode_message(_read_line(conn))
        response = handler(message)
        payload = encode_message(response)
    except Exception as exc:
        payload = encode_message(_error_response(exc))
    conn.sendall(payload)


def _error_response(exc: Exception) -> dict[str, str]:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else "request failed"
    return {"type": "error", "message": message[:200]}


def prepare_socket_path(socket_path: Path) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    _unlink_stale_socket(socket_path)


def _unlink_stale_socket(socket_path: Path) -> None:
    try:
        mode = socket_path.stat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"socket path exists and is not a socket: {socket_path}")
    if _socket_accepts_connections(socket_path):
        raise RuntimeError(f"cocodex daemon is already running at {socket_path}")
    _unlink_socket(socket_path)


def _socket_accepts_connections(socket_path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(socket_path))
            return True
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
        return False
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
            return False
        raise


def _unlink_socket(socket_path: Path) -> None:
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
