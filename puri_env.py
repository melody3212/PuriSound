"""Environment-backed PuriSound settings.

Copy .env.example to .env and fill in values for local/Pi deployment.
"""

from __future__ import annotations

import os


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DEFAULT_YAMNET_URL = env("PURI_YAMNET_URL", "http://127.0.0.1:5000")
DEFAULT_DB_URL = env("PURI_FIREBASE_DB_URL")
DEFAULT_DEVICE_ID = env("PURI_DEVICE_ID")
DEFAULT_DEVICE_NAME = env("PURI_DEVICE_NAME", "PuriSound Speaker")
DEFAULT_OWNER_ID = env("PURI_OWNER_ID")