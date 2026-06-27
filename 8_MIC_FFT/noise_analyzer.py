#!/usr/bin/env python3
"""마이크 입력을 FFT로 분석해 주변 노이즈 유형을 2초마다 터미널에 출력합니다."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pyaudio
from scipy.signal import welch

SAMPLE_RATE = 48_000
CHUNK = 2048
REPORT_INTERVAL = 2.0
ANALYSIS_SECONDS = 2.0

# 저음역·중음역·고음역 각 3단계, 총 9종
NOISE_CATEGORIES = (
    # id, region, sub_label, low_hz, high_hz, description
    ("L1", "저음역", "초저역", 20, 100, "50/60Hz 험음·건물/설비 진동"),
    ("L2", "저음역", "저역", 100, 300, "럼블·공조·에어컨 저주파"),
    ("L3", "저음역", "중저역", 300, 500, "두꺼운 저음·기계 체공진"),
    ("M1", "중음역", "중저중역", 500, 1200, "웅성한 중저음·저역 기계음"),
    ("M2", "중음역", "중역", 1200, 2500, "음성·대화·악기 중심 대역"),
    ("M3", "중음역", "중고역", 2500, 4000, "날카로운 중고음·치찰·마찰음"),
    ("H1", "고음역", "고역", 4000, 7000, "팬·환기·냉각 고역 잡음"),
    ("H2", "고음역", "초고역", 7000, 10_000, "전자식 히스·첨성 고음"),
    ("H3", "고음역", "극고역", 10_000, 16_000, "극초고역 날카로운 잡음"),
)

REGIONS = ("저음역", "중음역", "고음역")


@dataclass
class NoiseProfile:
    label: str
    detail: str
    confidence: float


def list_input_devices(pa: pyaudio.PyAudio) -> list[tuple[int, dict]]:
    devices: list[tuple[int, dict]] = []
    for index in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(index)
        if info.get("maxInputChannels", 0) > 0:
            devices.append((index, info))
    return devices


def analysis_bands(sample_rate: int) -> tuple[tuple[str, str, str, float, float, str], ...]:
    nyquist = sample_rate / 2
    bands: list[tuple[str, str, str, float, float, str]] = []
    for cat_id, region, sub_label, low, high, desc in NOISE_CATEGORIES:
        if low >= nyquist:
            continue
        bands.append((cat_id, region, sub_label, low, min(high, nyquist - 1), desc))
    return tuple(bands)


def resolve_sample_rate(pa: pyaudio.PyAudio, device_index: int, requested: int) -> int:
    candidates = [requested, int(pa.get_device_info_by_index(device_index)["defaultSampleRate"])]
    for rate in (48_000, 44_100, 32_000, 16_000, 8_000):
        if rate not in candidates:
            candidates.append(rate)

    for rate in candidates:
        try:
            if pa.is_format_supported(
                rate,
                input_device=device_index,
                input_channels=1,
                input_format=pyaudio.paInt16,
            ):
                return rate
        except ValueError:
            continue

    raise RuntimeError(f"장치 {device_index}에서 사용 가능한 샘플레이트를 찾지 못했습니다.")


def pick_default_input_device(pa: pyaudio.PyAudio, preferred: str | None) -> int:
    devices = list_input_devices(pa)
    if not devices:
        raise RuntimeError(
            "사용 가능한 마이크 입력 장치가 없습니다. "
            "USB 마이크 연결을 확인하거나, 다른 send_firebase 프로세스가 "
            "마이크를 점유 중이면 종료 후 다시 실행하세요. "
            "(확인: ps aux | grep send_firebase)"
        )

    if preferred is not None:
        needle = preferred.lower()
        for index, info in devices:
            name = str(info.get("name", "")).lower()
            if needle in name or needle == str(index):
                return index
        raise RuntimeError(f"요청한 장치를 찾을 수 없습니다: {preferred}")

    default = pa.get_default_input_device_info()
    return int(default["index"])


def band_energy(freqs: np.ndarray, spectrum: np.ndarray, low: float, high: float) -> float:
    mask = (freqs >= low) & (freqs < high)
    if not np.any(mask):
        return 0.0
    return float(np.sum(spectrum[mask]))


def category_ratios(
    freqs: np.ndarray,
    spectrum: np.ndarray,
    bands: tuple[tuple[str, str, str, float, float, str], ...],
) -> dict[str, float]:
    total = float(np.sum(spectrum))
    if total <= 1e-12:
        return {cat_id: 0.0 for cat_id, *_ in bands}
    return {
        cat_id: band_energy(freqs, spectrum, low, high) / total
        for cat_id, _, _, low, high, _ in bands
    }


def region_ratios(
    category_ratio: dict[str, float],
    bands: tuple[tuple[str, str, str, float, float, str], ...],
) -> dict[str, float]:
    totals = {region: 0.0 for region in REGIONS}
    for cat_id, region, *_ in bands:
        totals[region] += category_ratio.get(cat_id, 0.0)
    return totals


def spectral_centroid(freqs: np.ndarray, spectrum: np.ndarray) -> float:
    total = float(np.sum(spectrum))
    if total <= 1e-12:
        return 0.0
    return float(np.sum(freqs * spectrum) / total)


def spectral_flatness(spectrum: np.ndarray) -> float:
    spectrum = np.maximum(spectrum, 1e-12)
    geo = float(np.exp(np.mean(np.log(spectrum))))
    arith = float(np.mean(spectrum))
    return geo / arith if arith > 0 else 0.0


def harmonic_score(freqs: np.ndarray, spectrum: np.ndarray, fundamental: float) -> float:
    """50/60Hz 계열 험음 검출 점수."""
    if fundamental <= 0:
        return 0.0

    peaks: list[float] = []
    for harmonic in range(1, 9):
        target = fundamental * harmonic
        if target >= freqs[-1]:
            break
        idx = int(np.argmin(np.abs(freqs - target)))
        window = spectrum[max(0, idx - 1) : idx + 2]
        peaks.append(float(np.max(window)))

    if not peaks:
        return 0.0

    fundamental_strength = peaks[0]
    harmonic_sum = float(np.sum(peaks[1:]))
    baseline = float(np.median(spectrum)) + 1e-12
    ratio = (fundamental_strength + harmonic_sum) / baseline
    return min(1.0, ratio / 12.0)


def dominant_peak(freqs: np.ndarray, spectrum: np.ndarray) -> tuple[float, float]:
    if spectrum.size == 0:
        return 0.0, 0.0
    idx = int(np.argmax(spectrum))
    return float(freqs[idx]), float(spectrum[idx])


def pick_noise_category(
    cat_ratio: dict[str, float],
    bands: tuple[tuple[str, str, str, float, float, str], ...],
    hum_score: float,
    flatness: float,
) -> tuple[str, float]:
    """9개 세분 대역 중 우세 유형과 신뢰도를 반환."""
    scores = dict(cat_ratio)

    if hum_score > 0.4 and "L1" in scores:
        scores["L1"] += hum_score * 0.25

    if flatness > 0.3:
        for cat_id in ("H1", "H2", "H3"):
            if cat_id in scores:
                scores[cat_id] += flatness * 0.1

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_id, top_ratio = ordered[0]
    second_ratio = ordered[1][1] if len(ordered) > 1 else 0.0

    margin = top_ratio - second_ratio
    confidence = min(0.95, 0.45 + top_ratio * 0.8 + margin * 1.5)
    return top_id, confidence


def classify_noise(
    audio: np.ndarray,
    sample_rate: int,
) -> tuple[NoiseProfile, dict[str, float | str]]:
    audio = audio.astype(np.float64)
    audio = audio - np.mean(audio)

    rms = float(np.sqrt(np.mean(audio**2)))
    peak = float(np.max(np.abs(audio)))

    freqs, psd = welch(
        audio,
        fs=sample_rate,
        nperseg=min(4096, len(audio)),
        noverlap=min(2048, len(audio) // 2),
        window="hann",
    )
    spectrum = psd.copy()
    bands = analysis_bands(sample_rate)

    cat_ratio = category_ratios(freqs, spectrum, bands)
    region_ratio = region_ratios(cat_ratio, bands)
    centroid = spectral_centroid(freqs, spectrum)
    flatness = spectral_flatness(spectrum)
    dom_freq, _ = dominant_peak(freqs, spectrum)
    hum_50 = harmonic_score(freqs, spectrum, 50.0)
    hum_60 = harmonic_score(freqs, spectrum, 60.0)
    hum_score = max(hum_50, hum_60)

    metrics: dict[str, float | str] = {
        "rms_db": 20 * np.log10(rms + 1e-12),
        "peak": peak,
        "centroid_hz": centroid,
        "flatness": flatness,
        "dominant_hz": dom_freq,
        "hum_score": hum_score,
        **{f"cat_{k}": v for k, v in cat_ratio.items()},
        **{f"region_{k}": v for k, v in region_ratio.items()},
    }

    if rms < 0.003:
        return (
            NoiseProfile("조용함", "유의미한 소음이 거의 감지되지 않습니다.", 0.9),
            metrics,
        )

    top_id, confidence = pick_noise_category(cat_ratio, bands, hum_score, flatness)
    cat_map = {cat_id: (region, sub_label, low, high, desc) for cat_id, region, sub_label, low, high, desc in bands}
    region, sub_label, low, high, desc = cat_map[top_id]

    dominant_region = max(region_ratio, key=region_ratio.get)
    region_share = region_ratio[dominant_region]

    if region != dominant_region and region_share - region_ratio[region] > 0.15:
        detail = (
            f"주 에너지는 {dominant_region}({region_share * 100:.0f}%)이나 "
            f"세분 특성상 {sub_label}({low:.0f}-{high:.0f}Hz) 패턴이 가장 뚜렷합니다. {desc}"
        )
    else:
        detail = f"{sub_label} 대역({low:.0f}-{high:.0f}Hz) 우세. {desc}"

    label = f"{region} · {sub_label}"
    return NoiseProfile(label, detail, confidence), metrics


def format_report(
    profile: NoiseProfile,
    metrics: dict[str, float | str],
    device_name: str,
    elapsed: float,
    sample_rate: int,
) -> str:
    bands = analysis_bands(sample_rate)
    lines = [
        "",
        "═" * 58,
        f"  노이즈 분석  │  {time.strftime('%H:%M:%S')}  │  누적 {elapsed:.0f}s",
        "═" * 58,
        f"  장치     : {device_name}",
        f"  유형     : {profile.label}",
        f"  설명     : {profile.detail}",
        f"  신뢰도   : {profile.confidence * 100:.0f}%",
        "─" * 58,
        f"  음량 RMS : {metrics['rms_db']:.1f} dBFS",
        f"  중심주파수: {metrics['centroid_hz']:.0f} Hz",
        f"  지배톤   : {metrics['dominant_hz']:.0f} Hz",
        "─" * 58,
        "  음역별 에너지 비율",
    ]

    for region in REGIONS:
        ratio = float(metrics[f"region_{region}"])
        bar = "█" * int(ratio * 30)
        lines.append(f"  {region:<8} {ratio * 100:5.1f}% {bar}")

    lines.append("─" * 58)
    lines.append("  세분 대역 (9종)")

    current_region = ""
    for cat_id, region, sub_label, low, high, _ in bands:
        if region != current_region:
            current_region = region
            lines.append(f"  [{region}]")
        ratio = float(metrics[f"cat_{cat_id}"])
        bar = "█" * int(ratio * 30)
        label = f"    {sub_label} {low:.0f}-{high:.0f}Hz"
        lines.append(f"  {label:<26} {ratio * 100:5.1f}% {bar}")

    lines.append("═" * 58)
    return "\n".join(lines)


def record_loop(
    device: int | str | None,
    sample_rate: int,
    report_interval: float,
) -> None:
    pa = pyaudio.PyAudio()
    stream = None

    try:
        preferred = str(device) if device is not None else None
        index = pick_default_input_device(pa, preferred)
        info = pa.get_device_info_by_index(index)
        device_name = str(info.get("name", f"device-{index}"))
        sample_rate = resolve_sample_rate(pa, index, sample_rate)

        samples_needed = int(sample_rate * ANALYSIS_SECONDS)
        buffer: list[np.ndarray] = []
        buffered = 0

        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=index,
            frames_per_buffer=CHUNK,
        )

        print(f"마이크 녹음 시작: [{index}] {device_name}")
        print(f"샘플레이트 {sample_rate} Hz, {report_interval:.0f}초마다 FFT 분석 출력")
        print("종료: Ctrl+C\n")

        start = time.monotonic()
        last_report = start

        while True:
            raw = stream.read(CHUNK, exception_on_overflow=False)
            chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            buffer.append(chunk)
            buffered += chunk.size

            while buffered >= samples_needed:
                merged = np.concatenate(buffer)
                audio = merged[:samples_needed]
                remainder = merged[samples_needed:]
                buffer = [remainder] if remainder.size else []
                buffered = remainder.size

                profile, metrics = classify_noise(audio, sample_rate)
                elapsed = time.monotonic() - start
                print(
                    format_report(profile, metrics, device_name, elapsed, sample_rate),
                    flush=True,
                )
                last_report = time.monotonic()

            # 버퍼가 부족할 때도 간격을 맞추기 위해 짧게 대기
            if time.monotonic() - last_report < report_interval and buffered < samples_needed:
                time.sleep(0.01)

    except KeyboardInterrupt:
        print("\n분석을 종료합니다.")
    finally:
        if stream is not None:
            stream.stop_stream()
            stream.close()
        pa.terminate()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="마이크 FFT 실시간 분석으로 주변 노이즈 유형을 2초마다 출력합니다.",
    )
    parser.add_argument(
        "--device",
        "-d",
        help="입력 장치 이름 일부 또는 인덱스 (예: 'USB', 1)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="입력 장치 목록을 출력하고 종료",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=SAMPLE_RATE,
        help=f"샘플레이트 Hz (기본 {SAMPLE_RATE})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=REPORT_INTERVAL,
        help=f"분석 결과 출력 주기 초 (기본 {REPORT_INTERVAL})",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    pa = pyaudio.PyAudio()
    if args.list_devices:
        print("입력 장치 목록:")
        for index, info in list_input_devices(pa):
            default = " (default)" if index == pa.get_default_input_device_info()["index"] else ""
            print(f"  [{index}] {info['name']}{default}")
        pa.terminate()
        return 0
    pa.terminate()

    device: int | str | None = args.device
    if args.device is not None:
        try:
            device = int(args.device)
        except ValueError:
            device = args.device

    record_loop(
        device=device,
        sample_rate=args.rate,
        report_interval=args.interval,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())