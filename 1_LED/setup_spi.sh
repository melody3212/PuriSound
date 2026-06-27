#!/bin/bash
# WS2812B SPI 모드(GPIO 10) — 내장 오디오와 충돌 없음
set -euo pipefail

if [[ "${EUID:-}" -ne 0 ]]; then
    echo "root 권한이 필요합니다:"
    echo "  sudo $0"
    exit 1
fi

CONFIG=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "$candidate" ]]; then
        CONFIG="$candidate"
        break
    fi
done

if [[ -z "$CONFIG" ]]; then
    echo "오류: config.txt를 찾을 수 없습니다."
    exit 1
fi

if grep -q '^#dtparam=spi=on' "$CONFIG"; then
    sed -i 's/^#dtparam=spi=on/dtparam=spi=on/' "$CONFIG"
    echo "수정: $CONFIG (dtparam=spi=on)"
elif grep -q '^dtparam=spi=on' "$CONFIG"; then
    echo "이미 설정됨: $CONFIG (dtparam=spi=on)"
else
    echo "" >> "$CONFIG"
    echo "# WS2812B NeoPixel SPI (GPIO 10)" >> "$CONFIG"
    echo "dtparam=spi=on" >> "$CONFIG"
    echo "추가: $CONFIG (dtparam=spi=on)"
fi

if ! grep -q '^core_freq=250' "$CONFIG"; then
    echo "" >> "$CONFIG"
    echo "# Pi 3 SPI 클럭 고정 (WS2812 타이밍)" >> "$CONFIG"
    echo "core_freq=250" >> "$CONFIG"
    echo "추가: $CONFIG (core_freq=250)"
else
    echo "이미 설정됨: $CONFIG (core_freq=250)"
fi

echo ""
echo "설정 완료. 재부팅 필요: sudo reboot"
echo "배선: DIN → GPIO 10 (물리 핀 19)"