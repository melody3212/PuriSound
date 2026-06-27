#!/usr/bin/env python3
"""WS2812B LED 스트립 — 재생 트랙 기반 그라데이션.

직접 실행 시 14_noise_ai noise_controller IPC를 폴링해 LED를 표시합니다.
"""

from __future__ import annotations

import argparse
import atexit
import fcntl
import os
import signal
import sys
import time
from pathlib import Path

from ensure_venv import reexec_if_needed

reexec_if_needed()

LED_DIR = "/data/1_LED"
if LED_DIR not in sys.path:
    sys.path.insert(0, LED_DIR)

from config import (  # noqa: E402
    LED_BRIGHTNESS,
    LED_CHANNEL,
    LED_COUNT,
    LED_DMA,
    LED_FREQ_HZ,
    LED_INVERT,
    LED_PIN,
)
from rpi_ws281x import Color, PixelStrip  # noqa: E402

from noise_colors import (  # noqa: E402
    scale_rgb,
    spectrum_for_noise_type,
    track_noise_type,
)

GRADIENT_CYCLES = 2.0


def lerp_rgb(
    left: tuple[int, int, int],
    right: tuple[int, int, int],
    t: float,
) -> tuple[int, int, int]:
    clamped = max(0.0, min(1.0, t))
    return tuple(
        int(left[channel] + (right[channel] - left[channel]) * clamped)
        for channel in range(3)
    )


def build_color_stops(tracks: list[dict]) -> tuple[list[tuple[int, int, int]], float]:
    fallback = track_noise_type(tracks[0])
    colors: list[tuple[int, int, int]] = []
    volumes: list[float] = []

    for track in tracks:
        noise_type = track_noise_type(track, fallback=fallback)
        spectrum = spectrum_for_noise_type(noise_type)
        colors.extend(spectrum)
        volumes.append(float(track.get("volume", 0.5)))

    if not colors:
        return [(0, 0, 0)], 0.0

    avg_volume = sum(volumes) / len(volumes)
    master_brightness = 0.7 + 0.3 * max(0.0, min(1.0, avg_volume))
    return colors, master_brightness


def sample_color_gradient(
    colors: list[tuple[int, int, int]],
    position: float,
    *,
    master_brightness: float = 1.0,
) -> tuple[int, int, int]:
    if not colors:
        return (0, 0, 0)
    if len(colors) == 1:
        return scale_rgb(colors[0], master_brightness)

    wrapped = position % 1.0
    scaled = wrapped * len(colors)
    stop = int(scaled) % len(colors)
    blend = scaled - int(scaled)
    next_stop = (stop + 1) % len(colors)
    rgb = lerp_rgb(colors[stop], colors[next_stop], blend)
    return scale_rgb(rgb, master_brightness)


class LedController:
    def __init__(self) -> None:
        if not os.path.exists("/dev/spidev0.0"):
            raise RuntimeError(
                "SPI 비활성화 — sudo /data/1_LED/setup_spi.sh 후 재부팅"
            )
        self.strip = PixelStrip(
            LED_COUNT,
            LED_PIN,
            LED_FREQ_HZ,
            LED_DMA,
            LED_INVERT,
            LED_BRIGHTNESS,
            LED_CHANNEL,
        )
        self.strip.begin()

    @property
    def led_count(self) -> int:
        return self.strip.numPixels()

    def clear(self) -> None:
        for index in range(self.led_count):
            self.strip.setPixelColor(index, Color(0, 0, 0))
        self.strip.show()

    def render_gradient(
        self,
        tracks: list[dict],
        *,
        phase: float,
    ) -> None:
        if not tracks:
            self.clear()
            return

        colors, master_brightness = build_color_stops(tracks)
        rotation = phase % 1.0
        count = self.led_count

        for index in range(count):
            ring_pos = index / count
            grad_pos = ((ring_pos + rotation) * GRADIENT_CYCLES) % 1.0
            rgb = sample_color_gradient(
                colors,
                grad_pos,
                master_brightness=master_brightness,
            )
            self.strip.setPixelColor(index, Color(*rgb))

        self.strip.show()

    def close(self) -> None:
        self.clear()


POLL_INTERVAL = 0.12
ROTATION_SPEED = 0.1
OFF_CONFIRM_POLLS = 4
LED_LOCK_PATH = Path("/tmp/led_ai.lock")
LED_PID_PATH = Path("/tmp/led_ai.pid")

_running = True
_lock_file = None


def _stop(_signum=None, _frame=None) -> None:
    global _running
    _running = False


def _acquire_led_lock() -> None:
    global _lock_file
    LED_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LED_LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        print(
            "이미 16_led_ai LED 프로세스가 실행 중입니다.\n"
            "  종료: pkill -f led_controller.py",
            file=sys.stderr,
        )
        sys.exit(1)

    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _lock_file = lock_file
    LED_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _release_led_lock() -> None:
    global _lock_file
    if _lock_file is not None:
        try:
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
            _lock_file.close()
        except OSError:
            pass
        _lock_file = None
    try:
        LED_LOCK_PATH.unlink(missing_ok=True)
        LED_PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def run_foreground(*, poll_interval: float = POLL_INTERVAL) -> None:
    from play_state import describe_tracks, resolve_playback_tracks

    _acquire_led_lock()
    atexit.register(_release_led_lock)

    led = LedController()
    print("=== 16_led_ai LED (14_noise_ai 연동) ===")
    print(f"LED {led.led_count}구 — GPIO 10 (SPI)")
    print("재생 명령: /tmp/player_ai_command.json (14_noise_ai)")
    print(f"PID: {os.getpid()}")
    print("종료: Ctrl+C\n", flush=True)

    cached_tracks: list[dict] = []
    last_label = ""
    phase = 0.0
    off_streak = 0

    try:
        while _running:
            tracks, active = resolve_playback_tracks(cached_tracks)

            if tracks:
                cached_tracks = tracks
                off_streak = 0
            elif not active:
                off_streak += 1
                if off_streak >= OFF_CONFIRM_POLLS:
                    cached_tracks = []
            else:
                off_streak = 0

            label = describe_tracks(cached_tracks)

            if not cached_tracks:
                if last_label != "off":
                    led.clear()
                    print("LED OFF — 재생 없음", flush=True)
                    last_label = "off"
            else:
                if label != last_label:
                    print(f"LED ACTIVE — {label}", flush=True)
                    last_label = label
                phase += ROTATION_SPEED * poll_interval
                led.render_gradient(cached_tracks, phase=phase)

            time.sleep(poll_interval)
    finally:
        led.close()
        _release_led_lock()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="14_noise_ai 재생 명령에 맞춰 LED 그라데이션 표시"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=POLL_INTERVAL,
        help="14_noise_ai IPC 폴링 주기(초)",
    )
    return parser


def main() -> None:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    args = build_parser().parse_args()
    run_foreground(poll_interval=args.poll_interval)


if __name__ == "__main__":
    main()