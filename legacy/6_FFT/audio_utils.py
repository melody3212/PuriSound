"""공통 오디오 분석/장치 확인 유틸리티."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pyaudio

try:
    from scipy.io import wavfile
except ImportError:
    wavfile = None

try:
    from pydub import AudioSegment
except ImportError:
    AudioSegment = None

try:
    import pygame
except ImportError:
    pygame = None


AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac"}
_PROJECT_ROOT = Path(__file__).resolve().parent


def default_masking_folder() -> Path:
    return _PROJECT_ROOT / "masking_sounds"


def default_cache_path() -> Path:
    return _PROJECT_ROOT / "masking_fft_cache.json"


def list_masking_files(folder: Path) -> list[Path]:
    return sorted(
        f for f in folder.iterdir() if f.suffix.lower() in AUDIO_EXTENSIONS
    )


def save_profiles_cache(profiles: dict[Path, dict], cache_path: Path) -> None:
    cache_data = {
        "version": 1,
        "profiles": [
            {"file": path.name, "low": p["low"], "mid": p["mid"], "high": p["high"]}
            for path, p in sorted(profiles.items(), key=lambda x: x[0].name)
        ],
    }
    cache_path.write_text(
        json.dumps(cache_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_audio_mono(path: Path, max_samples: int = 4096):
    suffix = path.suffix.lower()

    if suffix == ".wav" and wavfile is not None:
        fs, data = wavfile.read(path)
        if data.ndim > 1:
            data = np.mean(data, axis=1)
        data = data.astype(np.float32)
        peak = float(np.max(np.abs(data)) or 1.0)
        if peak > 1.0:
            data /= peak
        return fs, data[:max_samples]

    if AudioSegment is None:
        raise RuntimeError(f"mp3 분석에는 pydub + ffmpeg 필요: {path.name}")

    segment = AudioSegment.from_file(path)
    segment = segment.set_channels(1)
    samples = np.array(segment.get_array_of_samples(), dtype=np.float32)
    peak = float(1 << (8 * segment.sample_width - 1))
    samples /= peak
    return segment.frame_rate, samples[:max_samples]


def compute_fft_profile(data: np.ndarray, fs: int) -> dict[str, float]:
    n = min(len(data), 2048)
    if n < 64:
        return {"low": 0.33, "mid": 0.34, "high": 0.33}

    chunk = data[:n].astype(np.float32)
    yf = np.fft.rfft(chunk)
    freqs = np.fft.rfftfreq(n, 1 / fs)
    mag = np.abs(yf)

    low = float(np.sum(mag[(freqs >= 20) & (freqs < 300)]))
    mid = float(np.sum(mag[(freqs >= 300) & (freqs < 3500)]))
    high = float(np.sum(mag[(freqs >= 3500) & (freqs < 11000)]))
    total = low + mid + high + 1e-8

    return {
        "low": round(low / total, 3),
        "mid": round(mid / total, 3),
        "high": round(high / total, 3),
    }


def analyze_files(files: list[Path], show_progress: bool = False) -> dict[Path, dict]:
    profiles: dict[Path, dict] = {}
    for path in files:
        if show_progress:
            print(f"  분석 중: {path.name}")
        try:
            fs, data = load_audio_mono(path)
            profiles[path] = compute_fft_profile(data, fs)
        except Exception as exc:
            if show_progress:
                print(f"    건너뜀: {exc}")
    return profiles


def _profiles_from_cache(
    cache: dict,
    files: list[Path],
) -> dict[Path, dict] | None:
    by_name = {f.name: f for f in files}
    expected_names = [f.name for f in files]
    cached_items = cache.get("profiles", [])
    cached_names = [item["file"] for item in cached_items]

    if cached_names != expected_names:
        return None

    profiles: dict[Path, dict] = {}
    for item in cached_items:
        path = by_name.get(item["file"])
        if path is None:
            return None
        profiles[path] = {
            "low": item["low"],
            "mid": item["mid"],
            "high": item["high"],
        }
    return profiles


def load_or_build_profiles(
    folder: Path,
    cache_path: Path,
    *,
    force_rebuild: bool = False,
) -> dict[Path, dict]:
    files = list_masking_files(folder)
    if not files:
        raise RuntimeError(f"{folder} 에 오디오 파일이 없습니다.")

    if not force_rebuild and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            profiles = _profiles_from_cache(cache, files)
            if profiles:
                print(f"FFT 캐시 로드: {cache_path} ({len(profiles)}개)")
                return profiles
            print("캐시가 현재 마스킹 파일 목록과 달라 다시 분석합니다.")
        except (json.JSONDecodeError, KeyError, TypeError):
            print("캐시 파일이 손상되어 다시 분석합니다.")

    print("마스킹 파일 FFT 분석 중... (최초 1회, 이후 캐시 사용)")
    profiles = analyze_files(files, show_progress=True)
    if not profiles:
        raise RuntimeError("분석 가능한 마스킹 파일이 없습니다.")

    save_profiles_cache(profiles, cache_path)
    print(f"FFT 캐시 저장: {cache_path}")
    return profiles


def samples_to_db(samples: np.ndarray, db_offset: float = 100.0) -> float:
    """int16/float 샘플을 dB(추정 SPL)로 변환. offset으로 임계값 보정 가능."""
    if samples.dtype != np.float32:
        data = samples.astype(np.float32) / 32768.0
    else:
        data = samples

    rms = float(np.sqrt(np.mean(data * data)))
    if rms < 1e-10:
        return 0.0
    return 20.0 * np.log10(rms) + db_offset


def mic_chunk_to_profile(mic_data: bytes, rate: int) -> dict[str, float]:
    data = np.frombuffer(mic_data, dtype=np.int16).astype(np.float32) / 32768.0
    return compute_fft_profile(data, rate)


def select_best_masking(
    mic_profile: dict[str, float],
    profiles: dict[Path, dict],
) -> Path:
    best_score = -1.0
    best_path = next(iter(profiles))
    for path, profile in profiles.items():
        score = (
            mic_profile["low"] * profile["low"]
            + mic_profile["mid"] * profile["mid"]
            + mic_profile["high"] * profile["high"]
        )
        if score > best_score:
            best_score = score
            best_path = path
    return best_path


class DurationTimer:
    """조건이 threshold_sec 이상 누적되면 True. tolerance_sec 이하의 반대 구간은 허용."""

    def __init__(self, threshold_sec: float = 5.0, tolerance_sec: float = 2.0):
        self.threshold_sec = threshold_sec
        self.tolerance_sec = tolerance_sec
        self.accumulated = 0.0
        self.break_time = 0.0

    def reset(self):
        self.accumulated = 0.0
        self.break_time = 0.0

    def update(self, condition: bool, dt: float) -> bool:
        if condition:
            self.accumulated += dt
            self.break_time = 0.0
        else:
            self.break_time += dt
            if self.break_time > self.tolerance_sec:
                self.accumulated = 0.0
        return self.accumulated >= self.threshold_sec


def list_audio_devices(pa: pyaudio.PyAudio):
    print("\n=== 오디오 장치 목록 ===")
    default_in = pa.get_default_input_device_info()
    default_out = pa.get_default_output_device_info()
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        tags = []
        if i == default_in["index"]:
            tags.append("기본 입력")
        if i == default_out["index"]:
            tags.append("기본 출력")
        tag = f" ({', '.join(tags)})" if tags else ""
        print(
            f"  [{i}] {info['name']}{tag} | "
            f"in={int(info['maxInputChannels'])} out={int(info['maxOutputChannels'])}"
        )
    print(
        f"\n기본 마이크: [{default_in['index']}] {default_in['name']}\n"
        f"기본 스피커: [{default_out['index']}] {default_out['name']}"
    )


def check_microphone(pa: pyaudio.PyAudio, rate: int, chunk: int) -> bool:
    print("\n=== 마이크 확인 ===")
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=rate,
            input=True,
            frames_per_buffer=chunk,
        )
    except Exception as exc:
        print(f"마이크 열기 실패: {exc}")
        return False

    try:
        print("  1초간 입력 테스트 중... (소리를 내 보세요)")
        frames = []
        start = time.time()
        while time.time() - start < 1.0:
            frames.append(stream.read(chunk, exception_on_overflow=False))

        data = np.frombuffer(b"".join(frames), dtype=np.int16)
        rms = float(np.sqrt(np.mean((data.astype(np.float32) / 32768.0) ** 2)))
        peak = int(np.max(np.abs(data))) if len(data) else 0
        print(f"  RMS={rms:.5f} | peak={peak}")

        if peak < 50:
            print("  경고: 입력 신호가 거의 없습니다. 마이크 연결/볼륨을 확인하세요.")
            return False

        print("  마이크 OK")
        return True
    finally:
        stream.stop_stream()
        stream.close()


def check_speaker(test_file: Path | None = None, volume: float = 0.5) -> bool:
    print("\n=== 스피커 확인 ===")
    if pygame is None:
        print("pygame 미설치")
        return False

    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)

        if test_file and test_file.exists():
            pygame.mixer.music.load(str(test_file))
            pygame.mixer.music.set_volume(volume)
            pygame.mixer.music.play()
            print(f"  테스트 재생: {test_file.name} (약 2초)")
            time.sleep(2.0)
            pygame.mixer.music.stop()
        else:
            print("  pygame mixer 초기화 OK (테스트 파일 없음)")

        print("  스피커 OK")
        return True
    except Exception as exc:
        print(f"  스피커 테스트 실패: {exc}")
        return False


def print_live_status(
    db: float,
    state: str,
    loud_acc: float,
    quiet_acc: float,
    playing_name: str | None,
):
    play_text = playing_name or "-"
    line = (
        f"\r dB: {db:5.1f} | 상태: {state:<8} | "
        f"소음누적: {loud_acc:4.1f}s | 정숙누적: {quiet_acc:4.1f}s | 재생: {play_text[:30]}"
    )
    sys.stdout.write(line.ljust(100))
    sys.stdout.flush()