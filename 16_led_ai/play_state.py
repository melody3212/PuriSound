"""14/15 재생 상태 파일 읽기."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

PLAY_STATE_PATH = Path("/tmp/play_mp3_state.json")
PLAYER_STATUS_PATH = Path("/tmp/player_ai_status.json")
PLAYER_COMMAND_PATH = Path("/tmp/player_ai_command.json")
PLAYER_DAEMON_PID_PATH = Path("/tmp/player_ai_daemon.pid")

PLAYBACK_STALE_SEC = 8.0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def player_daemon_running() -> bool:
    if not PLAYER_DAEMON_PID_PATH.exists():
        return False
    try:
        pid = int(PLAYER_DAEMON_PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return pid_alive(pid)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_play_state() -> dict | None:
    return _read_json(PLAY_STATE_PATH)


def read_player_status() -> dict | None:
    return _read_json(PLAYER_STATUS_PATH)


def read_player_command() -> dict | None:
    return _read_json(PLAYER_COMMAND_PATH)


def _normalize_track(track: dict, *, fallback: str | None = None) -> dict:
    name = track.get("name") or track.get("file") or ""
    if isinstance(name, str) and "/" in name:
        name = Path(name).name
    return {
        "file": str(track.get("path") or track.get("file") or name),
        "name": str(name),
        "volume": float(track.get("volume", 0.5)),
        "noise_type": track.get("noise_type") or fallback,
    }


def _tracks_from_payload(
    tracks: list[dict],
    *,
    fallback: str | None = None,
) -> list[dict]:
    normalized: list[dict] = []
    for track in tracks:
        if isinstance(track, dict):
            normalized.append(_normalize_track(track, fallback=fallback))
    return normalized


def _tracks_from_command(command: dict) -> list[dict]:
    fallback = None
    tracks = command.get("tracks") or []
    normalized = _tracks_from_payload(tracks, fallback=fallback)
    if normalized and not normalized[0].get("noise_type"):
        for track in normalized:
            if track.get("noise_type"):
                fallback = track["noise_type"]
                break
    if fallback:
        for track in normalized:
            track.setdefault("noise_type", fallback)
    return normalized


def playback_explicitly_stopped() -> bool:
    command = read_player_command()
    status = read_player_status()

    command_stop = command is not None and command.get("action") == "stop"
    status_silent = status is not None and not status.get("audible", False)

    if command_stop and status_silent:
        state = read_play_state()
        if not state or not state.get("tracks"):
            return True
    return False


def _tracks_from_noise_ai_command() -> tuple[list[dict], bool] | None:
    """14_noise_ai noise_controller IPC(/tmp/player_ai_command.json) 우선 해석."""
    command = read_player_command()
    if not command or command.get("source") != "14_noise_ai":
        return None

    action = command.get("action")
    if action == "stop":
        return [], False
    if action == "play":
        tracks = _tracks_from_command(command)
        if tracks:
            return tracks, True
        return [], False
    return None


def resolve_playback_tracks(
    cached_tracks: list[dict],
) -> tuple[list[dict], bool]:
    """재생 트랙과 재생 유지 여부를 반환합니다."""
    noise_ai = _tracks_from_noise_ai_command()
    if noise_ai is not None:
        return noise_ai

    state = read_play_state()
    if state:
        tracks = _tracks_from_payload(
            state.get("tracks") or [],
            fallback=state.get("primary_noise_type"),
        )
        if tracks:
            updated_at = float(state.get("updated_at", 0.0))
            playing = state.get("playing", True)
            if playing and (time.time() - updated_at) <= PLAYBACK_STALE_SEC:
                return tracks, True
            if player_daemon_running():
                return tracks, True

    status = read_player_status()
    if status:
        if status.get("audible"):
            tracks = _tracks_from_payload(status.get("tracks") or [])
            if tracks:
                return tracks, True
        if status.get("action") == "play" and status.get("tracks"):
            tracks = _tracks_from_payload(status.get("tracks") or [])
            if tracks:
                return tracks, True

    command = read_player_command()
    if command and command.get("action") == "play":
        tracks = _tracks_from_command(command)
        if tracks and player_daemon_running():
            return tracks, True

    if cached_tracks and player_daemon_running() and not playback_explicitly_stopped():
        return cached_tracks, True

    if playback_explicitly_stopped():
        return [], False

    if cached_tracks and player_daemon_running():
        return cached_tracks, True

    return [], False


def describe_tracks(tracks: list[dict]) -> str:
    if not tracks:
        return "재생 없음"
    from noise_colors import NOISE_LABELS, track_noise_type

    labels: list[str] = []
    for track in tracks:
        noise_type = track_noise_type(track)
        label = NOISE_LABELS.get(noise_type or "", noise_type or track.get("name", "?"))
        volume = int(float(track.get("volume", 0.0)) * 100)
        labels.append(f"{label} {volume}%")
    return " · ".join(labels)