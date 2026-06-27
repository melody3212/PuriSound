#!/usr/bin/env python3
"""9_send_firebase가 저장한 noiseEvents를 Firebase에서 읽어 실시간으로 표시합니다."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ensure_venv import reexec_if_needed

reexec_if_needed()

import firebase_admin
from firebase_admin import credentials, db, firestore

_DATA_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DATA_ROOT))
from puri_env import DEFAULT_DB_URL, DEFAULT_DEVICE_ID  # noqa: E402

SEND_FIREBASE_DIR = Path("/data/9_send_firebase")
DEFAULT_INTERVAL = 1.0
FETCH_LIMIT = 30

running = True


def stop(_signum=None, _frame=None) -> None:
    global running
    running = False


def init_firebase(
    cred_path: Path,
    database_url: str | None,
    use_firestore: bool,
) -> None:
    if firebase_admin._apps:
        return
    cred = credentials.Certificate(str(cred_path))
    options: dict[str, str] = {}
    if not use_firestore and database_url:
        options["databaseURL"] = database_url
    firebase_admin.initialize_app(cred, options or None)


def parse_detected_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if hasattr(value, "timestamp"):
        return datetime.fromtimestamp(value.timestamp(), tz=timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def event_sort_key(event: dict[str, Any]) -> float:
    detected_at = parse_detected_at(event.get("detectedAt"))
    if detected_at is not None:
        return detected_at.timestamp()
    return 0.0


def _normalize_event(data: dict[str, Any], doc_id: str) -> dict[str, Any]:
    event = dict(data)
    event["noiseEventId"] = event.get("noiseEventId") or doc_id
    return event


def fetch_latest_firestore(device_id: str) -> dict[str, Any] | None:
    client = firestore.client()
    try:
        query = (
            client.collection("devices")
            .document(device_id)
            .collection("noiseEvents")
            .order_by("detectedAt", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        for doc in query.stream():
            return _normalize_event(doc.to_dict() or {}, doc.id)
    except Exception:
        pass

    docs = (
        client.collection("devices")
        .document(device_id)
        .collection("noiseEvents")
        .limit(FETCH_LIMIT)
        .stream()
    )
    events: list[dict[str, Any]] = []
    for doc in docs:
        events.append(_normalize_event(doc.to_dict() or {}, doc.id))
    if not events:
        return None
    events.sort(key=event_sort_key, reverse=True)
    return events[0]


def fetch_latest_rtdb(device_id: str) -> dict[str, Any] | None:
    ref = db.reference(f"devices/{device_id}/noiseEvents")
    snapshot = ref.order_by_key().limit_to_last(FETCH_LIMIT).get()
    if not snapshot:
        return None

    events: list[dict[str, Any]] = []
    if isinstance(snapshot, dict):
        for key, value in snapshot.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            item["noiseEventId"] = item.get("noiseEventId") or key
            events.append(item)

    if not events:
        return None
    events.sort(key=event_sort_key, reverse=True)
    return events[0]


def fetch_latest_event(device_id: str, use_firestore: bool) -> dict[str, Any] | None:
    if use_firestore:
        return fetch_latest_firestore(device_id)
    return fetch_latest_rtdb(device_id)


DEFAULT_YAMNET_GAIN_DB = 10.0
REGION_ORDER = ("저음역", "중음역", "고음역")
BAND_ORDER = ("L1", "L2", "L3", "M1", "M2", "M3", "H1", "H2", "H3")


def format_detected_at_raw(value: Any) -> str:
    detected_at = parse_detected_at(value)
    if detected_at is None:
        return str(value)
    return detected_at.isoformat()


def yamnet_status_line(yamnet_data: dict[str, Any] | None) -> str:
    if not yamnet_data:
        return "  YAMNet   : 상세 없음 (구버전 이벤트 — 9_send_firebase 재시작 필요)"

    status = str(yamnet_data.get("status", "unknown"))
    if status == "disabled":
        return "  YAMNet   : 사용 안 함 (--no-yamnet)"
    if status == "unavailable":
        return "  YAMNet   : 꺼짐 (오디오 부족, FFT 분류만 사용)"
    if status == "error":
        err = str(yamnet_data.get("error", "unknown"))
        if len(err) > 48:
            err = err[:45] + "..."
        return f"  YAMNet   : 꺼짐 ({err}) → FFT 분류 사용"

    primary = yamnet_data.get("primaryLabel")
    score = float(yamnet_data.get("primaryScore", 0.0) or 0.0)
    if not primary:
        return "  YAMNet   : 켜짐 — (유효 분류 없음, FFT 분류 사용)"

    line = f"  YAMNet   : 켜짐 — {primary} ({score * 100:.0f}%)"
    input_peak = yamnet_data.get("inputPeakDbfs")
    gain_db = yamnet_data.get("gainDb", DEFAULT_YAMNET_GAIN_DB)
    if input_peak is not None:
        line += f"  [입력 {float(input_peak):.0f} dBFS, +{float(gain_db):.0f} dB 보정]"
    return line


def fft_detail_lines(fft_data: dict[str, Any] | None) -> list[str]:
    if not fft_data:
        return [
            "  FFT      : 상세 없음 (구버전 이벤트 — 9_send_firebase 재시작 필요)"
        ]

    confidence = fft_data.get("confidence")
    confidence_text = (
        f"{float(confidence) * 100:.0f}%"
        if isinstance(confidence, (int, float))
        else "-"
    )

    lines = [
        f"  FFT      : {fft_data.get('label', '-')}",
        f"  FFT 설명 : {fft_data.get('detail', '-')}",
        f"  FFT 신뢰 : {confidence_text}",
        f"  RMS      : {fft_data.get('rmsDbfs', '-')} dBFS",
        f"  중심주파수: {fft_data.get('centroidHz', '-')} Hz",
        f"  지배톤   : {fft_data.get('dominantHz', '-')} Hz",
        f"  flatness : {fft_data.get('flatness', '-')}",
        f"  humScore : {fft_data.get('humScore', '-')}",
        "  ── 음역별 에너지 ──",
    ]

    regions = fft_data.get("regions") or {}
    for region in REGION_ORDER:
        if region not in regions:
            continue
        ratio = float(regions[region])
        bar = "█" * int(ratio * 30)
        lines.append(f"  {region:<8} {ratio * 100:5.1f}% {bar}")

    lines.append("  ── 세분 대역 (9종) ──")
    bands = fft_data.get("bands") or []
    band_map = {
        str(band.get("id")): band
        for band in bands
        if isinstance(band, dict) and band.get("id")
    }

    current_region = ""
    for band_id in BAND_ORDER:
        band = band_map.get(band_id)
        if not band:
            continue
        region = str(band.get("region", ""))
        if region != current_region:
            current_region = region
            lines.append(f"  [{region}]")
        ratio = float(band.get("ratio", 0.0))
        bar = "█" * int(ratio * 30)
        sub_label = band.get("subLabel", "")
        low_hz = float(band.get("lowHz", 0.0))
        high_hz = float(band.get("highHz", 0.0))
        label = f"    {sub_label} {low_hz:.0f}-{high_hz:.0f}Hz"
        lines.append(f"  {label:<26} {ratio * 100:5.1f}% {bar}")

    return lines


def yamnet_top5_lines(yamnet_data: dict[str, Any] | None) -> list[str]:
    if not yamnet_data or yamnet_data.get("status") != "online":
        return []

    lines: list[str] = []
    predictions = yamnet_data.get("predictions") or []
    for pred in predictions[:5]:
        if not isinstance(pred, dict):
            continue
        rank = pred.get("rank", len(lines) + 1)
        label = pred.get("label", "?")
        score = float(pred.get("score", 0.0))
        lines.append(f"  YAMNet #{rank}: {label} ({score * 100:.1f}%)")
    return lines


def format_event(event: dict[str, Any], device_id: str) -> str:
    noise_type = str(event.get("noiseType", "-"))
    confidence = event.get("confidence")
    confidence_text = (
        f"{float(confidence) * 100:.0f}%"
        if isinstance(confidence, (int, float))
        else "-"
    )
    masking_required = event.get("maskingRequired")
    if masking_required is True:
        masking_text = "필요"
    elif masking_required is False:
        masking_text = "불필요"
    else:
        masking_text = "-"

    event_id = event.get("noiseEventId", "-")
    yamnet_label = event.get("yamnetLabel", "-")
    yamnet_data = event.get("yamnet")
    if not isinstance(yamnet_data, dict):
        yamnet_data = None
    fft_data = event.get("fft")
    if not isinstance(fft_data, dict):
        fft_data = None

    lines = [
        "",
        "─" * 50,
        f"  noiseEvents 조회  │  감지 시각 {format_detected_at_raw(event.get('detectedAt'))}",
        f"  path     : devices/{device_id}/noiseEvents/{event_id}",
        yamnet_status_line(yamnet_data),
        *yamnet_top5_lines(yamnet_data),
        *fft_detail_lines(fft_data),
        f"  db       : {event.get('db', '-')}",
        f"  주파수   : {event.get('frequencyHz', '-')} Hz",
        f"  noiseType: {noise_type}",
        f"  분류     : {yamnet_label} ({confidence_text})",
        f"  마스킹   : {masking_text}",
        "─" * 50,
    ]
    return "\n".join(lines)


def event_key(event: dict[str, Any]) -> str:
    event_id = event.get("noiseEventId")
    if event_id:
        return str(event_id)
    detected_at = parse_detected_at(event.get("detectedAt"))
    if detected_at is not None:
        return detected_at.isoformat()
    return ""


def print_event_if_new(
    event: dict[str, Any] | None,
    device_id: str,
    last_key: str | None,
) -> str | None:
    if event is None:
        print(
            f"\n[{datetime.now().strftime('%H:%M:%S')}] "
            "아직 noiseEvents가 없습니다. 9_send_firebase 전송을 기다리는 중...",
            flush=True,
        )
        return last_key

    current_key = event_key(event)
    if current_key and current_key != last_key:
        print(format_event(event, device_id), flush=True)
        return current_key
    return last_key


def watch_firestore_loop(args: argparse.Namespace) -> None:
    client = firestore.client()
    last_key: str | None = None

    try:
        latest = fetch_latest_firestore(args.device_id)
        last_key = print_event_if_new(latest, args.device_id, last_key)
    except Exception as exc:
        print(f"[오류] 최신 이벤트 조회 실패: {exc}", flush=True)

    col = (
        client.collection("devices")
        .document(args.device_id)
        .collection("noiseEvents")
        .order_by("detectedAt", direction=firestore.Query.DESCENDING)
        .limit(1)
    )

    def on_snapshot(docs, _changes, _read_time) -> None:
        nonlocal last_key
        if not running or not docs:
            return
        doc = docs[0]
        event = _normalize_event(doc.to_dict() or {}, doc.id)
        last_key = print_event_if_new(event, args.device_id, last_key)

    watcher = col.on_snapshot(on_snapshot)

    try:
        while running:
            time.sleep(0.2)
    finally:
        watcher.unsubscribe()


def poll_loop(args: argparse.Namespace) -> None:
    use_firestore = not args.rtdb
    last_key: str | None = None

    while running:
        try:
            event = fetch_latest_event(args.device_id, use_firestore)
            last_key = print_event_if_new(event, args.device_id, last_key)
        except Exception as exc:
            print(f"\n[오류] Firebase 조회 실패: {exc}", flush=True)

        deadline = time.monotonic() + args.interval
        while running and time.monotonic() < deadline:
            time.sleep(0.1)


def run_loop(args: argparse.Namespace) -> None:
    cred_path = Path(args.cred)
    use_firestore = not args.rtdb

    init_firebase(cred_path, args.database_url, use_firestore)

    print("=== PuriSound noiseEvents 뷰어 (13_noise_ai) ===")
    print(f"대상: devices/{args.device_id}/noiseEvents")
    print(f"저장소: {'Firestore' if use_firestore else 'Realtime DB'}")
    if use_firestore and not args.poll:
        print("모드: 실시간 감시 (Firestore on_snapshot)")
    else:
        print(f"모드: 폴링 ({args.interval:.0f}초)")
    print("종료: Ctrl+C\n")

    if use_firestore and not args.poll:
        watch_firestore_loop(args)
    else:
        poll_loop(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Firebase noiseEvents(YAMNet+FFT)를 주기적으로 조회해 표시"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"조회 주기 초 (기본 {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--cred",
        type=Path,
        default=SEND_FIREBASE_DIR / "firebase.json",
        help="Firebase 서비스 계정 JSON 경로",
    )
    parser.add_argument(
        "--device-id",
        default=DEFAULT_DEVICE_ID,
        help=f"devices 문서 ID (기본 {DEFAULT_DEVICE_ID})",
    )
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DB_URL,
        help=f"Realtime Database URL (--rtdb 사용 시, 기본 {DEFAULT_DB_URL})",
    )
    parser.add_argument(
        "--rtdb",
        action="store_true",
        help="Firestore 대신 Realtime Database 사용",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="실시간 감시 대신 주기적 폴링 사용",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cred_path = Path(args.cred)
    if not cred_path.is_file():
        print(f"Firebase 인증 파일을 찾을 수 없습니다: {cred_path}", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    run_loop(args)
    print("\n종료합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())