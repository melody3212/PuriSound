"""SeamlessLooper 기반 마스킹 MP3 연속 재생."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from seamless_audio import CHANNELS_PER_TRACK, SeamlessLooper, ensure_mixer_channels

GAIN_DB = float(os.environ.get("GAIN_DB", "12"))
MASTER_VOLUME = float(os.environ.get("MASTER_VOLUME", "1.0"))
ALSA_PCM_PERCENT = os.environ.get("ALSA_PCM_PERCENT", "100")
MAX_TRACKS = 3


@dataclass
class _TrackState:
    path: Path
    volume: float
    name: str = ""


class MaskingPlayer:
    def __init__(self, *, audio_dev: str | None = None) -> None:
        self._audio_dev = audio_dev or os.environ.get(
            "AUDIODEV", "plughw:CARD=Headphones,DEV=0"
        )
        self._lock = threading.Lock()
        self._tracks: list[_TrackState] = []
        self._loopers: list[SeamlessLooper] = []
        self._active = False
        self._pygame: Any = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            if not self._loopers:
                return False
            return any(looper.is_playing() for looper in self._loopers)

    @property
    def current_track_name(self) -> str | None:
        with self._lock:
            if not self._tracks:
                return None
            if len(self._tracks) == 1:
                return self._tracks[0].name or None
            return ", ".join(track.name for track in self._tracks if track.name)

    @staticmethod
    def _effective_volume(volume: float) -> float:
        gain = 10 ** (GAIN_DB / 20.0) if GAIN_DB else 1.0
        return max(0.0, min(1.0, float(volume) * MASTER_VOLUME * gain))

    @staticmethod
    def _alsa_card_from_device(audio_dev: str) -> str:
        match = re.search(r"CARD=([^,]+)", audio_dev)
        if match:
            return match.group(1)
        return "Headphones"

    def _boost_alsa_volume(self) -> None:
        if not ALSA_PCM_PERCENT:
            return
        card = self._alsa_card_from_device(self._audio_dev)
        try:
            subprocess.run(
                ["amixer", "-c", card, "set", "PCM", f"{ALSA_PCM_PERCENT}%"],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _ensure_mixer(self) -> Any:
        if self._pygame is not None and self._pygame.mixer.get_init():
            return self._pygame

        os.environ["SDL_AUDIODRIVER"] = "alsa"
        os.environ["AUDIODEV"] = self._audio_dev
        self._boost_alsa_volume()

        import pygame

        ensure_mixer_channels(pygame, MAX_TRACKS)
        self._pygame = pygame
        return pygame

    def play_tracks(self, track_specs: list[dict[str, Any]]) -> None:
        if not track_specs:
            self.stop()
            return

        specs = track_specs[:MAX_TRACKS]
        pygame = self._ensure_mixer()
        ensure_mixer_channels(pygame, len(specs))

        with self._lock:
            for looper in self._loopers:
                looper.stop()
            self._loopers = []
            self._tracks = []

            for index, spec in enumerate(specs):
                path = Path(str(spec["path"]))
                if not path.is_file():
                    raise FileNotFoundError(f"MP3를 찾을 수 없습니다: {path}")

                volume = float(spec.get("volume", 1.0))
                sound = pygame.mixer.Sound(str(path))
                base_channel = index * CHANNELS_PER_TRACK
                looper = SeamlessLooper(
                    sound,
                    (
                        pygame.mixer.Channel(base_channel),
                        pygame.mixer.Channel(base_channel + 1),
                    ),
                    self._effective_volume(volume),
                )
                looper.start()
                self._loopers.append(looper)
                self._tracks.append(
                    _TrackState(path=path, volume=volume, name=path.name)
                )

            self._active = bool(self._loopers)

    def play_single(self, path: Path, *, volume: float = 1.0) -> None:
        self.play_tracks([{"path": str(path), "volume": volume}])

    def update_volume(self, volume: float) -> bool:
        with self._lock:
            if not self._tracks:
                return False
            clamped = max(0.0, min(1.0, float(volume)))
            effective = self._effective_volume(clamped)
            for track, looper in zip(self._tracks, self._loopers, strict=True):
                track.volume = clamped
                looper.set_volume(effective)
            return True

    def update_track_volumes(self, volumes: list[float]) -> bool:
        with self._lock:
            if not self._tracks or len(volumes) != len(self._tracks):
                return False
            for track, looper, volume in zip(
                self._tracks, self._loopers, volumes, strict=True
            ):
                clamped = max(0.0, min(1.0, float(volume)))
                track.volume = clamped
                looper.set_volume(self._effective_volume(clamped))
            return True

    def stop(self) -> None:
        with self._lock:
            for looper in self._loopers:
                looper.stop()
            self._loopers = []
            self._tracks = []
            self._active = False

    def close(self) -> None:
        self.stop()
        if self._pygame is not None and self._pygame.mixer.get_init():
            self._pygame.mixer.quit()
        self._pygame = None