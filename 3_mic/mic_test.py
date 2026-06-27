#!/usr/bin/env python3
"""USB 마이크 테스트."""

import argparse
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

from config import (
    ALSA_DEVICE,
    CHANNELS,
    PLAYBACK_DEVICE,
    PLAYBACK_VOLUME,
    RECORD_SECONDS,
    SAMPLE_RATE,
)


def check_arecord():
    if not shutil.which("arecord"):
        print("오류: arecord가 없습니다. alsa-utils를 설치하세요.", file=sys.stderr)
        print("  sudo apt install alsa-utils", file=sys.stderr)
        sys.exit(1)


def list_devices():
    print("녹음 장치 목록 (arecord -l):")
    print("-" * 50)
    result = subprocess.run(
        ["arecord", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        sys.exit(1)
    print(result.stdout)
    print("사용 예: plughw:3,0  (card 3, device 0)")


def find_usb_mic_device():
    result = subprocess.run(
        ["arecord", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        match = re.match(r"^card (\d+):.*USB", line, re.IGNORECASE)
        if match:
            return f"plughw:{match.group(1)},0"
    return None


def rms_level(samples):
    if not samples:
        return 0.0
    square_sum = sum(s * s for s in samples)
    return math.sqrt(square_sum / len(samples))


def peak_level(samples):
    return max((abs(s) for s in samples), default=0)


def level_bar(value, max_value=32768, width=40):
    ratio = min(value / max_value, 1.0)
    filled = int(ratio * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def record_with_meter(device, seconds, sample_rate=SAMPLE_RATE, channels=CHANNELS):
    chunk_frames = int(sample_rate * 0.1)
    chunk_bytes = chunk_frames * channels * 2

    proc = subprocess.Popen(
        [
            "arecord", "-q",
            "-D", device,
            "-f", "S16_LE",
            "-r", str(sample_rate),
            "-c", str(channels),
            "-t", "raw",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    frames = []
    peaks = []
    total_chunks = int(seconds / 0.1)

    print(f"{seconds}초 녹음 중 — 마이크에 소리를 내세요.")
    try:
        for i in range(total_chunks):
            data = proc.stdout.read(chunk_bytes)
            if not data:
                break

            samples = struct.unpack(f"<{len(data) // 2}h", data)
            frames.append(data)
            chunk_peak = peak_level(samples)
            peaks.append(chunk_peak)

            bar = level_bar(chunk_peak)
            print(f"\r  {bar} peak={chunk_peak:5d}", end="", flush=True)

        print()
    finally:
        proc.terminate()
        proc.wait(timeout=2)

    if proc.returncode not in (0, -15, None) and not frames:
        err = proc.stderr.read().decode(errors="replace").strip()
        raise RuntimeError(err or "arecord failed")

    return b"".join(frames), peaks


def write_wav(path, raw_data, sample_rate=SAMPLE_RATE, channels=CHANNELS):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw_data)


def amplify_for_playback(raw_data, volume=PLAYBACK_VOLUME):
    """녹음 원본은 유지하고, 재생용으로 음량만 키운다."""
    samples = struct.unpack(f"<{len(raw_data) // 2}h", raw_data)
    peak = peak_level(samples)
    if peak < 1:
        return raw_data

    gain = (32767 * volume) / peak
    boosted = []
    for sample in samples:
        value = int(sample * gain)
        value = max(-32768, min(32767, value))
        boosted.append(value)

    return struct.pack(f"<{len(boosted)}h", *boosted)


def analyze_audio(raw_data):
    samples = struct.unpack(f"<{len(raw_data) // 2}h", raw_data)
    rms = rms_level(samples)
    peak = peak_level(samples)
    return rms, peak


def play_wav(path, device=PLAYBACK_DEVICE):
    if not shutil.which("aplay"):
        print("경고: aplay 없음 — 재생 건너뜀", file=sys.stderr)
        return

    result = subprocess.run(
        ["aplay", "-q", "-D", device, path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "aplay failed").strip()
        raise RuntimeError(msg)


def judge_result(peak, rms):
    if peak < 200:
        return "입력 거의 없음 — 마이크 연결/음량을 확인하세요."
    if peak < 2000:
        return "약한 입력 감지 — 더 가까이서 말해 보세요."
    return "정상 — 마이크 입력이 잘 감지됩니다."


def main():
    parser = argparse.ArgumentParser(description="USB 마이크 테스트")
    parser.add_argument(
        "-d", "--device",
        default=None,
        help=f"ALSA 장치 (기본: USB 자동탐지 또는 {ALSA_DEVICE})",
    )
    parser.add_argument(
        "-t", "--seconds",
        type=float,
        default=RECORD_SECONDS,
        help=f"녹음 시간 (기본: {RECORD_SECONDS}초)",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="녹음 장치 목록 출력",
    )
    parser.add_argument(
        "-p", "--playback",
        action="store_true",
        help="녹음 내용을 3.5mm 스피커로 재생",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="녹음 파일 저장 경로 (.wav)",
    )
    args = parser.parse_args()

    check_arecord()

    if args.list:
        list_devices()
        return

    device = args.device or find_usb_mic_device() or ALSA_DEVICE

    print("USB 마이크 테스트")
    print(f"장치: {device}")
    print("USB 마이크를 연결했는지 확인하세요.")
    print()

    try:
        raw_data, peaks = record_with_meter(device, args.seconds)
        if not raw_data:
            raise RuntimeError("녹음 데이터가 없습니다.")

        rms, peak = analyze_audio(raw_data)
        max_chunk_peak = max(peaks) if peaks else peak

        print()
        print("결과:")
        print(f"  peak : {peak}")
        print(f"  rms  : {rms:.1f}")
        print(f"  판정 : {judge_result(peak, rms)}")

        out_path = args.output
        if out_path:
            write_wav(out_path, raw_data)
            print(f"  저장 : {out_path}")
        else:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                out_path = tmp.name
                write_wav(out_path, raw_data)

        if args.playback:
            print()
            print(f"3.5mm 스피커로 재생 ({PLAYBACK_DEVICE}, 볼륨 {PLAYBACK_VOLUME:.0%})")
            playback_data = amplify_for_playback(raw_data)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as pb_tmp:
                playback_path = pb_tmp.name
                write_wav(playback_path, playback_data)
            try:
                play_wav(playback_path)
            finally:
                os.unlink(playback_path)

        if not args.output:
            os.unlink(out_path)

        print()
        print("테스트 완료")
    except RuntimeError as e:
        print(f"오류: {e}", file=sys.stderr)
        print("  python3 mic_test.py --list  로 장치 확인", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()