#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export SDL_AUDIODRIVER="${SDL_AUDIODRIVER:-alsa}"
export AUDIODEV="${AUDIODEV:-plughw:CARD=Headphones,DEV=0}"
exec python3 player_run.py "$@"