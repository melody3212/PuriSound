"""JSON 파일 기반 영속 저장소."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_LOCK = threading.Lock()


def _path(name: str) -> Path:
    return _DATA_DIR / f"{name}.json"


def _load(name: str) -> list[dict[str, Any]]:
    path = _path(name)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save(name: str, records: list[dict[str, Any]]) -> None:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_record(name: str, record: dict[str, Any], *, max_records: int = 500) -> dict[str, Any]:
    with _LOCK:
        records = _load(name)
        records.insert(0, record)
        if len(records) > max_records:
            records = records[:max_records]
        _save(name, records)
    return record


def list_records(
    name: str,
    *,
    device_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with _LOCK:
        records = _load(name)

    if device_id:
        records = [row for row in records if row.get("device_id") == device_id]
    return records[:limit]


def latest_record(name: str, *, device_id: str | None = None) -> dict[str, Any] | None:
    rows = list_records(name, device_id=device_id, limit=1)
    return rows[0] if rows else None