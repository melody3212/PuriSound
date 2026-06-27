#!/usr/bin/env python3
"""통합 상태 파일을 읽어 노이즈 타입에 맞는 LED 색상을 표시합니다 (1_LED venv 전용)."""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import time
from pathlib import Path

LED_VENV = Path("/data/1_LED/venv/bin/python3")
if Path(sys.executable).resolve() != LED_VENV.resolve() and LED_VENV.is_file():
    os.execv(str(LED_VENV), [str(LED_VENV), *sys.argv])

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

from noise_colors import NOISE_LABELS, rgb_for_noise_type  # noqa: E402
from state_sync import STATE_PATH  # noqa: E402

POLL_INTERVAL = 0.15
PULSE_SPEED = 2.5

running = True


def stop(_signum=None, _frame=None) -> None:
    global running
    running = False


def read_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class LedDriver:
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

    def clear(self) -> None:
        for i in range(self.strip.numPixels()):
            self.strip.setPixelColor(i, Color(0, 0, 0))
        self.strip.show()

    def pulse(self, rgb: tuple[int, int, int], phase: float) -> None:
        depth = 0.35
        factor = 1.0 - depth + depth * (0.5 + 0.5 * math.sin(phase))
        color = Color(
            int(rgb[0] * factor),
            int(rgb[1] * factor),
            int(rgb[2] * factor),
        )
        for i in range(self.strip.numPixels()):
            self.strip.setPixelColor(i, color)
        self.strip.show()

    def close(self) -> None:
        self.clear()


def main() -> None:
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    led = LedDriver()
    print(f"LED 워커 시작 — {LED_COUNT}구, 상태: {STATE_PATH}")
    print("Ctrl+C로 종료")

    last_label = ""
    phase = 0.0

    try:
        while running:
            state = read_state()
            if not state:
                noise_type = "idle"
            else:
                noise_type = state.get("noise_type") or "idle"
            rgb = rgb_for_noise_type(noise_type)
            if rgb is None:
                noise_type = "idle"
                rgb = rgb_for_noise_type("idle")

            label = NOISE_LABELS.get(noise_type, noise_type)
            if label != last_label:
                print(f"LED: {label}")
                last_label = label
            phase += PULSE_SPEED * POLL_INTERVAL
            led.pulse(rgb, phase)

            time.sleep(POLL_INTERVAL)
    finally:
        led.close()
        print("LED 워커 종료")


if __name__ == "__main__":
    main()