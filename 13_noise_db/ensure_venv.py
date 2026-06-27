"""firebase-admin이 없으면 9_send_firebase venv로 자동 재실행."""

from __future__ import annotations

import os
import sys
from pathlib import Path

FIREBASE_VENV_PYTHON = Path("/data/9_send_firebase/.venv/bin/python3")


def reexec_if_needed() -> None:
    if os.environ.get("NOISE_AI_VENV") == "1":
        return
    try:
        import firebase_admin  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    if FIREBASE_VENV_PYTHON.is_file():
        os.environ["NOISE_AI_VENV"] = "1"
        os.execv(str(FIREBASE_VENV_PYTHON), [str(FIREBASE_VENV_PYTHON), *sys.argv])