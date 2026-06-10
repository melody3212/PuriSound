import sounddevice as sd

try:
    print("=== 사용 가능한 오디오 장치 목록 ===")
    devices = sd.query_devices()
    print(devices)
    
    default_input = sd.query_devices(kind='input')
    print("\n기본 입력 장치 정보:")
    print(default_input)
except Exception as e:
    print(f"디바이스 조회 중 에러 발생: {e}")
