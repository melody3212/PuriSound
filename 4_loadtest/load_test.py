#!/usr/bin/env python3
"""LED + 스피커 + 마이크 30분 동시 부하 테스트."""

import argparse
import importlib.util
import math
import os
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime

sys.path.insert(0, "/data/1_LED")
from ensure_venv import reexec_if_needed

reexec_if_needed()

from load_config import (
    DURATION_SECONDS,
    LED_BRIGHTNESS,
    LED_CHANNEL,
    LED_COUNT,
    LED_DMA,
    LED_DIR,
    LED_FREQ_HZ,
    LED_INVERT,
    LED_PIN,
    LOG_FILE,
    MIC_CHANNELS,
    MIC_DEVICE,
    MIC_LOG_INTERVAL,
    MIC_SAMPLE_RATE,
    SPEAKER_BEEP_DURATION,
    SPEAKER_BEEP_INTERVAL,
    SPEAKER_DEVICE,
    SPEAKER_SAMPLE_RATE,
    SPEAKER_SCALE_INTERVAL,
    SPEAKER_VOLUME,
    STATUS_INTERVAL,
)

running = True
stats_lock = threading.Lock()
stats = {
    "led_cycles": 0,
    "speaker_beeps": 0,
    "speaker_errors": 0,
    "mic_checks": 0,
    "mic_peak_max": 0,
    "mic_errors": 0,
    "errors": [],
}


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def stop(_signum=None, _frame=None):
    global running
    running = False


def load_module(name, path, base_dir):
    old_path = sys.path[:]
    sys.path.insert(0, base_dir)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path = old_path


def find_headphones_device():
    result = subprocess.run(["aplay", "-l"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        match = re.match(r"^card (\d+):.*Headphones", line)
        if match:
            return f"plughw:{match.group(1)},0"
    return None


def find_usb_mic_device():
    result = subprocess.run(["arecord", "-l"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        match = re.match(r"^card (\d+):.*USB", line, re.IGNORECASE)
        if match:
            return f"plughw:{match.group(1)},0"
    return None


def check_spi():
    if not os.path.exists("/dev/spidev0.0"):
        raise RuntimeError("SPI 비활성화 — sudo /data/1_LED/setup_spi.sh 후 재부팅")


def record_error(source, msg):
    with stats_lock:
        stats["errors"].append(f"{source}: {msg}")
        if source == "speaker":
            stats["speaker_errors"] += 1
        elif source == "mic":
            stats["mic_errors"] += 1


def wheel(pos):
    from rpi_ws281x import Color

    if pos < 85:
        return Color(pos * 3, 255 - pos * 3, 0)
    if pos < 170:
        pos -= 85
        return Color(255 - pos * 3, 0, pos * 3)
    pos -= 170
    return Color(0, pos * 3, 255 - pos * 3)


def led_worker(strip):
    from rpi_ws281x import Color

    log("LED 스레드 시작")
    cycle = 0
    colors = [
        Color(255, 0, 0),
        Color(0, 255, 0),
        Color(0, 0, 255),
        Color(255, 255, 255),
    ]

    try:
        while running:
            cycle += 1
            with stats_lock:
                stats["led_cycles"] = cycle

            for color in colors:
                if not running:
                    break
                for i in range(strip.numPixels()):
                    strip.setPixelColor(i, color)
                strip.show()
                time.sleep(0.5)

            for j in range(0, 256, 4):
                if not running:
                    break
                for i in range(strip.numPixels()):
                    strip.setPixelColor(i, wheel((i + j) & 255))
                strip.show()
                time.sleep(0.03)

            for i in range(strip.numPixels()):
                if not running:
                    break
                for px in range(strip.numPixels()):
                    strip.setPixelColor(px, Color(0, 0, 0))
                strip.setPixelColor(i, Color(255, 128, 0))
                strip.show()
                time.sleep(0.05)
    except Exception as e:
        record_error("led", str(e))
        log(f"LED 오류: {e}")
    finally:
        for i in range(strip.numPixels()):
            strip.setPixelColor(i, Color(0, 0, 0))
        strip.show()
        log("LED 스레드 종료")


def speaker_worker(device, speaker_mod):
    log(f"스피커 스레드 시작 ({device})")
    beep_freqs = [262, 330, 392, 440, 523]
    freq_idx = 0
    last_scale = time.monotonic()
    last_beep = 0.0

    try:
        while running:
            now = time.monotonic()

            if now - last_scale >= SPEAKER_SCALE_INTERVAL:
                for freq, duration in speaker_mod.TEST_NOTES:
                    if not running:
                        break
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                        speaker_mod.write_wav(
                            tmp.name,
                            speaker_mod.make_tone(freq, duration),
                        )
                        speaker_mod.play_wav(tmp.name, device)
                last_scale = now
                log("스피커: 음계 테스트 완료")

            if now - last_beep >= SPEAKER_BEEP_INTERVAL:
                freq = beep_freqs[freq_idx % len(beep_freqs)]
                freq_idx += 1
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                    speaker_mod.write_wav(
                        tmp.name,
                        speaker_mod.make_tone(
                            freq, SPEAKER_BEEP_DURATION,
                            sample_rate=SPEAKER_SAMPLE_RATE,
                            volume=SPEAKER_VOLUME,
                        ),
                    )
                    speaker_mod.play_wav(tmp.name, device)
                with stats_lock:
                    stats["speaker_beeps"] += 1
                last_beep = now

            time.sleep(0.1)
    except Exception as e:
        record_error("speaker", str(e))
        log(f"스피커 오류: {e}")
    finally:
        log("스피커 스레드 종료")


def peak_level(samples):
    return max((abs(s) for s in samples), default=0)


def rms_level(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def mic_worker(device):
    log(f"마이크 스레드 시작 ({device})")
    chunk_frames = int(MIC_SAMPLE_RATE * 0.5)
    chunk_bytes = chunk_frames * MIC_CHANNELS * 2
    last_log = time.monotonic()

    proc = subprocess.Popen(
        [
            "arecord", "-q",
            "-D", device,
            "-f", "S16_LE",
            "-r", str(MIC_SAMPLE_RATE),
            "-c", str(MIC_CHANNELS),
            "-t", "raw",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        while running:
            data = proc.stdout.read(chunk_bytes)
            if not data:
                err = proc.stderr.read().decode(errors="replace").strip()
                raise RuntimeError(err or "arecord 종료")

            samples = struct.unpack(f"<{len(data) // 2}h", data)
            peak = peak_level(samples)
            rms = rms_level(samples)

            with stats_lock:
                stats["mic_checks"] += 1
                if peak > stats["mic_peak_max"]:
                    stats["mic_peak_max"] = peak

            now = time.monotonic()
            if now - last_log >= MIC_LOG_INTERVAL:
                log(f"마이크: peak={peak} rms={rms:.1f}")
                last_log = now
    except Exception as e:
        record_error("mic", str(e))
        log(f"마이크 오류: {e}")
    finally:
        proc.terminate()
        proc.wait(timeout=2)
        log("마이크 스레드 종료")


def print_status(elapsed, total):
    with stats_lock:
        s = dict(stats)
    remaining = max(0, total - elapsed)
    log(
        f"진행 {elapsed/60:.1f}/{total/60:.1f}분 "
        f"(남은 {remaining/60:.1f}분) | "
        f"LED cycle={s['led_cycles']} | "
        f"speaker beep={s['speaker_beeps']} err={s['speaker_errors']} | "
        f"mic check={s['mic_checks']} peak_max={s['mic_peak_max']} err={s['mic_errors']}"
    )


def main():
    global running

    parser = argparse.ArgumentParser(description="LED+스피커+마이크 동시 부하 테스트")
    parser.add_argument(
        "-t", "--minutes",
        type=float,
        default=DURATION_SECONDS / 60,
        help=f"테스트 시간(분), 기본 {DURATION_SECONDS / 60:.0f}분",
    )
    args = parser.parse_args()
    duration = int(args.minutes * 60)

    if not os.path.isdir(LED_DIR):
        print(f"오류: {LED_DIR} 없음", file=sys.stderr)
        sys.exit(1)

    sys.path.insert(0, LED_DIR)
    from rpi_ws281x import PixelStrip

    speaker_mod = load_module(
        "speaker_test", "/data/2_speaker/speaker_test.py", "/data/2_speaker",
    )

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    speaker_device = find_headphones_device() or SPEAKER_DEVICE
    mic_device = find_usb_mic_device() or MIC_DEVICE

    try:
        check_spi()
    except RuntimeError as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)

    open(LOG_FILE, "w", encoding="utf-8").close()

    log("=" * 60)
    log(f"부하 테스트 시작 — {args.minutes:.0f}분")
    log(f"LED: SPI GPIO {LED_PIN} / 스피커: {speaker_device} / 마이크: {mic_device}")
    log("Ctrl+C로 중단")
    log("=" * 60)

    strip = PixelStrip(
        LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA,
        LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL,
    )
    strip.begin()

    threads = [
        threading.Thread(target=led_worker, args=(strip,), name="led", daemon=True),
        threading.Thread(
            target=speaker_worker,
            args=(speaker_device, speaker_mod),
            name="speaker",
            daemon=True,
        ),
        threading.Thread(target=mic_worker, args=(mic_device,), name="mic", daemon=True),
    ]

    for t in threads:
        t.start()

    start = time.monotonic()
    last_status = start

    try:
        while running and (time.monotonic() - start) < duration:
            time.sleep(1)
            now = time.monotonic()
            if now - last_status >= STATUS_INTERVAL:
                print_status(now - start, duration)
                last_status = now
    finally:
        running = False
        for t in threads:
            t.join(timeout=5)

        elapsed = time.monotonic() - start
        log("=" * 60)
        log(f"부하 테스트 종료 — 실행 시간 {elapsed/60:.1f}분")
        print_status(elapsed, duration)
        if stats["errors"]:
            log(f"오류 {len(stats['errors'])}건:")
            for err in stats["errors"][-10:]:
                log(f"  - {err}")
        else:
            log("오류 없음")
        log("=" * 60)


if __name__ == "__main__":
    main()