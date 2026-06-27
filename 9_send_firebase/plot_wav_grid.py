#!/usr/bin/env python3
"""9_send_firebase와 동일한 파이프라인으로 WAV를 분석해 3x4 격자 JPG를 생성합니다."""

from __future__ import annotations

import argparse
import os
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pyaudio
import requests
from scipy.io import wavfile
from scipy.signal import butter, sosfiltfilt

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent / "8_MIC_FFT"))
from noise_analyzer import (  # noqa: E402
    CHUNK,
    NOISE_CATEGORIES,
    REGIONS,
    classify_noise,
    pick_default_input_device,
    resolve_sample_rate,
)

from send_firebase import (  # noqa: E402
    DEFAULT_YAMNET_URL,
    RECORD_SECONDS,
    YAMNET_GAIN_DB,
    build_noise_event,
    check_yamnet_server,
    classify_yamnet,
    prepare_analysis_audio,
    sanitize_yamnet_result,
    yamnet_primary_prediction,
)

DEFAULT_CAPTURE = _SCRIPT_DIR / "last_capture.wav"
DEFAULT_OUTPUT = _SCRIPT_DIR / "wav_analysis_grid.jpg"
_SEND_FIREBASE_PIDFILE = _SCRIPT_DIR / ".send_firebase.pid"


def send_firebase_pid() -> int | None:
    if not _SEND_FIREBASE_PIDFILE.exists():
        return None
    try:
        pid = int(_SEND_FIREBASE_PIDFILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None
# 파형만 y축을 데이터 피크에 맞춰 타이트하게 줌 (작은 신호도 극적으로 보임)
WAVEFORM_Y_PADDING = 1.04
WAVEFORM_MIN_PEAK = 1e-9


def load_mono_wav(path: Path, max_seconds: float | None = RECORD_SECONDS) -> tuple[int, np.ndarray]:
    sample_rate, data = wavfile.read(path)
    if data.ndim > 1:
        data = np.mean(data, axis=1)
    if np.issubdtype(data.dtype, np.integer):
        audio = data.astype(np.float32) / np.iinfo(data.dtype).max
    else:
        audio = data.astype(np.float32)
        peak = float(np.max(np.abs(audio)) or 1.0)
        if peak > 1.0:
            audio /= peak

    if max_seconds is not None:
        audio = audio[: int(sample_rate * max_seconds)]
    return int(sample_rate), audio


def save_mono_wav(path: Path, sample_rate: int, audio: np.ndarray) -> None:
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def record_audio(
    seconds: float = RECORD_SECONDS,
    sample_rate: int = 48_000,
    device: int | str | None = None,
    wind_filter: bool = True,
) -> tuple[int, np.ndarray, np.ndarray, str]:
    """send_firebase와 동일하게 4초 마이크 녹음 후 raw/analysis 오디오를 반환합니다."""
    pa = pyaudio.PyAudio()
    stream = None
    try:
        preferred = str(device) if device is not None else None
        index = pick_default_input_device(pa, preferred)
        info = pa.get_device_info_by_index(index)
        device_name = str(info.get("name", f"device-{index}"))
        sample_rate = resolve_sample_rate(pa, index, sample_rate)

        samples_needed = int(sample_rate * seconds)
        buffer: list[np.ndarray] = []
        buffered = 0
        frames_per_buffer = max(CHUNK, int(sample_rate * 0.02))

        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=index,
            frames_per_buffer=frames_per_buffer,
        )

        print(f"마이크 녹음 중: [{index}] {device_name} ({seconds:.0f}s)...", flush=True)
        start = time.monotonic()
        while buffered < samples_needed:
            raw = stream.read(CHUNK, exception_on_overflow=False)
            chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            buffer.append(chunk)
            buffered += chunk.size
            if time.monotonic() - start > seconds + 5:
                raise TimeoutError("마이크 녹음 시간 초과")

        raw_audio = np.concatenate(buffer)[-samples_needed:]
        analysis_audio = prepare_analysis_audio(raw_audio, sample_rate, wind_filter)
        return sample_rate, raw_audio, analysis_audio, device_name
    finally:
        if stream is not None:
            stream.stop_stream()
            stream.close()
        pa.terminate()


def analyze_like_send_firebase(
    analysis_audio: np.ndarray,
    sample_rate: int,
    yamnet_url: str | None = DEFAULT_YAMNET_URL,
) -> tuple[dict, object, dict]:
    profile, metrics = classify_noise(analysis_audio, sample_rate)

    yamnet = None
    if yamnet_url:
        online, _ = check_yamnet_server(yamnet_url)
        yamnet = (
            classify_yamnet(analysis_audio, sample_rate, yamnet_url)
            if online
            else {"error": "서버 연결 불가"}
        )
        yamnet = sanitize_yamnet_result(yamnet)

    event = build_noise_event(
        profile,
        metrics,
        device_id="snapshot",
        owner_id="snapshot",
        yamnet=yamnet,
        yamnet_url=yamnet_url,
    )
    return event, profile, metrics


def waveform_ylim(filtered: np.ndarray) -> float:
    """파형 서브플롯용 y축 반폭. 피크 기준 타이트 줌으로 작은 신호도 뚜렷하게 보이게 합니다."""
    peak = float(np.max(np.abs(filtered)))
    if peak < WAVEFORM_MIN_PEAK:
        return 1e-6
    return peak * WAVEFORM_Y_PADDING


def bandpass(audio: np.ndarray, sample_rate: int, low_hz: float, high_hz: float) -> np.ndarray:
    nyquist = sample_rate / 2
    low = max(low_hz, 1.0) / nyquist
    high = min(high_hz, nyquist - 1.0) / nyquist
    if low >= high:
        return np.zeros_like(audio)
    sos = butter(4, [low, high], btype="band", output="sos")
    return sosfiltfilt(sos, audio.astype(np.float64)).astype(np.float32)


def _yamnet_line(yamnet: dict | None, yamnet_url: str | None) -> str:
    if not yamnet_url:
        return "YAMNet: 사용 안 함"
    if yamnet is None:
        return "YAMNet: 꺼짐"
    if "error" in yamnet:
        return f"YAMNet: 꺼짐 ({yamnet['error']})"
    primary, score = yamnet_primary_prediction(yamnet)
    if not primary:
        return "YAMNet: 켜짐 — (유효 분류 없음)"
    line = f"YAMNet: {primary} ({score * 100:.0f}%)"
    input_peak = yamnet.get("_input_peak_dbfs")
    if input_peak is not None:
        line += f"  [입력 {input_peak:.0f} dBFS, +{YAMNET_GAIN_DB:.0f} dB 보정]"
    return line


def _top_bands(metrics: dict, limit: int = 3) -> str:
    bands: list[tuple[str, float]] = []
    for cat_id, _region, sub_label, low_hz, high_hz, _desc in NOISE_CATEGORIES:
        key = f"cat_{cat_id}"
        if key not in metrics:
            continue
        bands.append((f"{cat_id} {sub_label} ({low_hz:.0f}-{high_hz:.0f} Hz)", float(metrics[key])))
    bands.sort(key=lambda item: item[1], reverse=True)
    return "  |  ".join(f"{name} {ratio * 100:.1f}%" for name, ratio in bands[:limit])


def plot_wav_grid(
    audio: np.ndarray,
    sample_rate: int,
    output_path: Path,
    event: dict,
    profile: object,
    metrics: dict,
    yamnet: dict | None,
    yamnet_url: str | None,
    source_label: str,
) -> None:
    fft_data = event.get("fft") or {}
    region_text = ", ".join(
        f"{name} {float(ratio) * 100:.0f}%"
        for name, ratio in (fft_data.get("regions") or {}).items()
    )

    fig, axes = plt.subplots(3, 4, figsize=(16, 11), constrained_layout=True)
    fig.suptitle(
        "9_send_firebase 실시간 분석\n"
        f"{source_label}  |  {event['detectedAt'].astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"{_yamnet_line(yamnet, yamnet_url)}\n"
        f"db {event['db']}  |  주파수 {event['frequencyHz']} Hz  |  "
        f"noiseType {event['noiseType']}  |  분류 {event['yamnetLabel']} ({event['confidence'] * 100:.0f}%)\n"
        f"FFT {fft_data.get('label', profile.label)} "
        f"(centroid {fft_data.get('centroidHz', metrics['centroid_hz'])} Hz, "
        f"dominant {fft_data.get('dominantHz', metrics['dominant_hz'])} Hz)  |  "
        f"음역 {region_text}",
        fontsize=11,
        fontweight="bold",
    )

    region_groups: dict[str, list[tuple]] = {region: [] for region in REGIONS}
    for cat in NOISE_CATEGORIES:
        region_groups[cat[1]].append(cat)

    for row, region in enumerate(REGIONS):
        cats = region_groups[region]
        region_ratio = float(metrics.get(f"region_{region}", 0.0))

        for col in range(3):
            cat_id, _, sub_label, low_hz, high_hz, _desc = cats[col]
            ratio = float(metrics.get(f"cat_{cat_id}", 0.0))
            ax = axes[row, col]
            nyquist = sample_rate / 2
            if low_hz >= nyquist:
                ax.text(
                    0.5,
                    0.5,
                    "대역 초과\n(샘플레이트 한계)",
                    ha="center",
                    va="center",
                    transform=ax.transAxes,
                )
                ax.set_title(
                    f"{cat_id} {sub_label}\n{low_hz:.0f}–{high_hz:.0f} Hz  ·  N/A",
                    fontsize=10,
                )
            else:
                filtered = bandpass(audio, sample_rate, low_hz, high_hz)
                times = np.arange(filtered.size) / sample_rate
                peak = float(np.max(np.abs(filtered)))
                ymax = waveform_ylim(filtered)
                ax.plot(times, filtered, color="#1f77b4", linewidth=0.9)
                ax.set_xlim(0, times[-1] if times.size else 1)
                ax.set_ylim(-ymax, ymax)
                ax.set_title(
                    f"{cat_id} {sub_label}\n{low_hz:.0f}–{high_hz:.0f} Hz  ·  {ratio * 100:.1f}%",
                    fontsize=10,
                )
                if peak >= WAVEFORM_MIN_PEAK:
                    ax.text(
                        0.98,
                        0.95,
                        f"±{peak:.4f}",
                        transform=ax.transAxes,
                        ha="right",
                        va="top",
                        fontsize=7,
                        color="#555555",
                        bbox={"boxstyle": "round,pad=0.2", "facecolor": "white", "alpha": 0.7},
                    )
            ax.set_xlabel("시간 (s)", fontsize=8)
            ax.set_ylabel("진폭", fontsize=8)
            ax.tick_params(labelsize=7)
            ax.grid(True, alpha=0.25)

        summary_ax = axes[row, 3]
        labels = [c[2] for c in cats]
        values = [float(metrics.get(f"cat_{c[0]}", 0.0)) * 100 for c in cats]
        colors = (
            ["#8c564b", "#e377c2", "#7f7f7f"]
            if region == "저음역"
            else (
                ["#2ca02c", "#17becf", "#bcbd22"]
                if region == "중음역"
                else ["#ff7f0e", "#d62728", "#9467bd"]
            )
        )
        bars = summary_ax.bar(labels, values, color=colors, edgecolor="white", linewidth=0.8)
        ymax = max(5.0, max(values) * 1.2) if values else 5.0
        summary_ax.set_ylim(0, ymax)
        summary_ax.set_title(f"{region} 요약  ·  {region_ratio * 100:.1f}%", fontsize=10)
        summary_ax.set_ylabel("에너지 (%)", fontsize=8)
        summary_ax.tick_params(labelsize=8)
        for bar, value in zip(bars, values):
            if value > 0:
                summary_ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + ymax * 0.02,
                    f"{value:.1f}%",
                    ha="center",
                    va="bottom",
                    fontsize=8,
                )

    fig.text(
        0.5,
        0.005,
        f"{profile.detail}  |  마스킹 {'필요' if event['maskingRequired'] else '불필요'}  |  {_top_bands(metrics)}",
        ha="center",
        va="bottom",
        fontsize=9,
        style="italic",
        wrap=True,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="jpg", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="9_send_firebase 기준 3x4 격자 JPG 생성")
    parser.add_argument("--wav", type=Path, help="분석할 WAV (없으면 --record 사용)")
    parser.add_argument("--record", action="store_true", help="마이크에서 4초 녹음 후 분석")
    parser.add_argument("--device", "-d", help="입력 장치 이름 일부 또는 인덱스")
    parser.add_argument("--rate", type=int, default=48_000, help="샘플레이트 Hz")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--capture-wav", type=Path, default=DEFAULT_CAPTURE, help="녹음 WAV 저장 경로")
    parser.add_argument("--seconds", type=float, default=RECORD_SECONDS)
    parser.add_argument("--yamnet-url", default=DEFAULT_YAMNET_URL)
    parser.add_argument("--no-yamnet", action="store_true")
    parser.add_argument("--no-wind-filter", action="store_true")
    args = parser.parse_args()

    plt.rcParams["font.family"] = ["NanumGothic", "DejaVu Sans", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

    yamnet_url = None if args.no_yamnet else args.yamnet_url
    wind_filter = not args.no_wind_filter
    device: int | str | None = args.device
    if device is not None:
        try:
            device = int(device)
        except ValueError:
            pass

    if args.record:
        running_pid = send_firebase_pid()
        if running_pid is not None:
            print(
                f"send_firebase가 실행 중입니다 (PID {running_pid}).\n"
                "  --record는 마이크를 빼앗아 send_firebase를 멈추게 합니다.\n"
                "  대신 아래 명령을 사용하세요:\n"
                "    .venv/bin/python3 plot_wav_grid.py --wav last_capture.wav\n"
                "  꼭 새로 녹음해야 한다면 send_firebase를 먼저 종료하세요.",
                file=sys.stderr,
            )
            return 1
        sample_rate, _raw_audio, analysis_audio, device_name = record_audio(
            seconds=args.seconds,
            sample_rate=args.rate,
            device=device,
            wind_filter=wind_filter,
        )
        save_mono_wav(args.capture_wav, sample_rate, analysis_audio)
        print(f"캡처 저장: {args.capture_wav}")
        source_label = f"마이크 [{device_name}]"
    elif args.wav and args.wav.exists():
        sample_rate, analysis_audio = load_mono_wav(args.wav, args.seconds)
        if wind_filter:
            analysis_audio = prepare_analysis_audio(analysis_audio, sample_rate, True)
        source_label = args.wav.name
    elif DEFAULT_CAPTURE.exists():
        sample_rate, analysis_audio = load_mono_wav(DEFAULT_CAPTURE, args.seconds)
        if wind_filter:
            analysis_audio = prepare_analysis_audio(analysis_audio, sample_rate, True)
        source_label = DEFAULT_CAPTURE.name
        print(f"기본 캡처 사용: {DEFAULT_CAPTURE}")
    else:
        print("WAV 파일이 없습니다. --record 또는 --wav 를 지정하세요.", file=sys.stderr)
        return 1

    event, profile, metrics = analyze_like_send_firebase(analysis_audio, sample_rate, yamnet_url)
    event["detectedAt"] = datetime.now(timezone.utc)

    yamnet = event.get("yamnet")
    yamnet_raw = None
    if isinstance(yamnet, dict) and yamnet.get("status") == "online":
        yamnet_raw = {
            "primary_label": yamnet.get("primaryLabel"),
            "primary_score": yamnet.get("primaryScore"),
            "predictions": [
                {
                    "rank": p.get("rank"),
                    "label": p.get("label"),
                    "score": p.get("score"),
                }
                for p in yamnet.get("predictions", [])
            ],
            "_input_peak_dbfs": yamnet.get("inputPeakDbfs"),
            "_yamnet_gain_db": yamnet.get("gainDb"),
        }

    plot_wav_grid(
        analysis_audio,
        sample_rate,
        args.output,
        event,
        profile,
        metrics,
        yamnet_raw,
        yamnet_url,
        source_label,
    )
    print(f"저장 완료: {args.output}")
    print(
        f"  {event['yamnetLabel']} | db {event['db']} | {profile.label} | "
        f"noiseType {event['noiseType']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())