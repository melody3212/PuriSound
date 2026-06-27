"""MP3 마스킹 노이즈를 끊김 없이 연속 재생하기 위한 헬퍼."""

from __future__ import annotations

import threading
from typing import Any

MIXER_FREQUENCY = 44100
MIXER_SIZE = -16
MIXER_CHANNELS = 2
MIXER_BUFFER = 16384
CHANNELS_PER_TRACK = 2
LOOP_OVERLAP_SEC = 0.08


def ensure_mixer_channels(pygame: Any, max_tracks: int) -> None:
    if not pygame.mixer.get_init():
        pygame.mixer.init(
            frequency=MIXER_FREQUENCY,
            size=MIXER_SIZE,
            channels=MIXER_CHANNELS,
            buffer=MIXER_BUFFER,
        )
    pygame.mixer.set_num_channels(max(2, max_tracks * CHANNELS_PER_TRACK))


class SeamlessLooper:
    """두 채널을 교대로 겹쳐 재생해 MP3 루프 경계의 끊김을 숨깁니다."""

    def __init__(
        self,
        sound: Any,
        channels: tuple[Any, Any],
        volume: float,
    ) -> None:
        self._sound = sound
        self._channels = channels
        self._volume = max(0.0, min(1.0, float(volume)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._length = max(sound.get_length(), 0.1)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="seamless-loop",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for channel in self._channels:
            channel.stop()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))
        for channel in self._channels:
            if channel.get_busy():
                channel.set_volume(self._volume)

    def is_playing(self) -> bool:
        if self._stop.is_set():
            return False
        return any(channel.get_busy() for channel in self._channels)

    def _run(self) -> None:
        period = max(0.1, self._length - LOOP_OVERLAP_SEC)
        active = 0
        channel = self._channels[active]
        channel.set_volume(self._volume)
        channel.play(self._sound)

        while not self._stop.wait(period):
            active ^= 1
            channel = self._channels[active]
            channel.set_volume(self._volume)
            channel.play(self._sound)