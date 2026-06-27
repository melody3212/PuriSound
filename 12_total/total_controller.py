#!/usr/bin/env python3
"""노이즈 분석(9) → 마스킹 재생(7) → LED 동기화(11) 통합 컨트롤러."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import numpy as np
import pyaudio

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DATA_ROOT))
from puri_env import DEFAULT_YAMNET_URL  # noqa: E402

SEND_FIREBASE_DIR = Path("/data/9_send_firebase")
MIC_FFT_DIR = Path("/data/8_MIC_FFT")
LED_VENV_PYTHON = Path("/data/1_LED/venv/bin/python3")
FIREBASE_VENV_PYTHON = Path("/data/9_send_firebase/.venv/bin/python3")

# send_firebase는 firebase_admin 없으면 venv로 재실행함 → pygame 환경이 깨지므로 스텁 처리
os.environ["SEND_FIREBASE_VENV"] = "1"
if "firebase_admin" not in sys.modules:
    _fb = types.ModuleType("firebase_admin")
    _fb._apps = []
    sys.modules["firebase_admin"] = _fb
    sys.modules["firebase_admin.credentials"] = types.ModuleType("credentials")
    sys.modules["firebase_admin.db"] = types.ModuleType("db")
    sys.modules["firebase_admin.firestore"] = types.ModuleType("firestore")

sys.path.insert(0, str(MIC_FFT_DIR))
sys.path.insert(0, str(SEND_FIREBASE_DIR))
sys.path.insert(0, str(ROOT))

from noise_analyzer import (  # noqa: E402
    CHUNK,
    classify_noise,
    list_input_devices,
    pick_default_input_device,
    resolve_sample_rate,
)
from send_firebase import (  # noqa: E402
    DBFS_TO_DB_OFFSET,
    DEFAULT_DB_URL,
    DEFAULT_DEVICE_ID,
    DEFAULT_OWNER_ID,
    REGION_TO_NOISE_TYPE,
    build_noise_event,
    check_yamnet_server,
    classify_yamnet,
    format_local_log,
    format_wind_skip_log,
    prepare_analysis_audio,
    sanitize_yamnet_result,
    wind_noise_score,
    WIND_SCORE_THRESHOLD,
)

from audio_player import MaskingPlayer  # noqa: E402
from duration_timer import DurationTimer  # noqa: E402
from masking_matcher import (  # noqa: E402
    MaskingCandidate,
    load_candidates,
    mic_profile_from_metrics,
    select_best_masking,
    should_switch_track,
)
from noise_colors import NOISE_LABELS  # noqa: E402
from state_sync import clear_state, write_state  # noqa: E402

LOCAL_ANALYSIS_SEC = 2.0
FIREBASE_RECORD_SEC = 4.0
LED_UPDATE_SEC = 0.5
FAST_LED_MIN_SEC = 1.0

running = True
_led_proc: subprocess.Popen | None = None


def stop(_signum=None, _frame=None) -> None:
    global running
    running = False


def dbfs_to_db(rms_db: float) -> int:
    return int(max(20, min(120, round(DBFS_TO_DB_OFFSET + rms_db))))


def derive_noise_type(metrics: dict[str, float | str]) -> str:
    regions = {
        "저음역": float(metrics["region_저음역"]),
        "중음역": float(metrics["region_중음역"]),
        "고음역": float(metrics["region_고음역"]),
    }
    dominant_region = max(regions, key=regions.get)
    if regions[dominant_region] < 0.35:
        return "pink"
    return REGION_TO_NOISE_TYPE[dominant_region]


def start_led_worker() -> subprocess.Popen | None:
    script = ROOT / "led_worker.py"
    if not LED_VENV_PYTHON.is_file():
        print("LED venv 없음 — LED 없이 실행합니다.", file=sys.stderr)
        return None
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.Popen(
        [str(LED_VENV_PYTHON), str(script)],
        cwd=str(ROOT),
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.3)
    if proc.poll() is not None:
        err = proc.stderr.read() if proc.stderr else ""
        print(f"LED 워커 시작 실패: {err.strip()}", file=sys.stderr)
        return None
    return proc


def stop_led_worker(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def push_firebase_event(
    event: dict[str, Any],
    *,
    cred_path: Path,
    device_id: str,
    database_url: str | None,
    use_firestore: bool,
) -> str | None:
    if not FIREBASE_VENV_PYTHON.is_file():
        print("Firebase venv 없음 — 전송 생략", file=sys.stderr)
        return None

    payload = {
        "cred_path": str(cred_path),
        "device_id": device_id,
        "use_firestore": use_firestore,
        "database_url": database_url,
        "event": {
            **event,
            "detectedAt": event["detectedAt"].isoformat(),
        },
    }
    script = ROOT / "firebase_push.py"
    result = subprocess.run(
        [str(FIREBASE_VENV_PYTHON), str(script)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print(f"Firebase 전송 실패: {result.stderr.strip()}", file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)["key"]
    except (json.JSONDecodeError, KeyError):
        return None


def update_output_state(
    *,
    playing: bool,
    noise_type: str | None,
    masking_file: str | None,
    db: int,
    masking_required: bool,
    label: str,
    status: str = "active",
) -> None:
    write_state(
        playing=playing,
        noise_type=noise_type,
        masking_file=masking_file,
        db=db,
        masking_required=masking_required,
        label=label,
        status=status,
    )


def schedule_firebase_push(
    event: dict[str, Any],
    *,
    cred_path: Path,
    device_id: str,
    database_url: str | None,
    use_firestore: bool,
    dry_run: bool,
    device_id_log: str,
    yamnet_url: str | None,
    yamnet: dict[str, Any] | None,
) -> None:
    def _worker() -> None:
        if dry_run:
            print(
                format_local_log(event, "dry-run", device_id_log, yamnet_url, yamnet),
                flush=True,
            )
            return
        key = push_firebase_event(
            event,
            cred_path=cred_path,
            device_id=device_id,
            database_url=database_url,
            use_firestore=use_firestore,
        )
        if key:
            print(
                format_local_log(event, key, device_id_log, yamnet_url, yamnet),
                flush=True,
            )

    threading.Thread(target=_worker, daemon=True).start()


def run_loop(args: argparse.Namespace) -> None:
    global _led_proc

    candidates = load_candidates(
        profiles_json=Path(args.profiles_json),
        sounds_dir=Path(args.sounds_dir),
    )
    print("=== PuriSound 통합 (분석 · 마스킹 · LED · Firebase) ===")
    print(f"마스킹 후보: {len(candidates)}개 ({args.sounds_dir})")

    player = MaskingPlayer(volume=args.volume)
    player.start()

    if not args.no_led:
        update_output_state(
            playing=False,
            noise_type="idle",
            masking_file=None,
            db=0,
            masking_required=False,
            label="시작",
            status="starting",
        )
        _led_proc = start_led_worker()
        if _led_proc is None:
            print("LED 비활성 — 워커 시작 실패", file=sys.stderr)

    pa = pyaudio.PyAudio()
    stream = None

    quiet_timer = DurationTimer(args.stop_sec, args.tolerance_sec)

    loop_start = time.monotonic()
    last_analysis = loop_start - args.interval
    last_led_update = loop_start - LED_UPDATE_SEC
    last_firebase = loop_start - args.firebase_interval
    last_noise_type: str | None = None
    last_candidate: MaskingCandidate | None = None
    active_candidate: MaskingCandidate | None = None
    active_noise_type: str | None = None
    last_switch_at = 0.0
    last_quiet_profile = True
    chunk_buffer: list[np.ndarray] = []
    chunk_samples = 0
    analysis_samples_needed = 0

    yamnet_url = args.yamnet_url if args.yamnet else None
    last_yamnet_online: bool | None = None

    try:
        preferred = str(args.device) if args.device is not None else None
        index = pick_default_input_device(pa, preferred)
        info = pa.get_device_info_by_index(index)
        device_name = str(info.get("name", f"device-{index}"))
        sample_rate = resolve_sample_rate(pa, index, args.rate)
        local_samples_needed = int(sample_rate * LOCAL_ANALYSIS_SEC)
        firebase_samples_needed = int(sample_rate * FIREBASE_RECORD_SEC)
        fast_led_samples_needed = int(sample_rate * FAST_LED_MIN_SEC)
        buffer_samples_needed = firebase_samples_needed
        frames_per_buffer = max(CHUNK, int(sample_rate * 0.02))

        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=index,
            frames_per_buffer=frames_per_buffer,
        )

        print(f"마이크: [{index}] {device_name} @ {sample_rate} Hz")
        print(
            f"분석 {args.interval:.0f}초 | LED {LED_UPDATE_SEC:.1f}초 | "
            f"Firebase {args.firebase_interval:.0f}초(백그라운드) | "
            f"마스킹 유지 {args.hold_sec:.0f}초 | 임계 {args.db_threshold:.0f} dB"
        )
        print(f"정지 대기: {args.stop_sec:.0f}s (정숙 시)")
        if args.no_led:
            print("LED: 비활성 (--no-led)")
        else:
            print("LED: led_worker.py (1_LED venv)")
        if not args.firebase:
            print("Firebase: 꺼짐")
        elif args.dry_run:
            print("Firebase: dry-run")
        else:
            print(f"Firebase: devices/{args.device_id}/noiseEvents")
        if yamnet_url:
            online, msg = check_yamnet_server(yamnet_url)
            print(f"YAMNet: {'켜짐' if online else '꺼짐'} — {msg}")
        else:
            print("YAMNet: 꺼짐 (FFT만 사용)")
        if args.wind_filter:
            print("바람 필터: 켜짐")
        else:
            print("바람 필터: 꺼짐")
        print("종료: Ctrl+C\n")

        chunk_duration = CHUNK / sample_rate

        while running:
            raw = stream.read(CHUNK, exception_on_overflow=False)
            chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            chunk_buffer.append(chunk)
            merged_buffer = np.concatenate(chunk_buffer)
            if merged_buffer.size > buffer_samples_needed:
                merged_buffer = merged_buffer[-buffer_samples_needed:]
            chunk_buffer = [merged_buffer]
            chunk_samples = merged_buffer.size

            rms_db = 20 * np.log10(float(np.sqrt(np.mean(chunk**2))) + 1e-12)
            db_value = dbfs_to_db(rms_db)
            is_loud = db_value >= args.db_threshold

            now = time.monotonic()

            if (
                not args.no_led
                and chunk_samples >= fast_led_samples_needed
                and (now - last_led_update) >= LED_UPDATE_SEC
            ):
                fast_audio = merged_buffer[-fast_led_samples_needed:]
                fast_profile, fast_metrics = classify_noise(fast_audio, sample_rate)
                fast_quiet = fast_profile.label == "조용함"
                fast_noise = derive_noise_type(fast_metrics)
                update_output_state(
                    playing=player.is_active,
                    noise_type="idle" if fast_quiet else fast_noise,
                    masking_file=(
                        player.current_file.name
                        if player.current_file
                        else None
                    ),
                    db=dbfs_to_db(float(fast_metrics["rms_db"])),
                    masking_required=not fast_quiet and is_loud,
                    label=fast_profile.label,
                    status="listening",
                )
                last_led_update = now

            if player.is_active:
                below_threshold = db_value < args.db_threshold
                if quiet_timer.update(last_quiet_profile or below_threshold, chunk_duration):
                    player.stop()
                    active_candidate = None
                    active_noise_type = None
                    print("\n■ 마스킹 정지 (정숙 상태 유지)")

            if _led_proc is not None and _led_proc.poll() is not None:
                err = _led_proc.stderr.read() if _led_proc.stderr else ""
                print(f"LED 워커 종료됨: {err.strip()}", file=sys.stderr)
                _led_proc = None

            if (
                chunk_samples >= local_samples_needed
                and (now - last_analysis) >= args.interval
            ):
                merged = chunk_buffer[0][-local_samples_needed:]

                analysis_audio = prepare_analysis_audio(
                    merged, sample_rate, args.wind_filter
                )
                profile, metrics = classify_noise(analysis_audio, sample_rate)

                yamnet = None
                if yamnet_url:
                    online, status_msg = check_yamnet_server(yamnet_url)
                    if last_yamnet_online is not None and online != last_yamnet_online:
                        state = "켜짐" if online else "꺼짐"
                        print(f"\n[YAMNet 상태 변경] {state} — {status_msg}", flush=True)
                    last_yamnet_online = online
                    yamnet = (
                        classify_yamnet(analysis_audio, sample_rate, yamnet_url)
                        if online
                        else {"error": "서버 연결 불가"}
                    )
                    yamnet = sanitize_yamnet_result(yamnet)

                wind_skip_firebase = False
                if args.wind_filter:
                    wind_score, wind_reason = wind_noise_score(
                        merged, sample_rate, metrics, yamnet
                    )
                    if wind_score >= WIND_SCORE_THRESHOLD:
                        print(format_wind_skip_log(wind_score, wind_reason, yamnet))
                        wind_skip_firebase = True

                noise_type = derive_noise_type(metrics)
                mic_profile = mic_profile_from_metrics(metrics)
                candidate = select_best_masking(
                    mic_profile, candidates, noise_type=noise_type
                )
                last_candidate = candidate
                last_noise_type = noise_type

                if player.is_active and should_switch_track(
                    current=active_candidate,
                    new_candidate=candidate,
                    current_noise_type=active_noise_type,
                    new_noise_type=noise_type,
                    now=now,
                    last_switch_at=last_switch_at,
                    hold_sec=args.hold_sec,
                ):
                    player.play(candidate.path)
                    active_candidate = candidate
                    active_noise_type = noise_type
                    last_switch_at = now
                    print(f"  → 마스킹 교체: {candidate.file_name}")

                quiet = profile.label == "조용함"
                last_quiet_profile = quiet
                analysis_db = dbfs_to_db(float(metrics["rms_db"]))
                masking_required = not quiet and analysis_db >= args.db_threshold

                event = build_noise_event(
                    profile,
                    metrics,
                    args.device_id,
                    args.owner_id,
                    yamnet,
                    yamnet_url,
                )

                if masking_required and not player.is_active:
                    player.play(candidate.path)
                    active_candidate = candidate
                    active_noise_type = noise_type
                    last_switch_at = now
                    quiet_timer.reset()
                    print(f"\n▶ 마스킹 시작: {candidate.file_name}")

                playing = player.is_active
                current = player.current_file
                noise_label = NOISE_LABELS.get(noise_type, noise_type)

                update_output_state(
                    playing=playing,
                    noise_type=noise_type if not quiet else "idle",
                    masking_file=current.name if current else candidate.file_name,
                    db=event["db"],
                    masking_required=masking_required,
                    label=profile.label,
                    status="active",
                )

                status = "재생중" if playing else "대기"
                print(
                    f"[{status}] chunk={db_value} dB analysis={analysis_db} dB | "
                    f"{noise_label} | 후보: {candidate.file_name} | "
                    f"분류: {profile.label} | 마스킹: "
                    f"{'필요' if masking_required else '불필요'}",
                    flush=True,
                )

                if (
                    args.firebase
                    and not wind_skip_firebase
                    and chunk_samples >= firebase_samples_needed
                    and (now - last_firebase) >= args.firebase_interval
                ):
                    fb_audio = chunk_buffer[0][-firebase_samples_needed:]
                    fb_profile, fb_metrics = classify_noise(fb_audio, sample_rate)
                    fb_yamnet = yamnet
                    if yamnet_url and fb_yamnet is None:
                        online, _ = check_yamnet_server(yamnet_url)
                        if online:
                            fb_yamnet = sanitize_yamnet_result(
                                classify_yamnet(fb_audio, sample_rate, yamnet_url)
                            )
                    fb_event = build_noise_event(
                        fb_profile,
                        fb_metrics,
                        args.device_id,
                        args.owner_id,
                        fb_yamnet,
                        yamnet_url,
                    )
                    schedule_firebase_push(
                        fb_event,
                        cred_path=Path(args.cred),
                        device_id=args.device_id,
                        database_url=args.database_url,
                        use_firestore=not args.rtdb,
                        dry_run=args.dry_run,
                        device_id_log=args.device_id,
                        yamnet_url=yamnet_url,
                        yamnet=fb_yamnet,
                    )
                    last_firebase = now

                last_analysis = now

            time.sleep(0.005)

    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        player.close()
        stop_led_worker(_led_proc)
        clear_state()
        if stream is not None:
            stream.stop_stream()
            stream.close()
        pa.terminate()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="노이즈 분석 + 마스킹 재생 + LED 통합 (7/9/11)"
    )
    parser.add_argument("--device", "-d", help="마이크 장치 이름 일부 또는 인덱스")
    parser.add_argument("--list-devices", action="store_true", help="입력 장치 목록")
    parser.add_argument("--rate", type=int, default=48_000, help="샘플레이트 Hz")
    parser.add_argument(
        "--interval", type=float, default=2.0, help="마스킹 분석 주기(초)"
    )
    parser.add_argument(
        "--firebase-interval",
        type=float,
        default=4.0,
        help="Firebase 전송 주기(초, 백그라운드)",
    )
    parser.add_argument(
        "--hold-sec",
        type=float,
        default=30.0,
        help="노이즈 타입 변경 시 최소 대기(초, 흔들림 방지)",
    )
    parser.add_argument("--db-threshold", type=float, default=40.0, help="마스킹 시작 dB")
    parser.add_argument("--stop-sec", type=float, default=5.0, help="재생 정지 누적(초)")
    parser.add_argument("--tolerance-sec", type=float, default=2.0, help="정적 허용(초)")
    parser.add_argument("--volume", type=float, default=0.7, help="마스킹 볼륨")
    parser.add_argument(
        "--profiles-json",
        default="/data/10_masking/masking_fft_profiles.json",
        help="마스킹 FFT 프로필 JSON",
    )
    parser.add_argument(
        "--sounds-dir",
        default="/data/6_FFT/masking_sounds",
        help="마스킹 MP3 폴더",
    )
    parser.add_argument("--no-led", action="store_true", help="LED 워커 비활성")
    parser.add_argument(
        "--yamnet",
        action="store_true",
        help="YAMNet API 분류 사용 (느려질 수 있음)",
    )
    parser.add_argument(
        "--yamnet-url",
        default=DEFAULT_YAMNET_URL,
        help="YAMNet API URL",
    )
    parser.add_argument(
        "--wind-filter",
        action="store_true",
        help="바람소리 필터 사용 (기본: 꺼짐)",
    )
    parser.add_argument(
        "--no-firebase",
        action="store_true",
        help="Firebase 전송 끄기 (기본: 인증 파일 있으면 켜짐)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Firebase dry-run")
    parser.add_argument(
        "--cred",
        type=Path,
        default=SEND_FIREBASE_DIR / "firebase.json",
    )
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    parser.add_argument("--owner-id", default=DEFAULT_OWNER_ID)
    parser.add_argument("--database-url", default=DEFAULT_DB_URL)
    parser.add_argument("--rtdb", action="store_true", help="Realtime DB 사용")
    return parser


def resolve_defaults(args: argparse.Namespace) -> None:
    """옵션 없이 실행해도 전체 파이프라인이 동작하도록 기본값을 맞춥니다."""
    cred_path = Path(args.cred)
    if args.no_firebase:
        args.firebase = False
    elif cred_path.is_file():
        args.firebase = True
    else:
        args.firebase = False
        print(
            f"Firebase 인증 없음 ({cred_path}) — 분석·재생·LED만 동작",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.wind_filter = args.wind_filter
    resolve_defaults(args)

    if args.list_devices:
        pa = pyaudio.PyAudio()
        print("입력 장치 목록:")
        for index, info in list_input_devices(pa):
            print(f"  [{index}] {info['name']}")
        pa.terminate()
        return 0

    device: int | str | None = args.device
    if args.device is not None:
        try:
            device = int(args.device)
        except ValueError:
            device = args.device
    args.device = device

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    run_loop(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())