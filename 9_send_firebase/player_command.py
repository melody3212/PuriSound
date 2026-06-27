"""9_send_firebase → 15_player_ai 재생 명령 IPC."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

COMMAND_PATH = Path("/tmp/player_ai_command.json")
STATUS_PATH = Path("/tmp/player_ai_status.json")
PLAYER_LOCK_PATH = Path("/tmp/player_ai.lock")
PLAYER_PID_PATH = Path("/tmp/player_ai_daemon.pid")

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


def write_command(command: dict[str, Any]) -> None:
    COMMAND_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = COMMAND_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")
    tmp.replace(COMMAND_PATH)