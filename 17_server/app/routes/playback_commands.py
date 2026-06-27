"""마스킹 재생 명령 저장 API."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from flask import Blueprint, jsonify, request

from app.storage import append_record, latest_record, list_records

playback_bp = Blueprint("playback_commands", __name__, url_prefix="/api/playback-commands")
_COLLECTION = "playback_commands"


def _serialize(record: dict) -> dict:
    return {
        "id": record["id"],
        "device_id": record.get("device_id"),
        "noise_event_id": record.get("noise_event_id"),
        "detected_at": record.get("detected_at"),
        "command": record.get("command") or {},
        "decision": record.get("decision") or {},
        "created_at": record["created_at"],
    }


@playback_bp.route("", methods=["GET"])
def list_playback_commands():
    device_id = (request.args.get("device_id") or "").strip() or None
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400

    records = list_records(_COLLECTION, device_id=device_id, limit=limit)
    return jsonify(
        {
            "commands": [_serialize(row) for row in records],
            "count": len(records),
        }
    )


@playback_bp.route("/latest", methods=["GET"])
def get_latest_playback_command():
    device_id = (request.args.get("device_id") or "").strip() or None
    record = latest_record(_COLLECTION, device_id=device_id)
    if record is None:
        return jsonify({"error": "No playback command found"}), 404
    return jsonify(_serialize(record))


@playback_bp.route("", methods=["POST"])
def create_playback_command():
    data = request.get_json(silent=True) or {}
    device_id = (data.get("device_id") or "").strip()
    command = data.get("command")
    decision = data.get("decision")

    if not device_id:
        return jsonify({"error": "device_id is required"}), 400
    if not isinstance(command, dict):
        return jsonify({"error": "command must be an object"}), 400
    if decision is not None and not isinstance(decision, dict):
        return jsonify({"error": "decision must be an object"}), 400

    now = datetime.now(timezone.utc).isoformat()
    record = {
        "id": str(uuid4()),
        "device_id": device_id,
        "noise_event_id": (data.get("noise_event_id") or "").strip() or None,
        "detected_at": data.get("detected_at"),
        "command": command,
        "decision": decision or {},
        "created_at": now,
    }
    append_record(_COLLECTION, record)
    return jsonify(_serialize(record)), 201