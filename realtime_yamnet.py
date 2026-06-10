import os
import sys
import csv
import queue
import time
from datetime import datetime
import numpy as np
import sounddevice as sd
import onnxruntime as ort

# Matplotlib가 GUI 창을 띄우지 않고 백그라운드에서만 이미지를 저장할 수 있도록 설정
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# 파라미터 설정
SAMPLE_RATE = 16000      # YAMNet이 요구하는 샘플링 레이트 (16kHz)
WINDOW_DURATION = 0.975  # YAMNet의 기본 윈도우 크기 (초 단위, 약 0.975초)
WINDOW_SIZE = int(SAMPLE_RATE * WINDOW_DURATION)  # 샘플 수 (15600개)
STEP_DURATION = 0.5      # 분석 주기 (초 단위, 0.5초마다 슬라이딩)
STEP_SIZE = int(SAMPLE_RATE * STEP_DURATION)      # 0.5초당 샘플 수 (8000개)
SCORE_THRESHOLD = 0.20   # 소리 분류 신뢰도 임계값 (스펙트로그램 저장용 및 출력용)
NOISE_DB_THRESHOLD = -40 # 소음 감지 데시벨(dB) 임계값
CAPTURE_COOLDOWN = 3.0   # 동일/신규 감지 후 다음 캡처까지 대기 시간 (초 단위)
OUTPUT_DIR = "captured_sounds" # 스펙트로그램 이미지가 저장될 기본 폴더

class RealtimeSoundClassifier:
    def __init__(self, model_path="yamnet.onnx", class_map_path="yamnet_class_map.csv"):
        print("YAMNet 실시간 감지 & 주파수 분류 시스템 초기화 중...")
        
        # 1. 클래스 매핑 로드
        self.class_names = self.load_class_map(class_map_path)
        
        # 2. ONNX 런타임 세션 초기화
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 2
        self.session = ort.InferenceSession(model_path, sess_options=opts)
        
        # 3. 오디오 입력을 위한 큐 및 버퍼 설정
        self.audio_queue = queue.Queue()
        self.audio_buffer = np.zeros(WINDOW_SIZE, dtype=np.float32)
        
        # 4. 저장 쿨다운 및 디렉토리 상태
        self.last_capture_time = 0.0
        if not os.path.exists(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR)
            print(f"이미지 저장 폴더 생성됨: {OUTPUT_DIR}")
        
    def load_class_map(self, path):
        class_names = []
        if not os.path.exists(path):
            raise FileNotFoundError(f"클래스 매핑 CSV 파일이 존재하지 않습니다: {path}")
            
        with open(path, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader) # 헤더 스킵
            for row in reader:
                if len(row) >= 3:
                    class_names.append(row[2])
        print(f"총 {len(class_names)}개의 소리 카테고리 로드 완료.")
        return class_names

    def audio_callback(self, indata, frames, time_info, status):
        """sounddevice 입력 스트림에서 호출되는 콜백 함수"""
        if status:
            print(f"오디오 스트림 경고: {status}", file=sys.stderr)
        self.audio_queue.put(indata.copy().flatten())

    def calculate_db(self, audio_data):
        """오디오 데이터의 RMS 값을 계산하여 상대적인 데시벨(dB) 수치 반환"""
        rms = np.sqrt(np.mean(audio_data**2) + 1e-9)
        db = 20 * np.log10(rms)
        return db

    def draw_db_bar(self, db, threshold):
        """콘솔에 실시간 소음 수준을 표시할 미터 바 생성"""
        min_db, max_db = -60.0, 0.0
        val = np.clip(db, min_db, max_db)
        percent = (val - min_db) / (max_db - min_db)
        bar_len = int(percent * 20)
        
        thresh_percent = (threshold - min_db) / (max_db - min_db)
        thresh_pos = int(thresh_percent * 20)
        
        bar = []
        for i in range(20):
            if i == thresh_pos:
                bar.append("|")
            elif i < bar_len:
                bar.append("#" if db > threshold else "=")
            else:
                bar.append(" ")
        
        return "".join(bar)

    def save_spectrogram(self, audio_data, sound_name, score):
        """1초간의 오디오 데이터를 주파수 스펙트로그램 이미지로 변환하여 저장"""
        current_time = time.time()
        # 쿨다운 상태 검사 (너무 조밀하게 많은 이미지가 쌓이는 것을 방지)
        if current_time - self.last_capture_time < CAPTURE_COOLDOWN:
            return False
            
        self.last_capture_time = current_time
        
        # 파일 저장 경로 설정 (폴더명 = 소리 종류 display_name)
        # 소리 이름에서 파일 시스템 비허용 특수문자 제거
        clean_sound_name = "".join([c for c in sound_name if c.isalpha() or c in (" ", "_", "-")]).strip()
        category_dir = os.path.join(OUTPUT_DIR, clean_sound_name)
        if not os.path.exists(category_dir):
            os.makedirs(category_dir)
            
        # 파일 이름 포맷: 소리명_날짜_정확도.png
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{clean_sound_name}_{timestamp}_{int(score*100)}percent.png"
        filepath = os.path.join(category_dir, filename)
        
        try:
            plt.figure(figsize=(7, 4.5))
            # 16kHz 오디오용 스펙트로그램 생성
            plt.specgram(audio_data, Fs=SAMPLE_RATE, NFFT=512, noverlap=256, cmap='viridis')
            
            plt.title(f"Spectrogram - {sound_name} ({score*100:.1f}%)")
            plt.xlabel("Time (seconds)")
            plt.ylabel("Frequency (Hz)")
            plt.ylim(0, SAMPLE_RATE / 2) # 나이퀴스트 주파수(8kHz)까지 제한
            plt.colorbar(label="Intensity (dB)")
            plt.tight_layout()
            
            plt.savefig(filepath, dpi=100)
            plt.close()
            
            # 콘솔 라인 지우고 새 이미지 저장 정보 출력
            sys.stdout.write("\n" + " " * 120 + "\r")
            print(f"📸 [스펙트로그램 저장됨] 소리종류: {sound_name:<15} | 경로: {filepath}")
            return True
        except Exception as e:
            print(f"\n스펙트로그램 이미지 저장 실패: {e}")
            plt.close()
            return False

    def run(self):
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype='float32',
            callback=self.audio_callback,
            blocksize=STEP_SIZE
        )
        
        print("\n=== 실시간 소음 주파수 분석 및 폴더 자동 분류 시작 ===")
        print(" * 마이크 입력을 분석하여 소음을 감지하고 스펙트로그램 이미지를 생성합니다.")
        print(f" * 소음 감지 기준: {NOISE_DB_THRESHOLD:.1f} dB | 저장 쿨다운: {CAPTURE_COOLDOWN}초")
        print(f" * 이미지 자동 분류 저장 위치: C:\\Users\\1552\\Desktop\\YAMNET\\{OUTPUT_DIR}\\<소리종류>\\")
        print(" * 종료하려면 'Ctrl + C'를 누르세요.\n")
        
        with stream:
            try:
                while True:
                    new_data = self.audio_queue.get()
                    
                    self.audio_buffer = np.roll(self.audio_buffer, -len(new_data))
                    self.audio_buffer[-len(new_data):] = new_data
                    
                    # 1. 상대 데시벨 계산
                    db = self.calculate_db(self.audio_buffer)
                    db_bar = self.draw_db_bar(db, NOISE_DB_THRESHOLD)
                    
                    # 2. YAMNet 추론
                    ort_inputs = {"waveform": self.audio_buffer}
                    outputs = self.session.run(["output_0"], ort_inputs)
                    scores = outputs[0]
                    mean_scores = np.mean(scores, axis=0)
                    
                    # 3. 소리 분석 및 텍스트 갱신
                    top_indices = np.argsort(mean_scores)[::-1][:3]
                    detected_sounds = []
                    
                    primary_sound = None
                    primary_score = 0.0
                    
                    for i, idx in enumerate(top_indices):
                        score = mean_scores[idx]
                        if score >= SCORE_THRESHOLD:
                            sound_name = self.class_names[idx]
                            detected_sounds.append(f"{sound_name}({score*100:.1f}%)")
                            
                            # 최상위 소리를 캡처 및 시각화 대상으로 선정
                            if i == 0:
                                primary_sound = sound_name
                                primary_score = score
                    
                    sounds_str = ", ".join(detected_sounds) if detected_sounds else "조용함/기타"
                    
                    # 4. 소음 감지 및 이미지 저장 조건 체크
                    # 데시벨 임계값을 초과하고, 신뢰성 있는 소리 종류가 매핑되었을 때
                    noise_status = "[ 정상 상태 ]"
                    if db > NOISE_DB_THRESHOLD:
                        noise_status = "[소음 감지!]"
                        if primary_sound and primary_sound not in ("Silence", "Background noise"):
                            # 스펙트로그램 저장 호출
                            self.save_spectrogram(self.audio_buffer, primary_sound, primary_score)
                    
                    # 한 줄 실시간 모니터 출력
                    sys.stdout.write(
                        f"\r{noise_status} {db:6.1f} dB | {db_bar} | 감지 소리: {sounds_str:<55}"
                    )
                    sys.stdout.flush()
                    
            except KeyboardInterrupt:
                print("\n\n사용자에 의해 실시간 감지가 중지되었습니다.")

if __name__ == "__main__":
    if not os.path.exists("yamnet.onnx") or not os.path.exists("yamnet_class_map.csv"):
        print("에러: 모델 파일(yamnet.onnx) 또는 클래스 맵 파일(yamnet_class_map.csv)이 존재하지 않습니다.")
        print("먼저 download_assets.py 스크립트를 실행하여 모델을 다운로드하세요.")
        sys.exit(1)
        
    try:
        classifier = RealtimeSoundClassifier()
        classifier.run()
    except Exception as e:
        print(f"\n시스템 오류 발생: {e}")
