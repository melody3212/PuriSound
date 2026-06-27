#!/usr/bin/env python3
"""masking_sounds 폴더의 오디오를 FFT 분석해 음역대별 에너지 비율을 JSON으로 저장합니다."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

try:
    from scipy.io import wavfile
except ImportError:
    wavfile = None

try:
    from pydub import AudioSegment
except ImportError:
    AudioSegment = None


PROJECT_ROOT = Path(__file__).resolve().parent
MASKING_FOLDER = PROJECT_ROOT / "masking_sounds"
OUTPUT_JSON = PROJECT_ROOT / "masking_fft_profiles.json"

AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac"}

# 음역대 정의 (Hz)
FREQUENCY_BANDS = {
    "sub_bass": {"label": "초저음", "range_hz": [20, 60]},
    "bass": {"label": "저음", "range_hz": [60, 300]},
    "low_mid": {"label": "중저음", "range_hz": [300, 1000]},
    "mid": {"label": "중음", "range_hz": [1000, 3500]},
    "high_mid": {"label": "중고음", "range_hz": [3500, 8000]},
    "high": {"label": "고음", "range_hz": [8000, 11000]},
}

# 마스킹 매칭용 3대역 (기존 시스템 호환)
MATCH_BANDS = {
    "low": {"label": "저음", "range_hz": [20, 300]},
    "mid": {"label": "중음", "range_hz": [300, 3500]},
    "high": {"label": "고음", "range_hz": [3500, 11000]},
}

FILL_THRESHOLD = 0.15


def _pick_loudest_window(samples: np.ndarray, max_samples: int) -> np.ndarray:
    """앞부분 무음이 있는 MP3도 대표 구간을 잡기 위해 RMS가 가장 큰 구간을 선택합니다."""
    if samples.size <= max_samples:
        return samples

    best_start = 0
    best_rms = -1.0
    step = max(1, max_samples // 4)
    last_start = max(0, samples.size - max_samples)
    for start in range(0, last_start + 1, step):
        chunk = samples[start : start + max_samples]
        rms = float(np.sqrt(np.mean(chunk**2)))
        if rms > best_rms:
            best_rms = rms
            best_start = start
    return samples[best_start : best_start + max_samples]


def load_audio_mono(path: Path, max_samples: int = 8192) -> tuple[int, np.ndarray]:
    suffix = path.suffix.lower()

    if suffix == ".wav" and wavfile is not None:
        fs, data = wavfile.read(path)
        if data.ndim > 1:
            data = np.mean(data, axis=1)
        data = data.astype(np.float32)
        peak = float(np.max(np.abs(data)) or 1.0)
        if peak > 1.0:
            data /= peak
        return int(fs), _pick_loudest_window(data, max_samples)

    if AudioSegment is None:
        raise RuntimeError(f"mp3 분석에는 pydub + ffmpeg 필요: {path.name}")

    segment = AudioSegment.from_file(path)
    segment = segment.set_channels(1)
    scan_ms = min(len(segment), 60_000)
    scan_segment = segment[:scan_ms]
    samples = np.array(scan_segment.get_array_of_samples(), dtype=np.float32)
    peak = float(1 << (8 * scan_segment.sample_width - 1))
    samples /= peak
    return scan_segment.frame_rate, _pick_loudest_window(samples, max_samples)


def band_energy(
    data: np.ndarray,
    fs: int,
    bands: dict[str, dict],
) -> dict[str, float]:
    n = min(len(data), 4096)
    if n < 64:
        uniform = round(1.0 / len(bands), 4)
        return {name: uniform for name in bands}

    chunk = data[:n].astype(np.float32)
    yf = np.fft.rfft(chunk)
    freqs = np.fft.rfftfreq(n, 1 / fs)
    mag = np.abs(yf)

    energies: dict[str, float] = {}
    for name, spec in bands.items():
        lo, hi = spec["range_hz"]
        energies[name] = float(np.sum(mag[(freqs >= lo) & (freqs < hi)]))

    total = sum(energies.values()) + 1e-8
    return {name: round(value / total, 4) for name, value in energies.items()}


def dominant_band(ratios: dict[str, float], band_defs: dict[str, dict]) -> str:
    name = max(ratios, key=ratios.get)
    return band_defs[name]["label"]


def filled_bands(ratios: dict[str, float], band_defs: dict[str, dict]) -> list[str]:
    ranked = sorted(ratios.items(), key=lambda x: x[1], reverse=True)
    result = [band_defs[name]["label"] for name, ratio in ranked if ratio >= FILL_THRESHOLD]
    return result or [band_defs[ranked[0][0]]["label"]]


def dominant_frequency_hz(profile_bands: dict[str, dict]) -> int:
    weighted = 0.0
    total = 0.0
    for band in profile_bands.values():
        lo, hi = band["range_hz"]
        center = (float(lo) + float(hi)) / 2.0
        ratio = float(band["ratio"])
        weighted += center * ratio
        total += ratio
    if total <= 0.0:
        return 0
    return int(round(weighted / total))


def match_band_to_color(match_label: str) -> str:
    mapping = {"저음": "브라운", "중음": "핑크", "고음": "화이트"}
    return mapping.get(match_label, "핑크")


def analyze_file(path: Path) -> dict:
    fs, data = load_audio_mono(path)
    detailed = band_energy(data, fs, FREQUENCY_BANDS)
    match = band_energy(data, fs, MATCH_BANDS)
    bands_for_centroid = {
        name: {
            "label": spec["label"],
            "range_hz": spec["range_hz"],
            "ratio": detailed[name],
        }
        for name, spec in FREQUENCY_BANDS.items()
    }
    dominant_hz = dominant_frequency_hz(bands_for_centroid)

    return {
        "file": path.name,
        "sample_rate_hz": fs,
        "dominant_frequency_hz": dominant_hz,
        "noise_color": match_band_to_color(dominant_band(match, MATCH_BANDS)),
        "bands": {
            name: {
                "label": spec["label"],
                "range_hz": spec["range_hz"],
                "ratio": detailed[name],
                "percent": round(detailed[name] * 100, 1),
            }
            for name, spec in FREQUENCY_BANDS.items()
        },
        "match_bands": {
            name: {
                "label": spec["label"],
                "range_hz": spec["range_hz"],
                "ratio": match[name],
                "percent": round(match[name] * 100, 1),
            }
            for name, spec in MATCH_BANDS.items()
        },
        "dominant_band": dominant_band(detailed, FREQUENCY_BANDS),
        "dominant_match_band": dominant_band(match, MATCH_BANDS),
        "filled_bands": filled_bands(detailed, FREQUENCY_BANDS),
        "filled_match_bands": filled_bands(match, MATCH_BANDS),
    }


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else MASKING_FOLDER
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else OUTPUT_JSON

    files = sorted(
        f for f in folder.iterdir() if f.suffix.lower() in AUDIO_EXTENSIONS
    )
    if not files:
        print(f"{folder} 에 오디오 파일이 없습니다.")
        sys.exit(1)

    print(f"분석 대상: {len(files)}개 ({folder})")

    profiles = []
    for path in files:
        print(f"  분석 중: {path.name}")
        try:
            profiles.append(analyze_file(path))
        except Exception as exc:
            print(f"    건너뜀: {exc}")

    if not profiles:
        print("분석 가능한 파일이 없습니다.")
        sys.exit(1)

    result = {
        "version": 1,
        "source_folder": str(folder),
        "band_definitions": {
            name: {"label": spec["label"], "range_hz": spec["range_hz"]}
            for name, spec in FREQUENCY_BANDS.items()
        },
        "match_band_definitions": {
            name: {"label": spec["label"], "range_hz": spec["range_hz"]}
            for name, spec in MATCH_BANDS.items()
        },
        "fill_threshold": FILL_THRESHOLD,
        "profiles": profiles,
    }

    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"저장 완료: {output} ({len(profiles)}개)")


if __name__ == "__main__":
    main()