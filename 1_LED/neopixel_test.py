#!/usr/bin/env python3
"""WS2812B 16구 네오픽셀 스트립 제어 테스트 (GPIO 10 / 물리 핀 19, SPI)."""

import os
import signal
import sys
import time

from ensure_venv import reexec_if_needed

reexec_if_needed()

from config import (
    LED_BRIGHTNESS,
    LED_CHANNEL,
    LED_COUNT,
    LED_DMA,
    LED_FREQ_HZ,
    LED_INVERT,
    LED_PIN,
)
from rpi_ws281x import Color, PixelStrip

running = True


def stop(_signum=None, _frame=None):
    global running
    running = False


def clear(strip):
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, Color(0, 0, 0))
    strip.show()


def fill(strip, color):
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, color)
    strip.show()


def rainbow_cycle(strip, wait_ms=50):
    for j in range(256):
        if not running:
            return
        for i in range(strip.numPixels()):
            strip.setPixelColor(i, wheel((i + j) & 255))
        strip.show()
        time.sleep(wait_ms / 1000.0)


def wheel(pos):
    if pos < 85:
        return Color(pos * 3, 255 - pos * 3, 0)
    if pos < 170:
        pos -= 85
        return Color(255 - pos * 3, 0, pos * 3)
    pos -= 170
    return Color(0, pos * 3, 255 - pos * 3)


def chase(strip, color, wait_ms=80):
    for i in range(strip.numPixels()):
        if not running:
            return
        clear(strip)
        strip.setPixelColor(i, color)
        strip.show()
        time.sleep(wait_ms / 1000.0)


def check_spi():
    if not os.path.exists("/dev/spidev0.0"):
        print("오류: SPI가 비활성화되어 있습니다.", file=sys.stderr)
        print("  sudo /data/1_LED/setup_spi.sh", file=sys.stderr)
        print("  sudo reboot", file=sys.stderr)
        sys.exit(1)


def main():
    global running

    check_spi()

    print(f"WS2812B 네오픽셀 테스트 — {LED_COUNT}구")
    print(f"GPIO {LED_PIN} (물리 핀 19, SPI)")
    print("Ctrl+C로 종료")

    strip = PixelStrip(
        LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA,
        LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL,
    )
    strip.begin()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        colors = [
            ("빨강", Color(255, 0, 0)),
            ("초록", Color(0, 255, 0)),
            ("파랑", Color(0, 0, 255)),
            ("흰색", Color(255, 255, 255)),
        ]
        for name, color in colors:
            if not running:
                break
            print(f"전체 {name}")
            fill(strip, color)
            time.sleep(1)

        if running:
            print("단일 LED 순차 점등")
            chase(strip, Color(255, 128, 0))

        if running:
            print("무지개 애니메이션")
            rainbow_cycle(strip)

    finally:
        clear(strip)
        print("종료 — 모든 LED OFF")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)