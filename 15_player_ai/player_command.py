"""9_send_firebase ↔ 15_player_ai 재생 명령 IPC."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

COMMAND_PATH = Path("/tmp/player_ai_command.json")
STATUS_PATH = Path("/tmp/player_ai_status.json")
LOCK_PATH = Path("/tmp/player_ai.lock")
PID_PATH = Path("/tmp/player_ai_daemon.pid")

Action = Literal["play", "stop"]


def build_command(
    *,
    seq: int,
    action: Action,
    tracks: list[dict[str, Any]] | None = None,
    source: str = "9_send_firebase",
) -> dict[str, Any]:
    return {
        "seq": seq,
        "updated_at": time.time(),
        "action": action,
        "tracks": tracks or [],
        "source": source,
    }


def read_command() -> dict[str, Any] | None:
    if not COMMAND_PATH.exists():
        return None
    try:
        data = json.loads(COMMAND_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_status(
    *,
    seq: int,
    action: Action,
    audible: bool,
    tracks: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    payload = {
        "seq": seq,
        "action": action,
        "applied_at": time.time(),
        "audible": audible,
        "tracks": tracks or [],
        "error": error,
    }
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATUS_PATH)


def clear_status() -> None:
    try:
        STATUS_PATH.unlink(missing_ok=True)
    except OSError:
        pass