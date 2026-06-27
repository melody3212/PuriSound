#!/usr/bin/env python3
"""3.5mm 잭 스피커 테스트 (bcm2835 내장 오디오)."""

import argparse
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

from config import (
    ALSA_DEVICE,
    BEEP_DURATION,
    PAUSE,
    SAMPLE_RATE,
    TEST_NOTES,
    VOLUME,
)


def check_aplay():
    if not shutil.which("aplay"):
        print("오류: aplay가 없습니다. alsa-utils를 설치하세요.", file=sys.stderr)
        print("  sudo apt install alsa-utils", file=sys.stderr)
        sys.exit(1)


def list_devices():
    print("재생 장치 목록 (aplay -l):")
    print("-" * 50)
    result = subprocess.run(
        ["aplay", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        sys.exit(1)
    print(result.stdout)
    print("사용 예: plughw:1,0  (card 1, device 0)")


def find_headphones_device():
    result = subprocess.run(
        ["aplay", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        match = re.match(r"^card (\d+):.*Headphones", line)
        if match:
            return f"plughw:{match.group(1)},0"
    return None


def make_tone(freq, duration, sample_rate=SAMPLE_RATE, volume=VOLUME):
    n_samples = int(sample_rate * duration)
    fade = min(int(sample_rate * 0.01), n_samples // 4) or 1
    frames = []

    for i in range(n_samples):
        t = i / sample_rate
        sample = math.sin(2 * math.pi * freq * t)

        if i < fade:
            sample *= i / fade
        elif i >= n_samples - fade:
            sample *= (n_samples - i) / fade

        value = int(sample * volume * 32767)
        value = max(-32768, min(32767, value))
        frames.append(struct.pack("<h", value))

    return b"".join(frames)


def write_wav(path, audio_data, sample_rate=SAMPLE_RATE):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data)


def play_wav(path, device):
    result = subprocess.run(
        ["aplay", "-q", "-D", device, path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "aplay failed").strip()
        raise RuntimeError(msg)


def play_beep(device, freq=440, duration=BEEP_DURATION):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        write_wav(tmp.name, make_tone(freq, duration))
        play_wav(tmp.name, device)


def play_scale(device):
    import time

    print("도레미파솔라시도 재생")
    for freq, duration in TEST_NOTES:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            write_wav(tmp.name, make_tone(freq, duration))
            play_wav(tmp.name, device)
        time.sleep(PAUSE)


def play_stereo_ping(device):
    """좌/우 채널 번갈아 테스트 (모노 스피커에서도 소리 확인용)."""
    import time

    print("좌-우 번갈아 비프 (모노 출력)")
    for label, freq in (("좌", 330), ("우", 660)):
        print(f"  {label}")
        play_beep(device, freq=freq, duration=0.25)
        time.sleep(PAUSE)


def main():
    parser = argparse.ArgumentParser(description="3.5mm 스피커 테스트")
    parser.add_argument(
        "-d", "--device",
        default=None,
        help=f"ALSA 장치 (기본: 자동탐지 또는 {ALSA_DEVICE})",
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="재생 장치 목록 출력",
    )
    args = parser.parse_args()

    check_aplay()

    if args.list:
        list_devices()
        return

    device = args.device or find_headphones_device() or ALSA_DEVICE

    print("3.5mm 스피커 테스트")
    print(f"장치: {device}")
    print("스피커를 3.5mm 잭에 연결했는지 확인하세요.")
    print()

    try:
        print("1) 단일 비프 (440Hz)")
        play_beep(device)

        print("2) 음계 테스트")
        play_scale(device)

        print("3) 좌우 번갈아 비프")
        play_stereo_ping(device)

        print()
        print("테스트 완료 — 소리가 들렸으면 정상입니다.")
    except RuntimeError as e:
        print(f"오류: {e}", file=sys.stderr)
        print("  python3 speaker_test.py --list  로 장치 확인", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()