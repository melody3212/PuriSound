#!/usr/bin/env python3
"""7_player 재생 노이즈 색상(brown/pink/white)에 맞춰 LED 색상을 동기화합니다."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ensure_venv import reexec_if_needed

reexec_if_needed()

from led_controller import LedController  # noqa: E402
from noise_colors import (  # noqa: E402
    NOISE_LABELS,
    rgb_for_noise_type,
)

LOCK_PATH = Path("/tmp/play_mp3.lock")
STATE_PATH = Path("/tmp/play_mp3_state.json")
DEFAULT_PLAYER_DIR = Path("/data/7_player")
PLAYER_PYTHON = os.environ.get("PLAYER_PYTHON", "/usr/bin/python3")
POLL_INTERVAL = 0.15
PULSE_SPEED = 2.5

running = True
_player_proc: subprocess.Popen | None = None


def stop(_signum=None, _frame=None) -> None:
    global running
    running = False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_lock_pid() -> int | None:
    if not LOCK_PATH.exists():
        return None
    try:
        return int(LOCK_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def read_play_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def active_noise_types(state: dict | None) -> list[str]:
    if not state:
        return []
    tracks = state.get("tracks") or []
    types: list[str] = []
    for track in tracks:
        noise_type = track.get("noise_type")
        if noise_type and noise_type not in types:
            types.append(noise_type)
    return types


def zones_from_noise_types(noise_types: list[str]) -> list[tuple[int, int, int]]:
    zones: list[tuple[int, int, int]] = []
    for noise_type in noise_types:
        rgb = rgb_for_noise_type(noise_type)
        if rgb:
            zones.append(rgb)
    return zones


def describe_state(state: dict | None) -> str:
    noise_types = active_noise_types(state)
    if not noise_types:
        return "재생 없음"
    labels = [NOISE_LABELS.get(nt, nt) for nt in noise_types]
    return ", ".join(labels)


def spawn_player(player_dir: Path) -> subprocess.Popen:
    script = player_dir / "play_mp3.py"
    if not script.is_file():
        raise FileNotFoundError(f"play_mp3.py 없음: {script}")
    return subprocess.Popen(
        [PLAYER_PYTHON, str(script), str(player_dir)],
        cwd=str(player_dir),
    )


def stop_player() -> None:
    global _player_proc
    if _player_proc is None:
        return
    if _player_proc.poll() is None:
        _player_proc.terminate()
        try:
            _player_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _player_proc.kill()
            _player_proc.wait(timeout=3)
    _player_proc = None


def sync_loop(led: LedController, spawn_player_flag: bool, player_dir: Path) -> None:
    global _player_proc, running

    last_label = ""
    phase = 0.0
    player_spawned = False

    if spawn_player_flag:
        print(f"7_player 실행: {player_dir}")
        _player_proc = spawn_player(player_dir)
        player_spawned = True

    try:
        while running:
            lock_pid = read_lock_pid()
            state = read_play_state()

            if player_spawned and _player_proc and _player_proc.poll() is not None:
                running = False
                break

            if lock_pid is None or not pid_alive(lock_pid):
                if last_label != "off":
                    led.clear()
                    print("LED OFF — 7_player 미실행")
                    last_label = "off"
                time.sleep(POLL_INTERVAL)
                continue

            noise_types = active_noise_types(state)
            zones = zones_from_noise_types(noise_types)
            label = describe_state(state)

            if not zones:
                if last_label != "unknown":
                    led.clear()
                    print("LED OFF — 노이즈 타입 미확인")
                    last_label = "unknown"
            else:
                if label != last_label:
                    print(f"LED 동기화: {label}")
                    last_label = label
                phase += PULSE_SPEED * POLL_INTERVAL
                led.pulse(zones, phase)

            time.sleep(POLL_INTERVAL)
    finally:
        stop_player()


def run_demo(led: LedController) -> None:
    demo_types = ["brown", "pink", "white"]
    print("데모 모드 — brown → pink → white 순환 (Ctrl+C 종료)")
    phase = 0.0
    index = 0
    while running:
        noise_type = demo_types[index % len(demo_types)]
        rgb = rgb_for_noise_type(noise_type)
        if rgb:
            label = NOISE_LABELS.get(noise_type, noise_type)
            print(f"LED: {label}")
            phase += PULSE_SPEED * POLL_INTERVAL
            led.pulse([rgb], phase)
        time.sleep(2.0)
        index += 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="7_player 노이즈 색상에 맞춰 LED 색상 동기화"
    )
    parser.add_argument(
        "--player-dir",
        type=Path,
        default=DEFAULT_PLAYER_DIR,
        help="7_player 폴더 경로",
    )
    parser.add_argument(
        "--spawn-player",
        action="store_true",
        help="7_player를 함께 실행 (play_mp3.py)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="7_player 없이 brown/pink/white LED 데모",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    led = LedController()
    print(f"LED {led.led_count}구 — GPIO 10 (SPI)")
    print(f"상태 파일: {STATE_PATH}")
    print("Ctrl+C로 종료")

    try:
        if args.demo:
            run_demo(led)
        else:
            sync_loop(led, args.spawn_player, args.player_dir.resolve())
    finally:
        led.close()
        print("종료 — 모든 LED OFF")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(1)