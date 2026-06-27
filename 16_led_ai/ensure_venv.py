"""rpi_ws281x가 없으면 1_LED venv Python으로 자동 재실행."""

import os
import sys

LED_DIR = "/data/1_LED"
VENV_PYTHON = os.path.join(LED_DIR, "venv", "bin", "python3")


def reexec_if_needed() -> None:
    try:
        import rpi_ws281x  # noqa: F401
        return
    except ImportError:
        pass

    if os.path.isfile(VENV_PYTHON):
        os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)