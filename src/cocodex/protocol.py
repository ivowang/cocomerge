from __future__ import annotations

import json
from typing import Any


class ProtocolError(ValueError):
    pass


TASK_MESSAGE_TYPES = {
    "freeze",
    "start_fusion",
    "blocked",
    "freeze_ack",
    "freeze_busy",
    "fusion_done",
}

KNOWN_TYPES = TASK_MESSAGE_TYPES | {
    "ack",
    "register",
    "registered",
    "heartbeat",
    "queued",
    "ready_to_integrate",
    "shutdown",
    "main_updated",
    "error",
}


def encode_message(message: dict[str, Any]) -> bytes:
    validate_message(message)
    return (json.dumps(message, sort_keys=True) + "\n").encode("utf-8")


def decode_message(raw: bytes) -> dict[str, Any]:
    try:
        message = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"invalid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")
    validate_message(message)
    return message


def validate_message(message: dict[str, Any]) -> None:
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")
    message_type = message.get("type")
    if not isinstance(message_type, str):
        raise ProtocolError("message type is required")
    if message_type not in KNOWN_TYPES:
        raise ProtocolError(f"unknown message type: {message_type}")
    if message_type in TASK_MESSAGE_TYPES and not message.get("task_id"):
        raise ProtocolError(f"{message_type} requires task_id")
    if message_type == "start_fusion" and not message.get("task_file"):
        raise ProtocolError("start_fusion requires task_file")
    if message_type == "main_updated" and not message.get("main_commit"):
        raise ProtocolError("main_updated requires main_commit")
    if message_type in {"register", "heartbeat", "ready_to_integrate", "shutdown"}:
        if not message.get("session"):
            raise ProtocolError(f"{message_type} requires session")
    if message_type == "error" and not isinstance(message.get("message"), str):
        raise ProtocolError("error requires message")
