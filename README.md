# LG PuriSound — 실시간 소음 감지 & 적응형 사운드 마스킹 시스템

> LG DX School DX 프로젝트  
> 온디바이스 AI 기반 층간소음 자동 감지 및 마스킹 솔루션

---

## 목차

1. [시스템 개요](#1-시스템-개요)
2. [아키텍처](#2-아키텍처)
3. [소음 감지 파이프라인](#3-소음-감지-파이프라인)
4. [스펙트럼 자동 매칭 알고리즘](#4-스펙트럼-자동-매칭-알고리즘)
5. [마스킹 사운드 시스템](#5-마스킹-사운드-시스템)
6. [Flask REST API](#6-flask-rest-api)
7. [대시보드 기능](#7-대시보드-기능)
8. [CMD 모니터](#8-cmd-모니터)
9. [설치 및 실행](#9-설치-및-실행)
10. [파일 구조](#10-파일-구조)

---

## 1. 시스템 개요

마이크로 입력된 오디오를 **YAMNet(ONNX)** 으로 실시간 분류하고,  
층간소음이 감지되면 **최적 마스킹 사운드를 자동 선택·재생**하는 온디바이스 AI 시스템입니다.

```
마이크 입력
    │
    ▼
┌─────────────────────────────────────────┐
│  오디오 슬라이딩 버퍼 (0.975s / 16kHz)  │
└───────────────────┬─────────────────────┘
                    │
          ┌─────────┴──────────┐
          ▼                    ▼
   YAMNet 추론            dB(SPL) 계산
   (ONNX Runtime)         (0.5s 블록 RMS)
          │                    │
          └─────────┬──────────┘
                    ▼
            소음 판단 로직
        (신뢰도 >= 40% AND dB >= 40)
                    │
         ┌──────────┴──────────┐
         ▼                     ▼
   소음 아님 → 침묵         소음 감지
   타이머 가동              유형 매핑
                             │
                    스펙트럼 FFT 분석
                             │
                    최적 마스커 자동 선택
                    (코사인 유사도)
                             │
                    Fade-in 마스킹 재생
```

### 핵심 특징

| 항목 | 내용 |
|------|------|
| 추론 방식 | 온디바이스 (클라우드 불필요) |
| AI 모델 | Google YAMNet (ONNX 변환, 521개 오디오 클래스) |
| 감지 주기 | 0.5초 슬라이딩 윈도우 |
| 제어 방식 | Flask REST API + SSE 실시간 스트리밍 |
| 권장 하드웨어 | Raspberry Pi 4 (2GB RAM 이상) |

---

## 2. 아키텍처

```
┌──────────────────────────────────────────────────────────┐
│                    app.py (Flask)                        │
│  REST API  │  SSE Stream  │  Static 파일 서빙            │
└──────────────────────┬───────────────────────────────────┘
                       │  Thread
                       ▼
┌──────────────────────────────────────────────────────────┐
│            yamnet_revised.py                             │
│                                                          │
│  RealtimeSoundClassifier                                 │
│  ├── ONNX Runtime (YAMNet)    <- yamnet.onnx             │
│  ├── sounddevice InputStream  <- 마이크 입력              │
│  ├── NoiseState               <- 상태 머신                │
│  └── MaskingPlayer                                       │
│       ├── 내장 노이즈 생성 (브라운/핑크/화이트)           │
│       ├── MP3/WAV 파일 로드   <- masking_sounds/         │
│       ├── 스펙트럼 분석 캐시  <- _spectra dict           │
│       └── sounddevice OutputStream <- 스피커 출력        │
└──────────────────────────────────────────────────────────┘
                       │
              ┌────────┴────────┐
              ▼                 ▼
     monitor.py           templates/
  (CMD 실시간 모니터)      index.html
                          (대시보드)
```

### 주요 모듈

| 파일 | 역할 |
|------|------|
| `yamnet_revised.py` | 핵심 감지·마스킹 엔진 |
| `app.py` | Flask REST API 서버 |
| `templates/index.html` | 웹 대시보드 (SPA) |
| `monitor.py` | CMD 실시간 모니터링 클라이언트 |
| `run_server.py` | 서버 실행 래퍼 (PATH 환경 포함) |

---

## 3. 소음 감지 파이프라인

### 3-1. 오디오 입력

```python
SAMPLE_RATE     = 16000   # YAMNet 요구 샘플레이트
WINDOW_DURATION = 0.975   # YAMNet 입력 윈도우 (15,600 샘플)
STEP_DURATION   = 0.5     # 슬라이딩 스텝 (8,000 샘플)
```

- sounddevice `InputStream` 콜백 → 0.5초마다 새 블록 수신
- 슬라이딩 버퍼: 새 블록을 우측에 append, 오래된 블록 제거

### 3-2. dB(SPL) 계산

```
dB_SPL = 20 x log10(RMS / 20x10^-6)
```

- **전체 버퍼(0.975s) 평균이 아닌 현재 블록(0.5s)만 사용**
- 이유: 개 짖는 소리·충격음 같은 순간 소음은 전체 평균 시 약 10dB 낮게 측정됨

### 3-3. YAMNet 추론

- ONNX Runtime으로 521개 클래스 점수 계산
- 상위 1개 라벨 + 신뢰도 추출

### 3-4. 소음 판단 조건

```python
is_noise = (db >= DB_THRESHOLD) and (confidence >= CONFIDENCE_MIN)
# 기본값: 40 dB(SPL) AND 40%
```

### 3-5. YAMNet 라벨 → 소음 유형 매핑

| 소음 유형 | 매핑 키워드 (일부) |
|-----------|-------------------|
| 발망치_충격음 | hammer, knock, thump, bang, impact |
| 배관음_드릴 | drill, power tool, saw, plumbing |
| 도로_교통음 | vehicle, traffic, car, engine, siren |
| 반려동물_짖음 | dog, bark, cat, meow, growl |
| 대화_생활음 | speech, conversation, child, music |
| 가구_끄는소리 | sliding door, scrape, furniture |

### 3-6. 상태 머신 (NoiseState)

```
[대기]
  │ 소음 감지 (is_noise=True)
  ▼
[소음 축적 중]  <- start_sec 동안 지속 감지
  │ start_sec 경과
  ▼
[마스킹 재생] ──────────────────────────────┐
  │ 소음 사라짐 (is_noise=False)            │
  ▼                                         │
[침묵 타이머 가동]  <- stop_sec 카운트다운  │
  │ 소음 재감지 → 타이머 리셋 ──────────────┘
  │ stop_sec 경과
  ▼
[마스킹 종료 (Fade-out)] → [대기]
```

---

## 4. 스펙트럼 자동 매칭 알고리즘

`masking_sounds/` 폴더에 MP3/WAV 파일을 넣으면, 소음 감지 시 **가장 효과적인 마스킹 파일을 자동으로 선택**합니다.

### 4-1. 주파수 대역 분류

오디오를 6개의 로그 스케일 주파수 대역으로 분해합니다.

| 대역 | 주파수 범위 | 대역명 | 해당 소리 예시 |
|------|------------|--------|---------------|
| 0 | 50 – 160 Hz | 저음 | 발망치 진동, 배관 충격 |
| 1 | 160 – 400 Hz | 하중음 | 드릴, 중저음 교통 |
| 2 | 400 – 1,000 Hz | 중음 | 목소리 기본 주파수 |
| 3 | 1,000 – 2,500 Hz | 상중음 | 목소리 배음, 드릴 고조파 |
| 4 | 2,500 – 5,500 Hz | 고음 | 개 짖는 소리, 악기 |
| 5 | 5,500 – 8,000 Hz | 초고음 | 찰칵, 고음 마찰음 |

### 4-2. 스펙트럼 프로파일 계산

```python
def _band_energies(audio, sr=16000):
    window = np.hanning(len(audio))
    power  = abs(FFT(audio * window)) ** 2          # FFT 파워 스펙트럼

    for i in range(6):
        bands[i] = sqrt(mean(power[freq_lo:freq_hi]))  # 대역별 RMS

    return bands / bands.sum()   # 합이 1이 되도록 정규화
```

### 4-3. 코사인 유사도 비교

```
similarity(noise, masker) = (noise · masker) / (||noise|| x ||masker||)
```

- 값이 1에 가까울수록 두 소리의 주파수 분포가 유사
- **심리음향 원리**: 마스킹은 소음과 같은 주파수 대역에 에너지가 있을 때 효과적

### 4-4. 자동 선택 흐름

```
마스킹 시작 트리거
       │
masking_sounds/ 에 파일 존재?
       │YES                     │NO
       ▼                        ▼
현재 오디오 버퍼           프로파일 설정값
FFT 스펙트럼 계산          (브라운/핑크/화이트)
       │                   사용 (기존 방식)
모든 후보 비교
(내장 3종 + 사용자 파일 전체)
       │
코사인 유사도 최대값 선택
       │
선택된 파일로 Fade-in 재생
```

### 4-5. 캐싱 전략

- 파일 최초 로드 시: ffmpeg subprocess → float32 PCM 변환 → 메모리 캐시 (`_file_cache`)
- 스펙트럼 프로파일: 파일별 6차원 벡터 사전 계산 → `_spectra` dict 캐시
- 이후 동일 파일 재생 시: 캐시 즉시 반환 (재디코딩 없음)

---

## 5. 마스킹 사운드 시스템

### 5-1. 내장 생성 노이즈

FFT 정형화(Spectral Shaping) 방식으로 30초 버퍼를 사전 생성합니다.

| 종류 | 스펙트럼 특성 | 주 용도 |
|------|--------------|--------|
| 브라운 노이즈 | -6 dB/octave | 발망치, 저주파 충격 |
| 핑크 노이즈 | -3 dB/octave | 교통소음, 중저음 |
| 화이트 노이즈 | 평탄 (0 dB/octave) | 광대역 차폐 |

```python
X = FFT(white_noise)
f = rfftfreq(n); f[0] = f[1]   # DC 0 나눗셈 방지
X_brown = X / f                 # -6 dB/oct
X_pink  = X / sqrt(f)           # -3 dB/oct
```

### 5-2. 사용자 MP3/WAV 파일

- 위치: `masking_sounds/` 폴더
- 지원 포맷: `.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`
- 로드 순서:
  1. `soundfile` 시도 (WAV/OGG/FLAC — 빠름)
  2. `ffmpeg subprocess` 폴백 (MP3 포함 전 포맷, Python 3.13+ 호환)
- 전처리: 모노 변환 → 16kHz 리샘플링 → 피크 정규화 → **최대 30초만 디코딩**

### 5-3. Fade-in / Fade-out

sounddevice 콜백 내에서 샘플 단위 볼륨 램프를 적용해 클릭 노이즈 없이 전환합니다.

```python
volume_ramp = np.linspace(start_vol, end_vol, frames)
output = audio_chunk * volume_ramp * master_gain
```

### 5-4. 소음 유형별 프로파일

```python
NOISE_PROFILES = {
    "발망치_충격음": {"masking": "브라운", "fade_in": 2.0, "fade_out": 3.0, "start_sec": 3,  "stop_sec": 20},
    "배관음_드릴":   {"masking": "브라운", "fade_in": 2.0, "fade_out": 3.0, "start_sec": 5,  "stop_sec": 15},
    "도로_교통음":   {"masking": "핑크",   "fade_in": 3.0, "fade_out": 4.0, "start_sec": 7,  "stop_sec": 8 },
    "반려동물_짖음": {"masking": "화이트", "fade_in": 2.0, "fade_out": 3.0, "start_sec": 4,  "stop_sec": 25},
    "대화_생활음":   {"masking": "화이트", "fade_in": 2.5, "fade_out": 3.0, "start_sec": 5,  "stop_sec": 12},
    "가구_끄는소리": {"masking": "브라운", "fade_in": 1.5, "fade_out": 2.0, "start_sec": 5,  "stop_sec": 5 },
}
# masking_sounds/에 파일이 있으면 masking 필드는 fallback으로만 사용됨
```

---

## 6. Flask REST API

서버 포트: **5000**

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/` | 대시보드 HTML |
| GET | `/api/status` | 현재 상태 전체 (dB, 신뢰도, 라벨, 마스킹, 매칭 점수) |
| POST | `/api/start` | 감지 시스템 시작 |
| POST | `/api/stop` | 감지 시스템 중지 |
| POST | `/api/config` | 파라미터 변경 (증폭/신뢰도/dB 임계값) |
| GET | `/api/profiles` | 소음 유형별 프로파일 조회 |
| POST | `/api/profiles` | 프로파일 업데이트 |
| POST | `/api/masking/play` | 수동 마스킹 시작 (내장 노이즈 또는 파일명) |
| POST | `/api/masking/stop` | 수동 마스킹 중지 |
| GET | `/api/masking/files` | masking_sounds/ 오디오 파일 목록 |
| GET | `/api/masking/analyze` | 파일별 6대역 스펙트럼 분석 결과 |
| GET | `/api/captures` | 캡처 미디어 목록 (PNG + WAV, 최신 30건) |
| GET | `/captures/<path>` | 캡처 파일 서빙 |
| GET | `/api/logs` | 이벤트 로그 (최근 50건, JSONL) |
| GET | `/api/stream` | SSE 실시간 스트림 (500ms 간격) |

### SSE 페이로드 예시

```json
{
  "running": true,
  "db": 52.3,
  "confidence": 61.2,
  "label": "Dog",
  "noise_type": "반려동물_짖음",
  "is_masking": true,
  "match_scores": {
    "danevaer-low-pink-noise-434732.mp3": 0.912,
    "화이트": 0.843,
    "핑크": 0.801
  }
}
```

---

## 7. 대시보드 기능

`http://<서버IP>:5000` 접속

| 패널 | 기능 |
|------|------|
| 시스템 제어 | 감지 시작/중지, 수동 마스킹 (브라운/핑크/화이트/파일 선택) |
| 실시간 모니터링 | dB 바, 신뢰도 바, YAMNet 라벨, 소음 유형, 마스킹 배지 |
| 감지 파라미터 | 증폭(0.5–10×), 신뢰도 임계값, dB 하한선 슬라이더 |
| 소음 프로파일 | 유형별 마스킹 사운드·Fade-in·Fade-out·판단 기준 편집 |
| 마스킹 파일 분석 | 파일별 6대역 스펙트럼 막대 그래프, 주 대역, 재생 시간 |
| 자동 선택 결과 | 최근 마스킹 시 선택된 파일과 코사인 유사도 점수 표시 |
| 캡처 미디어 | 소음 감지 시 자동 저장된 스펙트로그램(PNG) + 오디오(WAV) |
| 이벤트 로그 | 5초 자동 갱신, noise_detected / masking_start / masking_stop |

---

## 8. CMD 모니터

서버와 별도 터미널에서 실시간 현황을 확인합니다.

```bash
# 로컬 서버 모니터링
python monitor.py

# 원격 서버 (라즈베리파이 등)
python monitor.py 192.168.0.91
```

출력 예시:
```
 PuriSound CMD 모니터  →  http://127.0.0.1:5000

  [████████░░░░░░░░░░░░]  52.3dB  ▓▓▓▓▓▓░░░░  61.2%  Dog  반려동물_짖음  ▶ danevaer-low-pink (91%)

  19:21:05  [소음감지]  반려동물_짖음  52.3dB
  19:21:09  [마스킹 시작]  ★ danevaer-low-pink-noise-434732.mp3  <- 반려동물_짖음
```

- `★` = 스펙트럼 자동 매칭으로 선택된 파일
- 괄호 `%` = 코사인 유사도 점수

---

## 9. 설치 및 실행

### 패키지 설치

```bash
pip install flask onnxruntime sounddevice numpy soundfile matplotlib
```

### ffmpeg 설치 (MP3 지원, Python 3.13+)

```bash
# Windows
winget install Gyan.FFmpeg

# Raspberry Pi / Ubuntu
sudo apt install ffmpeg
```

### 실행

```bash
# CMD 창 1: 서버
python app.py

# CMD 창 2: 실시간 모니터 (선택)
python monitor.py
```

### masking_sounds/ 폴더 구성

```
masking_sounds/
├── rain.mp3        # 빗소리 → 저음 지배 → 발망치 충격음 매칭
├── fan.wav         # 팬 소음 → 중음 지배 → 대화 소음 매칭
└── ocean.mp3       # 파도   → 하중음 지배 → 교통 소음 매칭
```

파일 추가 후 대시보드의 **"분석 새로고침"** 버튼을 누르면 스펙트럼 분석 결과가 표시되고, 다음 마스킹부터 자동 적용됩니다.

---

## 10. 파일 구조

```
YAMNET/
├── app.py                  # Flask REST API 서버
├── yamnet_revised.py       # 핵심 감지·마스킹 엔진
├── monitor.py              # CMD 실시간 모니터 클라이언트
├── run_server.py           # 서버 실행 래퍼 (Windows PATH 포함)
├── yamnet.onnx             # YAMNet 모델 (gitignore)
├── yamnet_class_map.csv    # 521개 클래스 매핑 (gitignore)
├── masking_sounds/         # 사용자 마스킹 오디오 (gitignore)
├── captured_sounds/        # 감지 시 자동 저장 PNG+WAV (gitignore)
├── noise_log.jsonl         # 이벤트 로그 (gitignore)
└── templates/
    └── index.html          # 대시보드 SPA (의존성 없는 Vanilla JS)
```

---

## 기술 스택

| 구분 | 기술 |
|------|------|
| AI 추론 | ONNX Runtime + Google YAMNet (521 클래스) |
| 오디오 I/O | sounddevice (PortAudio 기반) |
| 신호 처리 | NumPy FFT, Hann windowing, 코사인 유사도 |
| 서버 | Flask (REST API + Server-Sent Events) |
| 프론트엔드 | Vanilla JS + CSS Grid (외부 의존성 없음) |
| 오디오 디코딩 | soundfile (WAV/OGG) + ffmpeg subprocess (MP3) |
| 시각화 | Matplotlib OO API (스펙트로그램 PNG, thread-safe) |
| 타겟 플랫폼 | Raspberry Pi 4 (온디바이스) / Windows 10+ (개발) |
