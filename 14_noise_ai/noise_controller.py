#!/usr/bin/env python3
"""Firebase noiseEvents(13번 DB) + 10_masking FFT 프로필 → 마스킹 결정 및 재생 명령."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DATA_ROOT))
sys.path.insert(0, str(ROOT))

from puri_env import DEFAULT_DB_URL, DEFAULT_DEVICE_ID  # noqa: E402

from firebase_client import (  # noqa: E402
    debug_firebase,
    dump_event_fields,
    event_key,
    fetch_latest_event,
    init_firebase,
    is_quota_error,
    parse_detected_at,
    set_debug_firebase,
    summarize_event,
)
from masking_decider import (  # noqa: E402
    DEFAULT_PROFILES_JSON,
    DEFAULT_SOUNDS_DIR,
    MAX_MASKING_TRACKS,
    MaskingDecision,
    decide_masking,
    load_candidates,
    load_fill_threshold,
)
from player_client import PlayerCommandWriter  # noqa: E402

SEND_FIREBASE_DIR = DATA_ROOT / "9_send_firebase"

running = True


def stop(_signum=None, _frame=None) -> None:
    global running
    running = False


SAMPLE_QUIET_EVENT: dict[str, Any] = {
    "deviceId": DEFAULT_DEVICE_ID,
    "detectedAt": "2026-06-22T07:14:52.296054+00:00",
    "noiseEventId": "XQ11MrrvH691HBTLZEJv",
    "db": 38,
    "frequencyHz": 187.5,
    "noiseType": "brown",
    "yamnetLabel": "조용함",
    "confidence": 0.9,
    "maskingRequired": False,
    "fft": {
        "label": "조용함",
        "detail": "유의미한 소음이 거의 감지되지 않습니다.",
        "confidence": 0.9,
        "rmsDbfs": -61.5253,
        "centroidHz": 462.7,
        "dominantHz": 187.5,
        "flatness": 0.0502,
        "humScore": 1.0,
        "regions": {
            "저음역": 0.737,
            "중음역": 0.257,
            "고음역": 0.006,
        },
    },
    "yamnet": {
        "status": "online",
        "primaryLabel": "Animal",
        "primaryScore": 0.199,
    },
}


SAMPLE_LOUD_EVENT: dict[str, Any] = {
    "deviceId": DEFAULT_DEVICE_ID,
    "detectedAt": "2026-06-22T08:00:00+00:00",
    "noiseEventId": "sample_loud",
    "db": 52,
    "frequencyHz": 187.5,
    "noiseType": "brown",
    "yamnetLabel": "기계음",
    "confidence": 0.82,
    "maskingRequired": True,
    "fft": {
        "label": "기계음 · 저음",
        "detail": "저음역 에너지가 높습니다.",
        "confidence": 0.82,
        "rmsDbfs": -48.0,
        "centroidHz": 462.7,
        "dominantHz": 187.5,
        "regions": {
            "저음역": 0.737,
            "중음역": 0.257,
            "고음역": 0.006,
        },
    },
}


def format_event_header(event: dict[str, Any], device_id: str) -> str:
    detected = parse_detected_at(event.get("detectedAt"))
    detected_text = detected.isoformat() if detected else str(event.get("detectedAt", "-"))
    event_id = event.get("noiseEventId", "-")
    return (
        f"noiseEvents → 마스킹 결정 │ {detected_text}\n"
        f"  path     : devices/{device_id}/noiseEvents/{event_id}"
    )


def format_decision_block(decision: MaskingDecision) -> str:
    lines = ["─" * 50, *decision.summary_lines(), "─" * 50]
    return "\n".join(lines)


def apply_decision(decision: MaskingDecision, writer: PlayerCommandWriter) -> None:
    writer.send_decision(decision)


def run_test_samples(args: argparse.Namespace) -> None:
    profiles_json = Path(args.profiles_json)
    candidates = load_candidates(
        profiles_json=profiles_json,
        sounds_dir=Path(args.sounds_dir),
    )
    fill_threshold = load_fill_threshold(profiles_json)
    print(
        f"=== 14_noise_ai 샘플 테스트 ({len(candidates)}개 마스킹 후보, "
        f"최대 {args.max_tracks}트랙) ===\n"
    )

    for label, event in (
        ("조용함 (마스킹 불필요)", SAMPLE_QUIET_EVENT),
        ("소음 감지 (brown, db=52)", SAMPLE_LOUD_EVENT),
    ):
        print(f"[{label}]")
        print(format_event_header(event, args.device_id))
        decision = decide_masking(
            event,
            candidates,
            db_threshold=args.db_threshold,
            db_full=args.db_full,
            min_volume=args.min_volume,
            max_volume=args.max_volume,
            hold_sec=args.hold_sec,
            max_tracks=args.max_tracks,
            fill_threshold=fill_threshold,
        )
        print(format_decision_block(decision))
        print()


def run_loop(args: argparse.Namespace) -> None:
    set_debug_firebase(args.debug_firebase)

    profiles_json = Path(args.profiles_json)
    candidates = load_candidates(
        profiles_json=profiles_json,
        sounds_dir=Path(args.sounds_dir),
    )
    fill_threshold = load_fill_threshold(profiles_json)
    cred_path = Path(args.cred)
    use_firestore = not args.rtdb
    if not args.dry_run:
        init_firebase(str(cred_path), args.database_url, use_firestore)
    elif args.debug_firebase:
        debug_firebase("dry-run 모드 — Firebase noiseEvents 조회 생략, SAMPLE_LOUD_EVENT 사용")

    writer = PlayerCommandWriter(
        device_id=args.device_id,
        use_firestore=use_firestore,
        publish_firebase=not args.dry_run,
        write_ipc=not args.no_ipc,
    )

    print("=== PuriSound noise_ai (14_noise_ai) ===")
    print(f"마스킹 후보: {len(candidates)}개 ({args.sounds_dir})")
    print(f"프로필 JSON: {args.profiles_json}")
    print(f"대상: devices/{args.device_id}/noiseEvents")
    print(f"저장소: {'Firestore' if use_firestore else 'Realtime DB'}")
    print(
        f"db 임계: {args.db_threshold:.0f} | 음량: "
        f"{args.min_volume:.0%}~{args.max_volume:.0%} | 홀드: {args.hold_sec:.0f}s"
    )
    print(
        f"레이어: 최대 {args.max_tracks}트랙 | "
        f"결핍 임계: {fill_threshold:.2f}"
    )
    if args.dry_run:
        print("명령 출력: dry-run — Firebase 미기록 (IPC만)")
        if not args.no_ipc:
            print("  IPC 폴백: /tmp/player_ai_command.json")
    else:
        storage = "playbackCommands/latest" if use_firestore else "playbackCommand"
        print(f"명령 출력: Firebase devices/{args.device_id}/{storage}")
        if not args.no_ipc:
            print("  IPC 폴백: /tmp/player_ai_command.json (16_led_ai 호환)")
    print("15·16번은 별도 터미널에서 각각 실행하세요.")
    if args.debug_firebase:
        print("Firebase 디버그: ON (--debug-firebase)")
    print("종료: Ctrl+C\n")

    last_key: str | None = None
    current_files: list[str] = []
    current_noise_type: str | None = None
    last_switch_at = 0.0
    poll_count = 0
    quota_backoff_sec = 0.0

    while running:
        try:
            poll_count += 1
            if args.dry_run:
                event = SAMPLE_LOUD_EVENT.copy()
                event["detectedAt"] = datetime.now(timezone.utc).isoformat()
                debug_firebase(
                    f"poll #{poll_count} dry-run 샘플 — {summarize_event(event)}"
                )
            else:
                debug_firebase(
                    f"poll #{poll_count} Firebase noiseEvents 조회 — "
                    f"device={args.device_id}"
                )
                event = fetch_latest_event(args.device_id, use_firestore)

            if event is None:
                debug_firebase(f"poll #{poll_count} 이벤트 없음 — {args.interval:.1f}s 대기")
                time.sleep(args.interval)
                continue

            key = event_key(event)
            debug_firebase(
                f"poll #{poll_count} 수신 — key={key!r} last_key={last_key!r} "
                f"{summarize_event(event)}"
            )
            dump_event_fields(event)

            if key and key == last_key:
                debug_firebase(
                    f"poll #{poll_count} 동일 이벤트 — 마스킹 재결정 생략 "
                    f"(새 noiseEvent 대기 중)"
                )
            else:
                now = time.monotonic()
                seconds_since_switch = (
                    now - last_switch_at if last_switch_at else args.hold_sec
                )

                decision = decide_masking(
                    event,
                    candidates,
                    db_threshold=args.db_threshold,
                    db_full=args.db_full,
                    min_volume=args.min_volume,
                    max_volume=args.max_volume,
                    current_files=current_files,
                    current_noise_type=current_noise_type,
                    hold_sec=args.hold_sec,
                    seconds_since_switch=seconds_since_switch,
                    max_tracks=args.max_tracks,
                    fill_threshold=fill_threshold,
                )

                print(format_event_header(event, args.device_id))
                print(format_decision_block(decision))

                if decision.action == "play":
                    current_files = decision.track_names()
                    current_noise_type = decision.noise_type
                    last_switch_at = now
                elif decision.action == "stop":
                    current_files = []
                    current_noise_type = None
                elif decision.action == "hold" and decision.tracks:
                    current_files = decision.track_names()

                apply_decision(decision, writer)
                last_key = key
                quota_backoff_sec = 0.0
                debug_firebase(
                    f"poll #{poll_count} 처리 완료 — action={decision.action} "
                    f"last_key={last_key!r}"
                )

        except Exception as exc:
            print(f"\n[오류] {exc}", flush=True)
            debug_firebase(f"poll #{poll_count} 예외: {exc!r}")
            if is_quota_error(exc):
                quota_backoff_sec = min(
                    300.0,
                    max(args.interval, quota_backoff_sec * 2 or 30.0),
                )
                print(
                    f"[안내] Firestore 일일/분당 읽기 한도 초과 — "
                    f"{quota_backoff_sec:.0f}초 후 재시도 "
                    f"(13번 뷰어·9번 전송과 동시 실행 시 한도 소진 빠름)",
                    flush=True,
                )

        wait_sec = max(args.interval, quota_backoff_sec)
        deadline = time.monotonic() + wait_sec
        while running and time.monotonic() < deadline:
            time.sleep(0.1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Firebase noiseEvents 기반 마스킹 결정 및 재생 명령"
    )
    parser.add_argument(
        "--profiles-json",
        default=str(DEFAULT_PROFILES_JSON),
        help="10_masking FFT 프로필 JSON",
    )
    parser.add_argument(
        "--sounds-dir",
        default=str(DEFAULT_SOUNDS_DIR),
        help="10_masking 마스킹 MP3 폴더",
    )
    parser.add_argument("--cred", type=Path, default=SEND_FIREBASE_DIR / "firebase.json")
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    parser.add_argument("--database-url", default=DEFAULT_DB_URL)
    parser.add_argument("--rtdb", action="store_true", help="Realtime DB 사용")
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Firebase 조회 주기(초, 기본 3)",
    )
    parser.add_argument("--db-threshold", type=float, default=40.0, help="마스킹 시작 dB")
    parser.add_argument(
        "--db-full",
        type=float,
        default=75.0,
        help="최대 음량에 도달하는 dB",
    )
    parser.add_argument("--min-volume", type=float, default=0.72, help="최소 재생 음량")
    parser.add_argument("--max-volume", type=float, default=1.0, help="최대 재생 음량")
    parser.add_argument(
        "--hold-sec",
        type=float,
        default=30.0,
        help="동일 noiseType 시 트랙 교체 최소 대기(초)",
    )
    parser.add_argument(
        "--max-tracks",
        type=int,
        default=MAX_MASKING_TRACKS,
        help="결핍 대역 채움용 최대 트랙 수",
    )
    parser.add_argument(
        "--test-sample",
        action="store_true",
        help="Firebase 없이 샘플 이벤트 2건으로 결정 테스트",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Firebase 대신 loud 샘플 이벤트로 폴링 루프 테스트",
    )
    parser.add_argument(
        "--debug-firebase",
        action="store_true",
        help="Firebase 수신/스킵/기록 상태를 [FB DEBUG]로 출력",
    )
    parser.add_argument(
        "--no-ipc",
        action="store_true",
        help="Firebase만 사용 (/tmp/player_ai_command.json 미기록)",
    )
    return parser


def main() -> None:
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    args = build_parser().parse_args()
    if not 1 <= args.max_tracks <= MAX_MASKING_TRACKS:
        raise SystemExit(f"--max-tracks는 1~{MAX_MASKING_TRACKS} 사이여야 합니다.")

    if args.test_sample:
        run_test_samples(args)
        return

    run_loop(args)


if __name__ == "__main__":
    main()