"""pygame 기반 마스킹 MP3 단일 트랙 재생기 (7_player 방식)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("SDL_AUDIODRIVER", "alsa")
os.environ.setdefault("AUDIODEV", "plughw:CARD=Headphones,DEV=0")

import pygame  # noqa: E402


class MaskingPlayer:
    def __init__(self, volume: float = 0.7) -> None:
        self.volume = volume
        self._lock = threading.Lock()
        self._target: Path | None = None
        self._playing: Path | None = None
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._playback_worker, daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=8192)
        self._worker.start()
        self._started = True

    def play(self, path: Path) -> None:
        with self._lock:
            self._target = path.resolve()

    def stop(self) -> None:
        with self._lock:
            self._target = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._target is not None

    @property
    def current_file(self) -> Path | None:
        with self._lock:
            return self._playing

    def _playback_worker(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                target = self._target

            if target is None:
                if self._playing is not None:
                    pygame.mixer.music.stop()
                    self._playing = None
                time.sleep(0.1)
                continue

            if target != self._playing:
                try:
                    pygame.mixer.music.load(str(target))
                    pygame.mixer.music.set_volume(self.volume)
                    pygame.mixer.music.play(loops=-1)
                    self._playing = target
                except pygame.error as exc:
                    print(f"재생 오류 ({target.name}): {exc}")
                    self._playing = None
                    with self._lock:
                        if self._target == target:
                            self._target = None
                    time.sleep(1.0)
                    continue

            if not pygame.mixer.music.get_busy():
                with self._lock:
                    if self._target == target:
                        pygame.mixer.music.play(loops=-1)

            time.sleep(0.1)

    def close(self) -> None:
        self._stop_event.set()
        self.stop()
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)
        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        self._started = False