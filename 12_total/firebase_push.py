#!/usr/bin/env python3
"""9_send_firebase venv에서 noiseEvents 1건을 전송합니다 (stdin JSON)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

FIREBASE_VENV = Path("/data/9_send_firebase/.venv/bin/python3")
if Path(sys.executable).resolve() != FIREBASE_VENV.resolve() and FIREBASE_VENV.is_file():
    os.execv(str(FIREBASE_VENV), [str(FIREBASE_VENV), *sys.argv])

SEND_FIREBASE_DIR = Path("/data/9_send_firebase")
sys.path.insert(0, str(SEND_FIREBASE_DIR))

from send_firebase import init_firebase, push_noise_event  # noqa: E402


def main() -> int:
    payload = json.load(sys.stdin)
    cred_path = Path(payload["cred_path"])
    event = payload["event"]
    device_id = payload["device_id"]
    use_firestore = payload.get("use_firestore", True)
    database_url = payload.get("database_url")

    detected_at = event.get("detectedAt")
    if isinstance(detected_at, str):
        event["detectedAt"] = datetime.fromisoformat(detected_at)
    if event["detectedAt"].tzinfo is None:
        event["detectedAt"] = event["detectedAt"].replace(tzinfo=timezone.utc)

    init_firebase(cred_path, database_url, use_firestore)
    key = push_noise_event(device_id, event, use_firestore)
    print(json.dumps({"key": key}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())