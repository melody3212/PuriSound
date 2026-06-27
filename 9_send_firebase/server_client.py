"""9_send_firebase → 17_server 마스킹 재생 명령 저장."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from masking_decider import MaskingDecision

DEFAULT_SERVER_URL = "http://127.0.0.1:5000"


def decision_to_payload(decision: MaskingDecision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "tracks": [
            {
                "file_name": track.file_name,
                "volume": track.volume,
                "match_score": track.match_score,
                "fill_bands": list(track.fill_bands),
            }
            for track in decision.tracks
        ],
        "total_volume": decision.total_volume,
        "noise_type": decision.noise_type,
        "db": decision.db,
        "masking_required": decision.masking_required,
        "match_score": decision.match_score,
        "reason": decision.reason,
        "deficient_bands": list(decision.deficient_bands),
    }


def _normalize_detected_at(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def post_playback_command(
    base_url: str,
    *,
    device_id: str,
    command: dict[str, Any],
    decision: MaskingDecision,
    noise_event_id: str | None = None,
    detected_at: Any = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    payload = {
        "device_id": device_id,
        "noise_event_id": noise_event_id,
        "detected_at": _normalize_detected_at(detected_at),
        "command": command,
        "decision": decision_to_payload(decision),
    }
    url = f"{base_url.rstrip('/')}/api/playback-commands"
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("17_server 응답 형식 오류")
    return data


def check_server(base_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    url = base_url.rstrip("/")
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code < 500:
            return True, f"켜짐 ({url})"
        return False, f"응답 오류 ({url}, HTTP {response.status_code})"
    except requests.ConnectionError:
        return False, f"꺼짐 ({url}, 연결 실패)"
    except requests.Timeout:
        return False, f"꺼짐 ({url}, 응답 시간 초과)"
    except requests.RequestException as exc:
        return False, f"꺼짐 ({url}, {exc})"