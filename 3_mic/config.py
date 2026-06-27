"""USB 마이크 테스트 설정."""

# arecord -l 기준 card 3 = AB13X USB Audio
ALSA_DEVICE = "plughw:3,0"

SAMPLE_RATE = 44100
CHANNELS = 1
RECORD_SECONDS = 3

# 녹음 재생 확인용 (2_speaker 3.5mm 출력)
PLAYBACK_DEVICE = "plughw:1,0"
PLAYBACK_VOLUME = 0.8   # 재생 시 목표 음량 (0.0 ~ 1.0, 2_speaker와 동일)