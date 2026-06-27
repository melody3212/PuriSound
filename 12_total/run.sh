#!/usr/bin/env bash
# PuriSound 통합 실행 — 옵션 없이 분석·재생·LED·Firebase 전체 동작
set -euo pipefail
cd "$(dirname "$0")"
exec python3 total_controller.py "$@"