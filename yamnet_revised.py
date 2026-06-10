import os
import sys
import csv
import json
import queue
import time
import threading
from datetime import datetime

import wave

import numpy as np
import sounddevice as sd
import onnxruntime as ort
import matplotlib
matplotlib.use('Agg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

# Windows 콘솔(cp949)에서도 █ ░ ■ ▶ 등 유니코드 문자를 출력할 수 있도록 UTF-8 강제
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# =============================================================================
# 기본 파라미터 (기존 YAMNet 추론 / 오디오 입력 스트림 설정은 그대로 유지)
# =============================================================================
SAMPLE_RATE = 16000      # YAMNet이 요구하는 샘플링 레이트 (16kHz)
WINDOW_DURATION = 0.975  # YAMNet 기본 윈도우 크기 (초)
WINDOW_SIZE = int(SAMPLE_RATE * WINDOW_DURATION)  # 샘플 수 (15600개)
STEP_DURATION = 0.5      # 분석 주기 (0.5초마다 슬라이딩)
STEP_SIZE = int(SAMPLE_RATE * STEP_DURATION)      # 0.5초당 샘플 수 (8000개)

# =============================================================================
# 1. 입력 증폭량 (10x -> 2x)
# =============================================================================
AMPLIFY = 2.0

# =============================================================================
# 2. 신뢰도 임계값 (40% 미만 결과는 무시)
# =============================================================================
CONFIDENCE_MIN = 0.40

# =============================================================================
# 3. dB(SPL) 임계값 (40dB 미만은 정적으로 간주)
# =============================================================================
DB_THRESHOLD = 40.0

# noise_detected 로그/콘솔 출력 throttle (초) — 연속 감지 시 과도한 로그 방지
DETECT_LOG_INTERVAL = 1.0

_BASE           = os.path.dirname(os.path.abspath(__file__))
LOG_FILE        = os.path.join(_BASE, "noise_log.jsonl")
OUTPUT_DIR      = os.path.join(_BASE, "captured_sounds")
CAPTURE_COOLDOWN = 5.0   # 같은 유형 연속 캡처 최소 간격 (초)

# =============================================================================
# 6. 소음 유형 -> 마스킹 사운드 / 프로파일 매핑
#    (5번의 start_sec / stop_sec 규칙을 하나의 프로파일에 병합)
# =============================================================================
NOISE_PROFILES = {
    "발망치_충격음": {"masking": "브라운", "fade_in": 2.0, "fade_out": 3.0, "start_sec": 3, "stop_sec": 20},
    "배관음_드릴":   {"masking": "브라운", "fade_in": 2.0, "fade_out": 3.0, "start_sec": 5, "stop_sec": 15},
    "도로_교통음":   {"masking": "핑크",   "fade_in": 3.0, "fade_out": 4.0, "start_sec": 7, "stop_sec": 8},
    "반려동물_짖음": {"masking": "화이트", "fade_in": 2.0, "fade_out": 3.0, "start_sec": 4, "stop_sec": 25},
    "대화_생활음":   {"masking": "화이트", "fade_in": 2.5, "fade_out": 3.0, "start_sec": 5, "stop_sec": 12},
    "가구_끄는소리": {"masking": "브라운", "fade_in": 1.5, "fade_out": 2.0, "start_sec": 5, "stop_sec": 5},
    "미분류":        {"masking": "화이트", "fade_in": 2.0, "fade_out": 3.0, "start_sec": 5, "stop_sec": 10},
}

# 마스킹 사운드별 짧은 설명 (콘솔 표시용)
MASKING_DESC = {
    "브라운": "진동 상쇄",
    "핑크":   "저주파 완화",
    "화이트": "광대역 차폐",
}


# =============================================================================
# 3. dB(SPL) 계산 — 양수의 실제 음압 레벨을 반환
# =============================================================================
def calculate_db_spl(audio: np.ndarray) -> float:
    REFERENCE = 20e-6  # 인간 청각 기준값 (20 마이크로파스칼)
    rms = np.sqrt(np.mean(audio ** 2))
    if rms == 0:
        return 0.0
    db_spl = 20 * np.log10(rms / REFERENCE)
    return round(db_spl, 1)


# =============================================================================
# 6. YAMNet 라벨 -> 한글 소음 유형 키 매핑
# =============================================================================
# 우선순위 순서대로 키워드를 검사한다. (앞쪽 유형이 더 우선)
_NOISE_KEYWORDS = [
    ("발망치_충격음", ["hammer", "knock", "thump", "thud", "footstep", "stomp",
                       "bang", "impact", "wood", "slam"]),
    ("배관음_드릴",   ["drill", "power tool", "saw", "sawing", "tool",
                       "plumbing", "pipe", "water tap", "sink"]),
    ("도로_교통음",   ["vehicle", "traffic", "car", "truck", "bus", "motorcycle",
                       "engine", "road", "horn", "siren", "motor"]),
    ("반려동물_짖음", ["dog", "bark", "bow-wow", "cat", "meow", "animal",
                       "growl", "howl", "yip"]),
    ("대화_생활음",   ["speech", "conversation", "talk", "narration", "child",
                       "children", "shout", "yell", "television", "music", "song",
                       "laughter", "baby"]),
    ("가구_끄는소리", ["sliding door", "scrape", "drag", "furniture", "chair",
                       "table", "door", "squeak", "scratch"]),
]


def yamnet_to_noise_type(label: str) -> str:
    """원시 YAMNet 라벨 문자열을 한글 소음 유형 키로 매핑한다."""
    if not label:
        return "미분류"
    l = label.lower()
    for noise_type, keywords in _NOISE_KEYWORDS:
        for kw in keywords:
            if kw in l:
                return noise_type
    return "미분류"


# =============================================================================
# 8. 이중 로깅 — JSON 파일 + 콘솔 텍스트를 동시에 처리하는 단일 함수
# =============================================================================
def _clear_status_line():
    """\\r 로 출력 중인 상태바를 지운 뒤 이벤트 메시지를 깔끔히 출력하기 위함"""
    sys.stdout.write("\r" + " " * 100 + "\r")


def _to_python(obj):
    """numpy 스칼라를 Python 기본 타입으로 변환 (JSON 직렬화 대응)"""
    if isinstance(obj, dict):
        return {k: _to_python(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_python(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


def log_event(event_type: str, data: dict):
    # 1) JSON 한 줄을 파일에 append (numpy float32 → Python float 변환)
    entry = {"timestamp": datetime.now().isoformat(), "event": event_type, **_to_python(data)}
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 2) 콘솔에 한글 텍스트 출력 (기존 프로그램 형식 유지)
    _clear_status_line()
    if event_type == "noise_detected":
        desc = MASKING_DESC.get(data.get("masking_noise", ""), "")
        print(
            f"외부소음  | {data['db']:.1f}dB | {data.get('duration_sec', 0.0):.1f}s | "
            f"{data['confidence'] * 100:.1f}% | {data['noise_type']} (외부)  | "
            f"■ {data.get('masking_noise', '')} ({desc})"
        )
        print(f"[층간소음 기록] 외부 소음({data['noise_type']})이 감지되어 저장되었습니다.")
    elif event_type == "masking_start":
        print(f"[마스킹 시작] ▶ {data['masking_noise']} 노이즈 — Fade-in {data['fade_in_sec']}초")
    elif event_type == "masking_stop":
        print(f"[마스킹 종료] ■ 침묵 {data['silence_duration_sec']}초 확인 — Fade-out {data['fade_out_sec']}초")
    sys.stdout.flush()


# =============================================================================
# 9. 상태 관리 클래스
# =============================================================================
class NoiseState:
    def __init__(self):
        self.noise_start_time = None      # 소음이 처음 연속 감지되기 시작한 시각
        self.last_noise_time = None       # 마지막으로 소음이 감지된 시각
        self.is_masking = False           # 현재 마스킹 재생 중 여부
        self.current_noise_type = None    # 현재 추적 중인 소음 유형
        self.current_profile = None       # 현재 소음 유형의 프로파일 dict
        self.silence_timer = None         # 종료(stop) 타이머 (threading.Timer)
        self.silence_start_time = None    # 침묵이 시작된 시각 (상태바 카운트다운용)

    def reset(self):
        self.noise_start_time = None
        self.last_noise_time = None
        self.is_masking = False
        self.current_noise_type = None
        self.current_profile = None
        if self.silence_timer is not None:
            self.silence_timer.cancel()
        self.silence_timer = None
        self.silence_start_time = None


# =============================================================================
# 7. 마스킹 오디오 출력 (Fade-in / Fade-out 포함)
# =============================================================================
class MaskingPlayer:
    """화이트/핑크/브라운 노이즈를 생성해 출력 스트림으로 재생한다.
    볼륨 램프는 numpy.linspace 로 만들고 audio_chunk * volume_ramp 로 적용한다."""

    def __init__(self, samplerate=SAMPLE_RATE, master_gain=0.3, buffer_sec=30):
        self.samplerate = samplerate
        self.master_gain = master_gain

        n = int(samplerate * buffer_sec)
        self.buffers = {
            "화이트": self._make_noise("white", n),
            "핑크":   self._make_noise("pink", n),
            "브라운": self._make_noise("brown", n),
        }
        self.current_buffer = self.buffers["화이트"]
        self.read_pos = 0

        # 볼륨/페이드 상태
        self.volume = 0.0        # 현재 적용 볼륨 (0.0 ~ 1.0)
        self.target_volume = 0.0  # 목표 볼륨
        self.fade_rate = 0.0     # 샘플당 볼륨 변화량 (0 이면 고정)

        self.stream = sd.OutputStream(
            samplerate=samplerate,
            channels=1,
            dtype="float32",
            callback=self._callback,
            blocksize=1024,
        )

    @staticmethod
    def _make_noise(kind: str, n: int) -> np.ndarray:
        """FFT 정형화를 이용한 화이트/핑크/브라운 노이즈 버퍼 생성."""
        white = np.random.randn(n)
        if kind == "white":
            y = white
        else:
            X = np.fft.rfft(white)
            f = np.fft.rfftfreq(n)
            f[0] = f[1]  # DC 0 나눗셈 방지
            if kind == "pink":
                X = X / np.sqrt(f)      # -3 dB/oct
            else:  # brown
                X = X / f               # -6 dB/oct
            y = np.fft.irfft(X, n)
        y = y / (np.max(np.abs(y)) + 1e-9)
        return y.astype(np.float32)

    def _callback(self, outdata, frames, time_info, status):
        if status:
            print(f"출력 스트림 경고: {status}", file=sys.stderr)

        # 노이즈 버퍼에서 frames 만큼 순환 추출
        buf = self.current_buffer
        n = len(buf)
        idx = (self.read_pos + np.arange(frames)) % n
        chunk = buf[idx]
        self.read_pos = (self.read_pos + frames) % n

        # 볼륨 램프 (numpy.linspace) 생성 후 audio_chunk * volume_ramp 적용
        if self.fade_rate > 0.0:
            if self.target_volume > self.volume:
                end = min(self.target_volume, self.volume + self.fade_rate * frames)
            else:
                end = max(self.target_volume, self.volume - self.fade_rate * frames)
        else:
            end = self.volume
        volume_ramp = np.linspace(self.volume, end, frames)
        self.volume = float(end)
        if self.volume == self.target_volume:
            self.fade_rate = 0.0  # 페이드 완료

        out = chunk * volume_ramp * self.master_gain
        outdata[:] = out.reshape(-1, 1).astype(np.float32)

    def start(self, noise_name: str, fade_in: float):
        """마스킹 시작 — fade_in 초에 걸쳐 볼륨을 0 -> 1 로 증가."""
        self.current_buffer = self.buffers.get(noise_name, self.buffers["화이트"])
        self.target_volume = 1.0
        self.fade_rate = 1.0 / max(fade_in * self.samplerate, 1.0)

    def stop(self, fade_out: float):
        """마스킹 종료 — fade_out 초에 걸쳐 볼륨을 1 -> 0 으로 감소."""
        self.target_volume = 0.0
        self.fade_rate = 1.0 / max(fade_out * self.samplerate, 1.0)


# =============================================================================
# 메인 컨트롤러 (기존 RealtimeSoundClassifier 의 추론/입력 로직 유지)
# =============================================================================
class RealtimeSoundClassifier:
    def __init__(self, model_path="yamnet.onnx", class_map_path="yamnet_class_map.csv"):
        print("YAMNet 사운드 마스킹 시스템 초기화 중...")

        # 1. 클래스 매핑 로드
        self.class_names = self.load_class_map(class_map_path)

        # 2. ONNX 런타임 세션 초기화
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 2
        self.session = ort.InferenceSession(model_path, sess_options=opts)

        # 3. 오디오 입력 큐 및 버퍼
        self.audio_queue = queue.Queue()
        self.audio_buffer = np.zeros(WINDOW_SIZE, dtype=np.float32)

        # 4. 상태 / 마스킹 / 동기화
        self.state = NoiseState()
        self.masking = MaskingPlayer(samplerate=SAMPLE_RATE)
        self.lock = threading.RLock()
        self._last_detect_log = 0.0

        # 외부 제어용 (Flask API)
        self._stop_event = threading.Event()
        self.current_db = 0.0
        self.current_confidence = 0.0
        self.current_label = ""

        # 캡처 저장
        self._last_capture = {}   # noise_type -> last save timestamp
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ---- 기존 로직 유지: 클래스맵 로드 ----------------------------------------
    def load_class_map(self, path):
        class_names = []
        if not os.path.exists(path):
            raise FileNotFoundError(f"클래스 매핑 CSV 파일이 존재하지 않습니다: {path}")
        with open(path, mode="r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader)  # 헤더 스킵
            for row in reader:
                if len(row) >= 3:
                    class_names.append(row[2])
        print(f"총 {len(class_names)}개의 소리 카테고리 로드 완료.")
        return class_names

    def stop(self):
        """외부에서 감지 루프를 안전하게 종료한다."""
        self._stop_event.set()

    def masking_play(self, noise_name: str = "화이트", fade_in: float = 1.0):
        """대시보드 수동 마스킹 시작 (run() 실행 중에만 유효)."""
        with self.lock:
            self.masking.start(noise_name, fade_in)
            self.state.is_masking = True
            self.state.current_noise_type = self.state.current_noise_type or "미분류"
            self.state.current_profile = NOISE_PROFILES.get(
                self.state.current_noise_type, NOISE_PROFILES["미분류"]
            )

    def _save_capture(self, noise_type: str, label: str, db: float,
                      confidence: float, audio_snap: np.ndarray):
        """스펙트로그램(PNG) + 오디오(WAV)를 백그라운드 스레드에서 저장."""
        now = time.time()
        if now - self._last_capture.get(noise_type, 0.0) < CAPTURE_COOLDOWN:
            return
        self._last_capture[noise_type] = now

        clean = "".join(c for c in noise_type if c.isalnum() or c in (' ', '_')).strip()
        dir_path = os.path.join(OUTPUT_DIR, clean)
        os.makedirs(dir_path, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"{clean}_{ts}_{int(confidence * 100)}pct"

        # ── PNG 스펙트로그램 (thread-safe OO API) ───────────────────────────
        try:
            fig = Figure(figsize=(7, 4))
            ax  = fig.add_subplot(1, 1, 1)
            _, _, _, im = ax.specgram(audio_snap, Fs=SAMPLE_RATE,
                                      NFFT=512, noverlap=256, cmap='viridis')
            ax.set_title(f"{noise_type} ({label})  {confidence*100:.1f}%  {db:.1f} dB")
            ax.set_xlabel("Time (s)")
            ax.set_ylabel("Frequency (Hz)")
            ax.set_ylim(0, SAMPLE_RATE / 2)
            fig.colorbar(im, ax=ax, label="Intensity (dB)")
            fig.tight_layout()
            FigureCanvasAgg(fig).print_figure(
                os.path.join(dir_path, base + ".png"), dpi=100
            )
        except Exception as e:
            print(f"\n[캡처] PNG 저장 실패: {e}", file=sys.stderr)

        # ── WAV 오디오 ────────────────────────────────────────────────────────
        try:
            pcm16 = (np.clip(audio_snap, -1.0, 1.0) * 32767).astype(np.int16)
            wav_path = os.path.join(dir_path, base + ".wav")
            with wave.open(wav_path, 'w') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm16.tobytes())
        except Exception as e:
            print(f"\n[캡처] WAV 저장 실패: {e}", file=sys.stderr)

        _clear_status_line()
        print(f"[캡처 저장] {noise_type} | {ts} | {confidence*100:.1f}%")

    def masking_stop_manual(self, fade_out: float = 1.0):
        """대시보드 수동 마스킹 정지."""
        with self.lock:
            if self.state.silence_timer is not None:
                self.state.silence_timer.cancel()
            self.masking.stop(fade_out)
            self.state.reset()

    # ---- 기존 로직 유지: 오디오 입력 콜백 -------------------------------------
    def audio_callback(self, indata, frames, time_info, status):
        if status:
            print(f"오디오 스트림 경고: {status}", file=sys.stderr)
        self.audio_queue.put(indata.copy().flatten())

    # =========================================================================
    # 4/5. 시작 / 종료 감지 규칙 처리
    # =========================================================================
    def handle_detection(self, is_noise, noise_type, label, db, confidence):
        now = time.time()
        st = self.state

        if is_noise:
            st.last_noise_time = now

            if st.is_masking:
                # 마스킹 중 소음이 다시 감지되면 종료 타이머를 리셋(취소)
                if st.silence_timer is not None:
                    st.silence_timer.cancel()
                    st.silence_timer = None
                    st.silence_start_time = None
            else:
                # 새 소음이거나 유형이 바뀌면 시작 타이머를 리셋
                if st.noise_start_time is None or st.current_noise_type != noise_type:
                    st.noise_start_time = now
                    st.current_noise_type = noise_type
                    st.current_profile = NOISE_PROFILES[noise_type]
                    self._last_detect_log = 0.0  # 유형 변경 즉시 로그 1회

                # 2. throttle 적용 noise_detected 로깅 + 캡처 저장
                if now - self._last_detect_log >= DETECT_LOG_INTERVAL:
                    log_event("noise_detected", {
                        "noise_type": noise_type,
                        "yamnet_label": label,
                        "db": db,
                        "confidence": round(float(confidence), 3),
                        "masking_noise": st.current_profile["masking"],
                        "duration_sec": round(now - st.noise_start_time, 1),
                    })
                    self._last_detect_log = now
                    audio_snap = self.audio_buffer.copy()
                    threading.Thread(
                        target=self._save_capture,
                        args=(noise_type, label, float(db), float(confidence), audio_snap),
                        daemon=True,
                    ).start()

                # 4. start_sec 이상 연속 지속 시 마스킹 시작
                if now - st.noise_start_time >= st.current_profile["start_sec"]:
                    self._start_masking()
        else:
            # 침묵 프레임
            if st.is_masking:
                # 5. 침묵이 시작되면 종료 타이머를 가동 (stop_sec 후 종료)
                if st.silence_timer is None:
                    st.silence_start_time = now
                    self._start_silence_timer()
            else:
                # 마스킹 전 단계에서 침묵이 오면 시작 타이머 리셋
                if st.noise_start_time is not None:
                    st.reset()

    def _start_masking(self):
        st = self.state
        profile = st.current_profile
        masking_noise = profile["masking"]
        st.is_masking = True
        st.silence_start_time = None

        self.masking.start(masking_noise, profile["fade_in"])
        log_event("masking_start", {
            "noise_type": st.current_noise_type,
            "masking_noise": masking_noise,
            "fade_in_sec": profile["fade_in"],
        })

    def _start_silence_timer(self):
        st = self.state
        # 항상 기존 타이머를 취소한 뒤 새 타이머 시작
        if st.silence_timer is not None:
            st.silence_timer.cancel()
        stop_sec = st.current_profile["stop_sec"]
        timer = threading.Timer(stop_sec, self._on_silence_timeout)
        timer.daemon = True
        st.silence_timer = timer
        timer.start()

    def _on_silence_timeout(self):
        with self.lock:
            st = self.state
            if not st.is_masking or st.current_profile is None:
                return
            profile = st.current_profile
            noise_type = st.current_noise_type
            stop_sec = profile["stop_sec"]
            fade_out = profile["fade_out"]

            self.masking.stop(fade_out)
            log_event("masking_stop", {
                "noise_type": noise_type,
                "silence_duration_sec": float(stop_sec),
                "fade_out_sec": fade_out,
            })
            st.reset()

    # =========================================================================
    # 10. 실시간 상태바 출력 (\r, 10칸 진행바)
    # =========================================================================
    def print_status(self):
        with self.lock:
            st = self.state
            now = time.time()

            if st.is_masking and st.current_profile is not None:
                stop_sec = st.current_profile["stop_sec"]
                if st.silence_start_time is not None:
                    elapsed = now - st.silence_start_time
                    remaining = max(0.0, stop_sec - elapsed)
                    frac = min(max(elapsed / stop_sec, 0.0), 1.0)
                    text = f"침묵 감지 중 — 종료까지 {remaining:.1f}초"
                else:
                    frac = 1.0
                    text = f"마스킹 재생 중 — 종료까지 {stop_sec:.1f}초"
            elif st.noise_start_time is not None and st.current_profile is not None:
                start_sec = st.current_profile["start_sec"]
                elapsed = now - st.noise_start_time
                remaining = max(0.0, start_sec - elapsed)
                frac = min(max(elapsed / start_sec, 0.0), 1.0)
                text = f"소음 감지 중 — 마스킹까지 {remaining:.1f}초"
            else:
                frac = 0.0
                text = "대기 중 — 소음 없음"

        filled = int(frac * 10)
        bar = "█" * filled + "░" * (10 - filled)
        sys.stdout.write(f"\r[{bar}] {text}    ")
        sys.stdout.flush()

    # =========================================================================
    # 메인 루프 (기존 YAMNet 추론 + 오디오 입력 스트림 유지)
    # =========================================================================
    def run(self):
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self.audio_callback,
            blocksize=STEP_SIZE,
        )

        print("\n=== 실시간 소음 감지 & 적응형 사운드 마스킹 시작 ===")
        print(f" * 입력 증폭: {AMPLIFY}x | 신뢰도 임계: {CONFIDENCE_MIN*100:.0f}% | dB 임계: {DB_THRESHOLD:.0f} dB(SPL)")
        print(f" * 이벤트 로그 파일: {LOG_FILE}")
        print(" * 종료하려면 'Ctrl + C'를 누르세요.\n")

        with stream, self.masking.stream:
            try:
                while not self._stop_event.is_set():
                    new_data = self.audio_queue.get()

                    # 1. 입력 증폭 (2x)
                    new_data = new_data * AMPLIFY

                    # 슬라이딩 버퍼 갱신 (기존 로직 유지)
                    self.audio_buffer = np.roll(self.audio_buffer, -len(new_data))
                    self.audio_buffer[-len(new_data):] = new_data

                    # 3. dB(SPL) 계산 — 현재 블록(0.5초)만 사용
                    # 전체 버퍼(0.975초) 평균을 쓰면 충격음·짖음 같은
                    # 순간 소음의 에너지가 희석되어 10dB 이상 낮게 측정됨
                    db = calculate_db_spl(new_data)

                    # === 기존 YAMNet 추론 로직 유지 ===
                    ort_inputs = {"waveform": self.audio_buffer}
                    outputs = self.session.run(["output_0"], ort_inputs)
                    scores = outputs[0]
                    mean_scores = np.mean(scores, axis=0)

                    top_idx = int(np.argmax(mean_scores))
                    confidence = float(mean_scores[top_idx])
                    label = self.class_names[top_idx] if top_idx < len(self.class_names) else ""
                    noise_type = yamnet_to_noise_type(label)

                    # 2/3. 신뢰도 40% 미만 또는 dB 임계 미만이면 소음 아님(=침묵)으로 처리
                    is_noise = (db >= DB_THRESHOLD) and (confidence >= CONFIDENCE_MIN)

                    with self.lock:
                        self.current_db = float(db)
                        self.current_confidence = float(confidence)
                        self.current_label = label
                        self.handle_detection(is_noise, noise_type, label, db, confidence)

                    # 10. 실시간 상태바
                    self.print_status()

            except KeyboardInterrupt:
                self._stop_event.set()
            finally:
                with self.lock:
                    self.state.reset()
                print("\n\n감지 시스템이 중지되었습니다.")


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
