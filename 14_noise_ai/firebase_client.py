"""13_noise_db와 동일한 방식으로 Firebase noiseEvents를 조회합니다."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

import firebase_admin
from firebase_admin import credentials, db, firestore

FETCH_LIMIT = 30
_debug_firebase = False


def set_debug_firebase(enabled: bool) -> None:
    global _debug_firebase
    _debug_firebase = enabled


def debug_firebase(message: str) -> None:
    if _debug_firebase:
        print(f"[FB DEBUG] {message}", flush=True, file=sys.stderr)


def is_quota_error(exc: BaseException) -> bool:
    text = str(exc)
    return "429" in text or "Quota exceeded" in text or "quota" in text.lower()


def summarize_event(event: dict[str, Any] | None) -> str:
    if event is None:
        return "event=None"
    detected = parse_detected_at(event.get("detectedAt"))
    detected_text = detected.isoformat() if detected else str(event.get("detectedAt", "-"))
    return (
        f"id={event.get('noiseEventId', '-')} "
        f"detectedAt={detected_text} "
        f"db={event.get('db', '-')} "
        f"noiseType={event.get('noiseType', '-')} "
        f"maskingRequired={event.get('maskingRequired', '-')}"
    )


def summarize_command(command: dict[str, Any] | None) -> str:
    if command is None:
        return "command=None"
    tracks = command.get("tracks") or []
    track_names = [
        str(track.get("name") or track.get("path", "?"))
        for track in tracks
        if isinstance(track, dict)
    ]
    return (
        f"seq={command.get('seq', '-')} "
        f"action={command.get('action', '-')} "
        f"tracks={len(tracks)} "
        f"names={track_names or ['-']}"
    )


def init_firebase(
    cred_path: str,
    database_url: str | None,
    use_firestore: bool,
) -> None:
    if firebase_admin._apps:
        debug_firebase("Firebase 이미 초기화됨 — 재사용")
        return
    cred = credentials.Certificate(cred_path)
    options: dict[str, str] = {}
    if not use_firestore and database_url:
        options["databaseURL"] = database_url
    firebase_admin.initialize_app(cred, options or None)
    storage = "Firestore" if use_firestore else f"RTDB ({database_url})"
    debug_firebase(f"Firebase 초기화 완료 — 저장소={storage}, cred={cred_path}")


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
    path = f"devices/{device_id}/noiseEvents"
    client = firestore.client()
    debug_firebase(f"Firestore 조회 시작 — path={path}")
    try:
        query = (
            client.collection("devices")
            .document(device_id)
            .collection("noiseEvents")
            .order_by("detectedAt", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        for doc in query.stream():
            event = _normalize_event(doc.to_dict() or {}, doc.id)
            debug_firebase(
                f"Firestore order_by 성공 — doc_id={doc.id} {summarize_event(event)}"
            )
            return event
        debug_firebase("Firestore order_by 결과 없음 — 문서 0건")
    except Exception as exc:
        if is_quota_error(exc):
            raise
        debug_firebase(f"Firestore order_by 실패 — fallback 사용: {exc}")

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
    debug_firebase(f"Firestore fallback — 최근 {FETCH_LIMIT}건 중 {len(events)}건 수신")
    if not events:
        debug_firebase("Firestore noiseEvents 없음")
        return None
    events.sort(key=event_sort_key, reverse=True)
    latest = events[0]
    debug_firebase(f"Firestore fallback 최신 — {summarize_event(latest)}")
    return latest


def fetch_latest_rtdb(device_id: str) -> dict[str, Any] | None:
    path = f"devices/{device_id}/noiseEvents"
    debug_firebase(f"RTDB 조회 시작 — path={path}")
    ref = db.reference(path)
    snapshot = ref.order_by_key().limit_to_last(FETCH_LIMIT).get()
    if not snapshot:
        debug_firebase("RTDB noiseEvents 없음")
        return None

    events: list[dict[str, Any]] = []
    if isinstance(snapshot, dict):
        for key, value in snapshot.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            item["noiseEventId"] = item.get("noiseEventId") or key
            events.append(item)

    debug_firebase(f"RTDB 수신 — 최근 {FETCH_LIMIT}건 중 {len(events)}건")
    if not events:
        debug_firebase("RTDB 파싱 가능한 이벤트 없음")
        return None
    events.sort(key=event_sort_key, reverse=True)
    latest = events[0]
    debug_firebase(f"RTDB 최신 — {summarize_event(latest)}")
    return latest


def fetch_latest_event(device_id: str, use_firestore: bool) -> dict[str, Any] | None:
    if use_firestore:
        return fetch_latest_firestore(device_id)
    return fetch_latest_rtdb(device_id)


def event_key(event: dict[str, Any]) -> str:
    event_id = event.get("noiseEventId")
    if event_id:
        return str(event_id)
    detected_at = parse_detected_at(event.get("detectedAt"))
    if detected_at is not None:
        return detected_at.isoformat()
    return ""


PLAYBACK_COMMAND_DOC = "latest"


def write_playback_command(
    device_id: str,
    command: dict[str, Any],
    *,
    use_firestore: bool,
) -> None:
    """14번 마스킹 결정 → 15번 재생기용 Firebase 명령 기록."""
    if use_firestore:
        path = f"devices/{device_id}/playbackCommands/{PLAYBACK_COMMAND_DOC}"
        (
            firestore.client()
            .collection("devices")
            .document(device_id)
            .collection("playbackCommands")
            .document(PLAYBACK_COMMAND_DOC)
            .set(command)
        )
        debug_firebase(f"Firestore 기록 완료 — path={path} {summarize_command(command)}")
        return
    path = f"devices/{device_id}/playbackCommand"
    db.reference(path).set(command)
    debug_firebase(f"RTDB 기록 완료 — path={path} {summarize_command(command)}")


def fetch_playback_command(
    device_id: str,
    *,
    use_firestore: bool,
) -> dict[str, Any] | None:
    if use_firestore:
        path = f"devices/{device_id}/playbackCommands/{PLAYBACK_COMMAND_DOC}"
        doc = (
            firestore.client()
            .collection("devices")
            .document(device_id)
            .collection("playbackCommands")
            .document(PLAYBACK_COMMAND_DOC)
            .get()
        )
        if not doc.exists:
            debug_firebase(f"Firestore playbackCommands 없음 — path={path}")
            return None
        data = doc.to_dict()
        command = data if isinstance(data, dict) else None
        debug_firebase(f"Firestore playbackCommands 수신 — path={path} {summarize_command(command)}")
        return command

    path = f"devices/{device_id}/playbackCommand"
    data = db.reference(path).get()
    command = data if isinstance(data, dict) else None
    debug_firebase(f"RTDB playbackCommand 수신 — path={path} {summarize_command(command)}")
    return command


def dump_event_fields(event: dict[str, Any]) -> None:
    """수신 이벤트 전체 필드를 디버그용으로 출력합니다."""
    if not _debug_firebase:
        return
    try:
        payload = json.dumps(event, ensure_ascii=False, default=str, indent=2)
    except TypeError:
        payload = str(event)
    debug_firebase(f"이벤트 raw:\n{payload}")