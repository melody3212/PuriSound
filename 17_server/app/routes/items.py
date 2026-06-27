from datetime import datetime, timezone
from uuid import uuid4

from flask import Blueprint, jsonify, request

items_bp = Blueprint("items", __name__, url_prefix="/api/items")

_items: dict[str, dict] = {}


def serialize_item(doc):
    return {
        "id": doc["id"],
        "title": doc["title"],
        "description": doc.get("description", ""),
        "created_at": doc["created_at"].isoformat(),
        "updated_at": doc["updated_at"].isoformat(),
    }


@items_bp.route("", methods=["GET"])
def list_items():
    items = sorted(_items.values(), key=lambda doc: doc["created_at"], reverse=True)
    return jsonify({"items": [serialize_item(doc) for doc in items], "count": len(items)})


@items_bp.route("/<item_id>", methods=["GET"])
def get_item(item_id):
    doc = _items.get(item_id)
    if doc is None:
        return jsonify({"error": "Item not found"}), 404

    return jsonify(serialize_item(doc))


@items_bp.route("", methods=["POST"])
def create_item():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400

    now = datetime.now(timezone.utc)
    item_id = str(uuid4())
    doc = {
        "id": item_id,
        "title": title,
        "description": (data.get("description") or "").strip(),
        "created_at": now,
        "updated_at": now,
    }
    _items[item_id] = doc
    return jsonify(serialize_item(doc)), 201


@items_bp.route("/<item_id>", methods=["PUT"])
def update_item(item_id):
    doc = _items.get(item_id)
    if doc is None:
        return jsonify({"error": "Item not found"}), 404

    data = request.get_json(silent=True) or {}
    updates = {}

    if "title" in data:
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "title cannot be empty"}), 400
        updates["title"] = title

    if "description" in data:
        updates["description"] = (data.get("description") or "").strip()

    if not updates:
        return jsonify({"error": "No fields to update"}), 400

    doc.update(updates)
    doc["updated_at"] = datetime.now(timezone.utc)
    return jsonify(serialize_item(doc))


@items_bp.route("/<item_id>", methods=["DELETE"])
def delete_item(item_id):
    if item_id not in _items:
        return jsonify({"error": "Item not found"}), 404

    del _items[item_id]
    return jsonify({"message": "Item deleted"})