"""rpi_ws281x가 없으면 1_LED venv Python으로 자동 재실행."""

import os
import sys

VENV_DIR = os.path.join(os.path.dirname(__file__), "venv")
VENV_PYTHON = os.path.join(VENV_DIR, "bin", "python3")


def _in_venv():
    return sys.prefix == VENV_DIR or sys.prefix.startswith(VENV_DIR + os.sep)


def reexec_if_needed():
    try:
        import rpi_ws281x  # noqa: F401
        return
    except ImportError:
        pass

    if _in_venv():
        return

    if os.path.isfile(VENV_PYTHON):
        os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)