#!/usr/bin/env python3
"""WS2812B LED 기본 테스트 — 실행하면 16구 LED가 점등됩니다."""

from __future__ import annotations

import os
import signal
import sys
import time

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

running = True


def stop(_signum=None, _frame=None) -> None:
    global running
    running = False


def clear(strip: PixelStrip) -> None:
    for index in range(strip.numPixels()):
        strip.setPixelColor(index, Color(0, 0, 0))
    strip.show()


def fill(strip: PixelStrip, color: int) -> None:
    for index in range(strip.numPixels()):
        strip.setPixelColor(index, color)
    strip.show()


def check_spi() -> None:
    if not os.path.exists("/dev/spidev0.0"):
        print("오류: SPI가 비활성화되어 있습니다.", file=sys.stderr)
        print("  sudo /data/1_LED/setup_spi.sh", file=sys.stderr)
        print("  sudo reboot", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    global running

    check_spi()

    print(f"=== LED 기본 테스트 — {LED_COUNT}구 ===")
    print(f"GPIO {LED_PIN} (물리 핀 19, SPI)")
    print("Ctrl+C로 종료\n", flush=True)

    strip = PixelStrip(
        LED_COUNT,
        LED_PIN,
        LED_FREQ_HZ,
        LED_DMA,
        LED_INVERT,
        LED_BRIGHTNESS,
        LED_CHANNEL,
    )
    strip.begin()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    colors = [
        ("빨강", Color(255, 0, 0)),
        ("초록", Color(0, 255, 0)),
        ("파랑", Color(0, 0, 255)),
        ("흰색", Color(255, 255, 255)),
    ]

    try:
        for name, color in colors:
            if not running:
                break
            print(f"전체 {name}", flush=True)
            fill(strip, color)
            time.sleep(1.5)

        if running:
            print("무지개 그라데이션", flush=True)
            for offset in range(256):
                if not running:
                    break
                for index in range(strip.numPixels()):
                    pos = (index * 256 // strip.numPixels() + offset) & 255
                    if pos < 85:
                        rgb = Color(pos * 3, 255 - pos * 3, 0)
                    elif pos < 170:
                        pos -= 85
                        rgb = Color(255 - pos * 3, 0, pos * 3)
                    else:
                        pos -= 170
                        rgb = Color(0, pos * 3, 255 - pos * 3)
                    strip.setPixelColor(index, rgb)
                strip.show()
                time.sleep(0.02)
    finally:
        clear(strip)
        print("종료 — 모든 LED OFF", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(1)