"""Environment-backed PuriSound settings.

Loads .env from this file's directory, then reads PURI_* variables.
"""

from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    env_file = _ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


_load_dotenv()

DEFAULT_YAMNET_URL = env("PURI_YAMNET_URL", "http://127.0.0.1:5000")
DEFAULT_DB_URL = env("PURI_FIREBASE_DB_URL")
DEFAULT_DEVICE_ID = env("PURI_DEVICE_ID")
DEFAULT_DEVICE_NAME = env("PURI_DEVICE_NAME", "PuriSound Speaker")
DEFAULT_OWNER_ID = env("PURI_OWNER_ID")