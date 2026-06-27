"""통합 상태 파일 — LED 워커와 공유."""

from __future__ import annotations

import json
import time
from pathlib import Path

STATE_PATH = Path("/tmp/purisound_total_state.json")


def write_state(
    *,
    playing: bool,
    noise_type: str | None,
    masking_file: str | None,
    db: int,
    masking_required: bool,
    label: str = "",
    status: str = "active",
) -> None:
    state = {
        "playing": playing,
        "noise_type": noise_type,
        "masking_file": masking_file,
        "db": db,
        "masking_required": masking_required,
        "label": label,
        "status": status,
        "updated_at": time.time(),
    }
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def clear_state() -> None:
    try:
        STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass