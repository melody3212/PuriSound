"""WS2812B LED 스트립 제어."""

from __future__ import annotations

import math
import os
import sys
import time

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
        self._base_brightness = LED_BRIGHTNESS

    @property
    def led_count(self) -> int:
        return self.strip.numPixels()

    def clear(self) -> None:
        for i in range(self.led_count):
            self.strip.setPixelColor(i, Color(0, 0, 0))
        self.strip.show()

    def fill(self, rgb: tuple[int, int, int]) -> None:
        color = Color(*rgb)
        for i in range(self.led_count):
            self.strip.setPixelColor(i, color)
        self.strip.show()

    def fill_zones(self, zones: list[tuple[int, int, int]]) -> None:
        if not zones:
            self.clear()
            return

        count = self.led_count
        zone_count = len(zones)
        for i in range(count):
            zone_index = min(int(i * zone_count / count), zone_count - 1)
            self.strip.setPixelColor(i, Color(*zones[zone_index]))
        self.strip.show()

    def pulse(
        self,
        zones: list[tuple[int, int, int]],
        phase: float,
        depth: float = 0.35,
    ) -> None:
        if not zones:
            self.clear()
            return

        factor = 1.0 - depth + depth * (0.5 + 0.5 * math.sin(phase))
        scaled = [
            tuple(int(channel * factor) for channel in rgb)
            for rgb in zones
        ]
        self.fill_zones(scaled)

    def close(self) -> None:
        self.clear()