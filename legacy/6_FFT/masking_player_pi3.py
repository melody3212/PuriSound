#!/usr/bin/env python3
"""Pi 3B+ optimized real-time noise masking player."""

import sys
import time
import threading
import argparse
from pathlib import Path

import numpy as np
import pyaudio

from audio_utils import (
    default_cache_path,
    default_masking_folder,
    load_or_build_profiles,
    mic_chunk_to_profile,
    select_best_masking,
)

try:
    import pygame
except ImportError:
    pygame = None


class Pi3MaskingPlayer:
    def __init__(
        self,
        masking_folder="masking_sounds",
        cache_path="masking_fft_cache.json",
        chunk=1024,
        rate=22050,
        switch_interval=4.0,
        volume=0.7,
        force_rebuild_cache: bool = False,
    ):
        self.rate = rate
        self.chunk = chunk
        self.switch_interval = switch_interval
        self.volume = volume
        self.masking_folder = Path(masking_folder)
        self.cache_path = Path(cache_path)

        self.masking_profiles = load_or_build_profiles(
            self.masking_folder,
            self.cache_path,
            force_rebuild=force_rebuild_cache,
        )
        self.masking_files = sorted(self.masking_profiles)

        if pygame is None:
            print("pygame이 필요합니다: pip install pygame")
            sys.exit(1)

        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        self._playback_lock = threading.Lock()
        self._current_wav = None
        self._playing_wav = None
        self._stop_playback = threading.Event()
        self._playback_thread = threading.Thread(
            target=self._playback_worker, daemon=True
        )
        self._playback_thread.start()

        self.p = pyaudio.PyAudio()

    def _playback_worker(self):
        while not self._stop_playback.is_set():
            with self._playback_lock:
                target = self._current_wav

            if target is None:
                time.sleep(0.1)
                continue

            if target != self._playing_wav:
                try:
                    pygame.mixer.music.load(str(target))
                    pygame.mixer.music.set_volume(self.volume)
                    pygame.mixer.music.play(loops=-1)
                    self._playing_wav = target
                except Exception as exc:
                    print(f"재생 오류 ({target.name}): {exc}")
                    self._playing_wav = None
                    time.sleep(1.0)
                    continue

            if not pygame.mixer.music.get_busy():
                with self._playback_lock:
                    if self._current_wav == target:
                        pygame.mixer.music.play(loops=-1)

            time.sleep(0.1)

    def _switch_masking(self, path: Path):
        with self._playback_lock:
            if self._current_wav == path:
                return
            self._current_wav = path

    def start(self):
        stream_in = self.p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk,
        )

        print("Pi 3B+ 마스킹 시스템 시작... (Ctrl+C로 종료)")
        print(f"  샘플레이트: {self.rate}Hz | chunk: {self.chunk} | 전환 주기: {self.switch_interval}s")

        last_switch = 0.0
        current_path = None

        try:
            while True:
                mic_data = stream_in.read(self.chunk, exception_on_overflow=False)
                profile = mic_chunk_to_profile(mic_data, self.rate)
                now = time.time()

                if current_path is None or now - last_switch >= self.switch_interval:
                    best_path = select_best_masking(profile, self.masking_profiles)
                    if best_path != current_path:
                        print(
                            f"→ 마스킹 변경: {best_path.name} | "
                            f"L:{profile['low']:.2f} M:{profile['mid']:.2f} H:{profile['high']:.2f}"
                        )
                        self._switch_masking(best_path)
                        current_path = best_path
                    last_switch = now

                time.sleep(0.05)

        except KeyboardInterrupt:
            print("\n종료합니다.")
        finally:
            self._stop_playback.set()
            stream_in.stop_stream()
            stream_in.close()
            self.p.terminate()
            pygame.mixer.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pi 3B+ 실시간 마스킹 플레이어")
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="FFT 캐시를 무시하고 마스킹 파일을 다시 분석",
    )
    args = parser.parse_args()

    player = Pi3MaskingPlayer(
        masking_folder=str(default_masking_folder()),
        cache_path=str(default_cache_path()),
        force_rebuild_cache=args.rebuild_cache,
    )
    player.start()