#!/usr/bin/env python3
"""9_send_firebase IPC 명령을 폴링해 MP3를 큐 방식으로 재생합니다."""

from __future__ import annotations

import argparse
import atexit
import collections
import fcntl
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from play_mp3 import MaskingPlayer
from player_command import (
    COMMAND_PATH,
    LOCK_PATH,
    PID_PATH,
    clear_status,
    read_command,
    write_status,
)

ROOT = Path(__file__).resolve().parent
SOUNDS_DIR = ROOT / "masking_sounds"
MASKING_SOUNDS_DIR = ROOT.parent / "10_masking" / "masking_sounds"

AUDIO_DEVICE = "plughw:CARD=Headphones,DEV=0"
NOISE_SWAP_SEC = 15.0
NOISE_SOURCES = frozenset({"9_send_firebase", "14_noise_ai"})

Action = Literal["play", "stop"]

_lock_file = None
_running = True
_shutting_down = False
_player: MaskingPlayer | None = None
_last_applied_seq: int | None = None
_pending_command: dict[str, Any] | None = None
_pending_stop: dict[str, Any] | None = None
_command_queue: collections.deque[dict[str, Any]] = collections.deque()


@dataclass
class _PlaybackState:
    seq: int
    track_keys: tuple[str, ...]
    paths: tuple[Path, ...]
    is_noise: bool
    started_at: float
    applied: list[dict[str, Any]]


_current: _PlaybackState | None = None


def noise_type_from_filename(name: str) -> str | None:
    lowered = name.lower()
    if "브라운" in name or "brown" in lowered:
        return "brown"
    if "핑크" in name or "pink" in lowered:
        return "pink"
    if "화이트" in name or "white" in lowered:
        return "white"
    return None


def resolve_track_path(track: dict[str, Any]) -> Path | None:
    name = str(track.get("name") or "")
    path_text = str(track.get("path") or "")
    candidates: list[Path] = []
    if path_text:
        candidates.append(Path(path_text))
    if name:
        candidates.extend(
            [
                SOUNDS_DIR / name,
                MASKING_SOUNDS_DIR / name,
            ]
        )
    if path_text:
        candidates.extend(
            [
                SOUNDS_DIR / Path(path_text).name,
                MASKING_SOUNDS_DIR / Path(path_text).name,
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def is_noise_command(command: dict[str, Any], track: dict[str, Any]) -> bool:
    source = str(command.get("source") or "")
    if source in NOISE_SOURCES:
        return True
    if track.get("noise_type"):
        return True
    name = str(track.get("name") or track.get("path") or "")
    return noise_type_from_filename(name) is not None


def command_tracks(command: dict[str, Any]) -> list[dict[str, Any]]:
    return [track for track in command.get("tracks") or [] if isinstance(track, dict)]


def primary_track(command: dict[str, Any]) -> dict[str, Any] | None:
    tracks = command_tracks(command)
    return tracks[0] if tracks else None


def track_keys_from_paths(paths: list[Path]) -> tuple[str, ...]:
    return tuple(sorted(path.name for path in paths))


def resolve_command_tracks(
    command: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[Path]] | None:
    resolved_tracks: list[dict[str, Any]] = []
    resolved_paths: list[Path] = []
    for track in command_tracks(command):
        mp3_path = resolve_track_path(track)
        if mp3_path is None:
            return None
        resolved_tracks.append(track)
        resolved_paths.append(mp3_path)
    if not resolved_tracks:
        return None
    return resolved_tracks, resolved_paths


def summarize_command(command: dict[str, Any] | None) -> str:
    if command is None:
        return "command=None"
    tracks = command.get("tracks") or []
    track_names = [
        str(track.get("name") or track.get("path", "?"))
        for track in tracks
        if isinstance(track, dict)
    ]
    return (
        f"seq={command.get('seq', '-')} "
        f"action={command.get('action', '-')} "
        f"source={command.get('source', '-')} "
        f"tracks={len(tracks)} "
        f"names={track_names or ['-']}"
    )


def get_player(audio_dev: str) -> MaskingPlayer:
    global _player
    if _player is None:
        _player = MaskingPlayer(audio_dev=audio_dev)
    return _player


def stop_audio() -> None:
    global _player, _current
    if _player is not None:
        _player.stop()
    _current = None


def release_lock() -> None:
    global _lock_file
    if _lock_file is None:
        return
    try:
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
        _lock_file.close()
    except OSError:
        pass
    _lock_file = None
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def shutdown(exit_code: int = 0) -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    stop_audio()
    clear_status()
    release_lock()
    raise SystemExit(exit_code)


def handle_exit(signum, _frame) -> None:
    global _running
    name = signal.Signals(signum).name
    print(f"\n종료 신호 수신 ({name}), 재생을 중지합니다.", flush=True)
    _running = False


def acquire_lock() -> None:
    global _lock_file
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        print(
            "이미 다른 player_run.py가 실행 중입니다.\n"
            "  종료: pkill -f player_run.py",
            file=sys.stderr,
        )
        sys.exit(1)

    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _lock_file = lock_file
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _build_applied_track(
    track: dict[str, Any],
    mp3_path: Path,
    *,
    volume: float,
) -> dict[str, Any]:
    noise_type = track.get("noise_type") or noise_type_from_filename(mp3_path.name)
    return {
        "file": mp3_path.name,
        "path": str(mp3_path),
        "noise_type": noise_type,
        "volume": volume,
    }


def _start_play(
    command: dict[str, Any],
    tracks: list[dict[str, Any]],
    mp3_paths: list[Path],
    *,
    audio_dev: str,
) -> list[dict[str, Any]]:
    global _current

    stop_audio()
    player = get_player(audio_dev)
    player.play_tracks(
        [
            {"path": str(mp3_path), "volume": float(track.get("volume", 1.0))}
            for track, mp3_path in zip(tracks, mp3_paths, strict=True)
        ]
    )

    applied = [
        _build_applied_track(track, mp3_path, volume=float(track.get("volume", 1.0)))
        for track, mp3_path in zip(tracks, mp3_paths, strict=True)
    ]
    seq = int(command.get("seq") or 0)
    primary = tracks[0]
    _current = _PlaybackState(
        seq=seq,
        track_keys=track_keys_from_paths(mp3_paths),
        paths=tuple(mp3_paths),
        is_noise=is_noise_command(command, primary),
        started_at=time.monotonic(),
        applied=applied,
    )

    names = ", ".join(
        f"{item['file']} ({item['noise_type'] or 'unknown'}, {item['volume']:.0%})"
        for item in applied
    )
    print(f"재생 중: {names}")
    return applied


def _seconds_since_noise_start() -> float | None:
    if _current is None or not _current.is_noise:
        return None
    return time.monotonic() - _current.started_at


def _can_swap_noise() -> bool:
    elapsed = _seconds_since_noise_start()
    return elapsed is not None and elapsed >= NOISE_SWAP_SEC


def _can_stop_playback() -> bool:
    if _current is None:
        return True
    if not _current.is_noise:
        return True
    return _can_swap_noise()


def _enqueue_command(command: dict[str, Any]) -> None:
    seq = int(command.get("seq") or 0)
    for queued in _command_queue:
        if int(queued.get("seq") or 0) == seq:
            return
    _command_queue.append(command)
    print(f"큐 대기: seq={seq} (현재 재생 중)")


def _commit_command(
    command: dict[str, Any],
    *,
    audio_dev: str,
    applied: list[dict[str, Any]] | None = None,
    audible: bool,
    error: str | None = None,
) -> None:
    global _last_applied_seq, _pending_command

    seq = int(command.get("seq") or 0)
    action = str(command.get("action") or "stop")
    write_status(
        seq=seq,
        action=action,  # type: ignore[arg-type]
        audible=audible,
        tracks=applied,
        error=error,
    )
    _last_applied_seq = seq
    if _pending_command is not None and int(_pending_command.get("seq") or 0) == seq:
        _pending_command = None


def _defer_command(command: dict[str, Any], *, reason: str) -> None:
    global _pending_command

    _pending_command = command
    elapsed = _seconds_since_noise_start()
    remaining = max(0.0, NOISE_SWAP_SEC - (elapsed or 0.0))
    print(
        f"노이즈 교체 대기: seq={command.get('seq')} "
        f"({remaining:.1f}s 후 교체 가능) — {reason}"
    )


def _defer_stop(command: dict[str, Any], *, reason: str) -> None:
    global _pending_stop

    _pending_stop = command
    elapsed = _seconds_since_noise_start()
    remaining = max(0.0, NOISE_SWAP_SEC - (elapsed or 0.0))
    print(
        f"정지 대기: seq={command.get('seq')} "
        f"({remaining:.1f}s 후 정지 가능) — {reason}"
    )


def apply_command(command: dict[str, Any], *, audio_dev: str) -> None:
    global _current, _pending_command, _pending_stop

    seq = int(command.get("seq") or 0)
    if _last_applied_seq is not None and seq == _last_applied_seq:
        return

    action = str(command.get("action") or "stop")
    print(f"명령 적용: {summarize_command(command)}")

    if action == "stop":
        if not _can_stop_playback():
            _defer_stop(command, reason="최소 재생 시간 미경과")
            return
        _pending_stop = None
        stop_audio()
        _commit_command(command, audio_dev=audio_dev, audible=False)
        print("재생 정지")
        _drain_queue(audio_dev=audio_dev)
        return

    if action != "play":
        print(f"알 수 없는 action={action!r}, 무시합니다.")
        return

    resolved = resolve_command_tracks(command)
    if resolved is None:
        tracks = command_tracks(command)
        if not tracks:
            if not _can_stop_playback():
                _defer_stop(command, reason="tracks 비어 있음")
                return
            _pending_stop = None
            stop_audio()
            _commit_command(
                command,
                audio_dev=audio_dev,
                audible=False,
                error="tracks 비어 있음",
            )
            print("tracks가 비어 있어 정지합니다.")
            _drain_queue(audio_dev=audio_dev)
            return

        track = tracks[0]
        if not _can_stop_playback():
            _defer_stop(command, reason="MP3 없음")
            return
        _pending_stop = None
        stop_audio()
        name = track.get("name") or track.get("path")
        _commit_command(
            command,
            audio_dev=audio_dev,
            audible=False,
            error=f"MP3를 찾을 수 없습니다: {name}",
        )
        print(f"재생 실패: MP3를 찾을 수 없습니다: {name}", file=sys.stderr)
        _drain_queue(audio_dev=audio_dev)
        return

    tracks, mp3_paths = resolved
    track_keys = track_keys_from_paths(mp3_paths)
    primary = tracks[0]
    is_noise = is_noise_command(command, primary)

    if _current is None:
        try:
            applied = _start_play(
                command, tracks, mp3_paths, audio_dev=audio_dev
            )
        except Exception as exc:
            stop_audio()
            _commit_command(
                command,
                audio_dev=audio_dev,
                audible=False,
                error=str(exc),
            )
            print(f"재생 실패: {exc}", file=sys.stderr)
            return
        _pending_stop = None
        _commit_command(command, audio_dev=audio_dev, applied=applied, audible=True)
        return

    if _current.track_keys == track_keys:
        player = get_player(audio_dev)
        player.update_track_volumes(
            [float(track.get("volume", 1.0)) for track in tracks]
        )
        applied = [
            _build_applied_track(
                track,
                mp3_path,
                volume=float(track.get("volume", 1.0)),
            )
            for track, mp3_path in zip(tracks, mp3_paths, strict=True)
        ]
        _current = _PlaybackState(
            seq=seq,
            track_keys=track_keys,
            paths=tuple(mp3_paths),
            is_noise=_current.is_noise,
            started_at=_current.started_at,
            applied=applied,
        )
        _pending_stop = None
        _commit_command(command, audio_dev=audio_dev, applied=applied, audible=True)
        names = ", ".join(
            f"{mp3_path.name} ({float(track.get('volume', 1.0)):.0%})"
            for track, mp3_path in zip(tracks, mp3_paths, strict=True)
        )
        print(f"음량 갱신: {names}")
        return

    if is_noise and _current.is_noise:
        if _can_swap_noise():
            previous = ", ".join(_current.track_keys)
            try:
                applied = _start_play(
                    command, tracks, mp3_paths, audio_dev=audio_dev
                )
            except Exception as exc:
                stop_audio()
                _commit_command(
                    command,
                    audio_dev=audio_dev,
                    audible=False,
                    error=str(exc),
                )
                print(f"재생 실패: {exc}", file=sys.stderr)
                return
            _pending_stop = None
            _commit_command(command, audio_dev=audio_dev, applied=applied, audible=True)
            print(f"노이즈 교체: {previous} → {', '.join(track_keys)}")
            return
        _defer_command(command, reason="동일 세션 내 노이즈 홀드")
        return

    _enqueue_command(command)


def _drain_queue(*, audio_dev: str) -> None:
    while _command_queue and _current is None:
        queued = _command_queue.popleft()
        apply_command(queued, audio_dev=audio_dev)
        if _current is not None:
            break


def try_apply_pending(*, audio_dev: str) -> None:
    if _pending_stop is not None and _can_stop_playback():
        apply_command(_pending_stop, audio_dev=audio_dev)
        return
    if _pending_command is None:
        return
    if not _can_swap_noise():
        return
    apply_command(_pending_command, audio_dev=audio_dev)


def build_local_test_command() -> dict[str, Any]:
    selected: dict[str, Any] | None = None

    for sounds_dir in (MASKING_SOUNDS_DIR, SOUNDS_DIR):
        if not sounds_dir.is_dir():
            continue
        for mp3_path in sorted(sounds_dir.glob("*.mp3")):
            noise_type = noise_type_from_filename(mp3_path.name)
            if noise_type is None:
                continue
            selected = {
                "path": str(mp3_path),
                "name": mp3_path.name,
                "volume": 0.8,
                "noise_type": noise_type,
            }
            break
        if selected is not None:
            break

    if selected is None:
        raise RuntimeError(
            f"테스트용 MP3가 없습니다: {MASKING_SOUNDS_DIR} 또는 {SOUNDS_DIR}"
        )

    return {
        "seq": 1,
        "updated_at": time.time(),
        "action": "play",
        "tracks": [selected],
        "source": "9_send_firebase",
    }


def build_local_fill_command() -> dict[str, Any]:
    selected: list[dict[str, Any]] = []

    for sounds_dir in (MASKING_SOUNDS_DIR, SOUNDS_DIR):
        if not sounds_dir.is_dir():
            continue
        for mp3_path in sorted(sounds_dir.glob("*.mp3")):
            noise_type = noise_type_from_filename(mp3_path.name)
            if noise_type is None:
                continue
            selected.append(
                {
                    "path": str(mp3_path),
                    "name": mp3_path.name,
                    "volume": 0.5,
                    "noise_type": noise_type,
                }
            )
            if len(selected) >= 2:
                break
        if len(selected) >= 2:
            break

    if not selected:
        return build_local_test_command()

    return {
        "seq": 1,
        "updated_at": time.time(),
        "action": "play",
        "tracks": selected,
        "source": "9_send_firebase",
    }


def run_loop(args: argparse.Namespace) -> None:
    global _running

    acquire_lock()
    atexit.register(stop_audio)
    atexit.register(release_lock)
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    print("=== PuriSound player (15_player__ai) ===")
    print(f"출력 장치: {args.audio_dev}")
    print(f"명령 소스: {COMMAND_PATH} (9_send_firebase IPC)")
    print(f"재생 방식: 단일 MP3 큐 (노이즈 교체 홀드 {NOISE_SWAP_SEC:.0f}s)")
    print(f"PID: {os.getpid()}")
    if args.test_local:
        print("모드: 로컬 테스트 (--test-local)")
    print("종료: Ctrl+C\n")

    if args.test_local:
        apply_command(build_local_test_command(), audio_dev=args.audio_dev)
        while _running and get_player(args.audio_dev).is_active:
            time.sleep(0.1)
        shutdown(0)

    poll_count = 0
    while _running:
        poll_count += 1
        try:
            try_apply_pending(audio_dev=args.audio_dev)
            command = read_command()
            if command is not None:
                apply_command(command, audio_dev=args.audio_dev)
            elif poll_count == 1:
                print("9_send_firebase 재생 명령 대기 중...")
        except Exception as exc:
            print(f"[오류] {exc}", flush=True)

        deadline = time.monotonic() + args.poll_interval
        while _running and time.monotonic() < deadline:
            time.sleep(0.05)

    stop_audio()
    release_lock()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="9_send_firebase IPC → pygame MP3 스트리밍 재생"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.15,
        help="명령 폴링 주기(초)",
    )
    parser.add_argument(
        "--audio-dev",
        default=AUDIO_DEVICE,
        help="ALSA 출력 장치 (AUDIODEV)",
    )
    parser.add_argument(
        "--test-local",
        action="store_true",
        help="9_send_firebase 없이 로컬 MP3 재생 테스트",
    )
    parser.add_argument(
        "--noise-swap-sec",
        type=float,
        default=NOISE_SWAP_SEC,
        help="노이즈 최소 재생·교체 대기(초). 이 시간 전에는 정지/교체 명령을 보류합니다.",
    )
    return parser


def main() -> None:
    global NOISE_SWAP_SEC

    args = build_parser().parse_args()
    NOISE_SWAP_SEC = max(0.0, float(args.noise_swap_sec))
    if args.test_local:
        args.poll_interval = 0.1
    run_loop(args)


if __name__ == "__main__":
    main()