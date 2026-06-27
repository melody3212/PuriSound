"""LED + 스피커 + 마이크 동시 부하 테스트 설정."""

import os

DURATION_MINUTES = 30
DURATION_SECONDS = DURATION_MINUTES * 60

# 1_LED (SPI 네오픽셀)
LED_DIR = "/data/1_LED"
LED_VENV_PYTHON = os.path.join(LED_DIR, "venv", "bin", "python3")
LED_COUNT = 16
LED_PIN = 10
LED_FREQ_HZ = 800000
LED_DMA = 10
LED_INVERT = False
LED_BRIGHTNESS = 64
LED_CHANNEL = 0

# 2_speaker (3.5mm)
SPEAKER_DEVICE = "plughw:1,0"
SPEAKER_SAMPLE_RATE = 44100
SPEAKER_VOLUME = 0.8
SPEAKER_BEEP_INTERVAL = 5.0   # 비프 간격 (초)
SPEAKER_BEEP_DURATION = 0.3
SPEAKER_SCALE_INTERVAL = 60.0  # 음계 테스트 주기 (초)

# 3_mic (USB)
MIC_DEVICE = "plughw:3,0"
MIC_SAMPLE_RATE = 44100
MIC_CHANNELS = 1
MIC_LOG_INTERVAL = 10.0        # 마이크 상태 로그 간격 (초)

# 로그
LOG_DIR = "/data/4_loadtest"
LOG_FILE = os.path.join(LOG_DIR, "load_test.log")
STATUS_INTERVAL = 60.0         # 진행 상황 출력 간격 (초)