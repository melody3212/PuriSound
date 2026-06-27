#!/usr/bin/env python3
"""
Pi 3B+ 소음 감지 기반 마스킹 컨트롤러.

1) mp3 FFT 사전 분석 (캐시)
2) 마이크/스피커 연결 확인
3) 실시간 dB 표시
4) 40dB 이상 5초(2초 정적 허용) → 상쇄 mp3 재생
5) 재생 중 40dB 이하 5초(2초 정적 허용) → 재생 정지
"""

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pyaudio

from audio_utils import (
    DurationTimer,
    check_microphone,
    check_speaker,
    default_cache_path,
    default_masking_folder,
    list_audio_devices,
    load_or_build_profiles,
    mic_chunk_to_profile,
    print_live_status,
    samples_to_db,
    select_best_masking,
)

try:
    import pygame
except ImportError:
    pygame = None


class NoiseMaskingController:
    def __init__(
        self,
        masking_folder: str = "masking_sounds",
        cache_path: str = "masking_fft_cache.json",
        chunk: int = 1024,
        rate: int = 22050,
        db_threshold: float = 40.0,
        db_offset: float = 100.0,
        trigger_sec: float = 5.0,
        stop_sec: float = 5.0,
        tolerance_sec: float = 2.0,
        volume: float = 0.7,
        skip_device_check: bool = False,
        force_rebuild_cache: bool = False,
    ):
        self.masking_folder = Path(masking_folder)
        self.cache_path = Path(cache_path)
        self.chunk = chunk
        self.rate = rate
        self.db_threshold = db_threshold
        self.db_offset = db_offset
        self.volume = volume
        self.skip_device_check = skip_device_check

        self.loud_timer = DurationTimer(trigger_sec, tolerance_sec)
        self.quiet_timer = DurationTimer(stop_sec, tolerance_sec)

        if pygame is None:
            print("pygame 필요: pip install pygame")
            sys.exit(1)

        print("=== 1단계: 마스킹 FFT 프로필 준비 ===")
        self.profiles = load_or_build_profiles(
            self.masking_folder,
            self.cache_path,
            force_rebuild=force_rebuild_cache,
        )
        self.test_file = next(iter(sorted(self.profiles)))

        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        self._playback_lock = threading.Lock()
        self._target_path: Path | None = None
        self._playing_path: Path | None = None
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._playback_worker, daemon=True)
        self._worker.start()

        self.p = pyaudio.PyAudio()
        self.is_playing = False

    def _playback_worker(self):
        while not self._stop_event.is_set():
            with self._playback_lock:
                target = self._target_path

            if target is None:
                if self._playing_path is not None:
                    pygame.mixer.music.stop()
                    self._playing_path = None
                time.sleep(0.1)
                continue

            if target != self._playing_path:
                try:
                    pygame.mixer.music.load(str(target))
                    pygame.mixer.music.set_volume(self.volume)
                    pygame.mixer.music.play(loops=-1)
                    self._playing_path = target
                except Exception as exc:
                    print(f"\n재생 오류 ({target.name}): {exc}")
                    self._playing_path = None
                    with self._playback_lock:
                        self._target_path = None
                    time.sleep(1.0)
                    continue

            if not pygame.mixer.music.get_busy():
                with self._playback_lock:
                    if self._target_path == target:
                        pygame.mixer.music.play(loops=-1)

            time.sleep(0.1)

    def _start_masking(self, path: Path):
        with self._playback_lock:
            self._target_path = path
        self.is_playing = True
        self.quiet_timer.reset()
        print(f"\n▶ 마스킹 시작: {path.name}")

    def _stop_masking(self):
        with self._playback_lock:
            self._target_path = None
        self.is_playing = False
        self.loud_timer.reset()
        print("\n■ 마스킹 정지 (정숙 상태 5초 유지)")

    def _run_device_checks(self) -> bool:
        print("\n=== 2단계: 장치 확인 ===")
        list_audio_devices(self.p)

        if self.skip_device_check:
            print("\n장치 확인 건너뜀 (--skip-check)")
            return True

        mic_ok = check_microphone(self.p, self.rate, self.chunk)
        spk_ok = check_speaker(self.test_file, volume=self.volume)
        if not mic_ok or not spk_ok:
            print("\n장치 확인 실패. 연결 후 다시 실행하세요.")
            return False
        print("\n장치 확인 완료")
        return True

    def run(self):
        if not self._run_device_checks():
            self._cleanup()
            sys.exit(1)

        stream = self.p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk,
        )

        print("\n=== 3단계: 실시간 소음 모니터링 ===")
        print(
            f"임계값: {self.db_threshold:.1f} dB | "
            f"트리거/정지: {self.loud_timer.threshold_sec:.0f}s | "
            f"정적허용: {self.loud_timer.tolerance_sec:.0f}s"
        )
        print("Ctrl+C 로 종료\n")

        chunk_duration = self.chunk / self.rate
        last_profile = {"low": 0.33, "mid": 0.34, "high": 0.33}

        try:
            while True:
                mic_data = stream.read(self.chunk, exception_on_overflow=False)
                samples = np.frombuffer(mic_data, dtype=np.int16)
                db = samples_to_db(samples, self.db_offset)
                last_profile = mic_chunk_to_profile(mic_data, self.rate)

                is_loud = db >= self.db_threshold
                is_quiet = db < self.db_threshold

                if not self.is_playing:
                    if self.loud_timer.update(is_loud, chunk_duration):
                        best = select_best_masking(last_profile, self.profiles)
                        self._start_masking(best)
                    state = "대기"
                    quiet_acc = self.quiet_timer.accumulated
                    loud_acc = self.loud_timer.accumulated
                else:
                    if self.quiet_timer.update(is_quiet, chunk_duration):
                        self._stop_masking()
                    state = "재생중"
                    loud_acc = self.loud_timer.accumulated
                    quiet_acc = self.quiet_timer.accumulated

                playing_name = None
                with self._playback_lock:
                    if self._target_path:
                        playing_name = self._target_path.name

                print_live_status(db, state, loud_acc, quiet_acc, playing_name)

        except KeyboardInterrupt:
            print("\n\n종료합니다.")
        finally:
            stream.stop_stream()
            stream.close()
            self._cleanup()

    def _cleanup(self):
        self._stop_event.set()
        self.p.terminate()
        if pygame.mixer.get_init():
            pygame.mixer.music.stop()
            pygame.mixer.quit()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Pi 3B+ 소음 감지 마스킹 컨트롤러")
    parser.add_argument("--db-threshold", type=float, default=40.0, help="소음 임계 dB")
    parser.add_argument("--db-offset", type=float, default=100.0, help="dB 보정값 (마이크 캘리브레이션)")
    parser.add_argument("--trigger-sec", type=float, default=5.0, help="재생 시작 누적 시간")
    parser.add_argument("--stop-sec", type=float, default=5.0, help="재생 정지 누적 시간")
    parser.add_argument("--tolerance-sec", type=float, default=2.0, help="정적 허용 시간")
    parser.add_argument("--volume", type=float, default=0.7, help="재생 볼륨")
    parser.add_argument("--skip-check", action="store_true", help="장치 확인 건너뛰기")
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="FFT 캐시를 무시하고 마스킹 파일을 다시 분석",
    )
    args = parser.parse_args()

    controller = NoiseMaskingController(
        masking_folder=str(default_masking_folder()),
        cache_path=str(default_cache_path()),
        db_threshold=args.db_threshold,
        db_offset=args.db_offset,
        trigger_sec=args.trigger_sec,
        stop_sec=args.stop_sec,
        tolerance_sec=args.tolerance_sec,
        volume=args.volume,
        skip_device_check=args.skip_check,
        force_rebuild_cache=args.rebuild_cache,
    )
    controller.run()


if __name__ == "__main__":
    main()