#!/bin/bash
# LED(rpi_ws281x)는 1_LED venv 필요
set -euo pipefail
exec /data/1_LED/venv/bin/python3 /data/4_loadtest/load_test.py "$@"