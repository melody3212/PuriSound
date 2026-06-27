#!/bin/bash
# Raspberry Pi 3B+ setup script

set -e

echo "=== PuriSound FFT - Pi 3B+ 설정 ==="

sudo apt-get update
sudo apt-get install -y \
    python3-pip python3-numpy python3-scipy \
    python3-pyaudio portaudio19-dev \
    ffmpeg libsdl2-dev libsdl2-mixer-dev \
    alsa-utils

pip3 install -r requirements.txt

echo ""
echo "설치 완료."
echo ""
echo "1) mp3 FFT 사전 분석 (선택, 첫 실행 시 자동으로도 됨):"
echo "  python3 preanalyze_masking.py"
echo ""
echo "2) 소음 감지 마스킹 실행:"
echo "  nice -n -10 python3 noise_controller_pi3.py"
echo ""
echo "선택: 블루투스 끄기 (CPU 절약)"
echo "  sudo systemctl stop bluetooth"