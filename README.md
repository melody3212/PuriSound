# PuriSound — 데이터 분석 & 서비스 기획 파이프라인

> VOC 수집 ️ NLP 데이터 분석 (임베딩, 클러스터링, IPA) ️ 서비스 기획으로 이어지는 프로젝트의 데이터 전과정 통합 저장소입니다.

---

## 폴더 구조 (data_analyze/)
*   notebooks/crawling/: 각 플랫폼별 크롤러 소스 코드 및 전처리 관련 주피터 노트북
*   notebooks/embedding/: KoSentenceBERT 임베딩 모델링 및 차원축소(UMAP), 토픽 클러스터링(LDA) 모델링 코드
*   notebooks/analysis/: 액터-액션 분류 및 IPA(Importance-Performance Analysis) 기회영역 분석 코드

---

## 데이터 분석 요약

### 1. 데이터 수집 (VOC Crawling)
*   채널: 유튜브(층간소음 피해자 채널), 블라인드(직장인 커뮤니티), 네이버 카페(레몬테라스 등), 네이버 지식인, 당근마켓
*   규모: 총 71,233건 수집 ️ 전처리 후 58,566건 유효 데이터 확보 (2023~2026년 기준)

### 2. 데이터 전처리
*   특수문자·이모지 제거, 한글 형태소 분석, 동의어 표준화(예: '층소', '발망치' ️ '층간소음'), 무의미한 단어 필터링을 진행했습니다.

### 3. 임베딩 및 차원축소 (Embedding & Dimensionality Reduction)
*   KoSentenceBERT 모델을 활용하여 한국어 구어체의 미묘한 뉘앙스를 고려한 고차원(768차원) 문맥 벡터 추출
*   UMAP을 사용해 768차원을 핵심 정보 손실 없이 5차원으로 축소하여 군집화 성능을 향상시켰습니다.

### 4. 클러스터링 및 액터 도출 (K-Means & Actor Analysis)
*   K-Means 알고리즘(Elbow & Silhouette 지표 기준 K=4 최적화)을 통해 고객 페인포인트 유형 분류
*   ACTOR 0 (45.5%): 발망치·새벽 소음 등 만성적인 층간소음 피해 주민
*   ACTOR 2 (28.6%): 강아지 짖음·생활 소음 및 환경/교통 소음에 노출된 이웃 주민

### 5. 기회 영역 분석 (IPA Analysis)
*   Relative Importance(중요도)와 Relative Satisfaction(만족도) 매트릭스를 기반으로 최우선 대처가 필요한 핵심 소음 유형 분석 ️ 충격음(발망치), 보복소음 우려, 불면증 유발 음역을 긴급 마스킹 대상으로 선정했습니다.

---

## 서비스 기획 (Service Concept & CX Strategy)

*   Pain Point: 공동 주택 특성상 외부 소음을 물리적으로 100% 차단할 수 없다는 무력감, 그리고 이웃과의 대면 충돌 우려
*   CX 목표: 소음의 '완전한 제거'가 아닌, 맞춤형 대응 사운드 재생을 통한 '정서적 통제감 회복' 및 취약 시간대 안식 보장
*   ThinQ 기반 5+1 모드:
    1.  층간소음 마스킹 모드: 발망치 및 충격음 마스킹 (저음역 위주 Brown Noise 분사)
    2.  외부 소음 차폐 모드: 도로 경적, 오토바이 배기음 등 마스킹 (고주파 차단 자연음 결합)
    3.  집중 몰입 모드: 백색소음을 통한 독서·재택근무 집중 유도
    4.  딥슬립 케어 모드: 수면 뇌파 안정을 돕는 서서히 작아지는 슬립 노이즈
    5.  펫 홈 케어 모드: 보호자 부재 시 반려동물의 심리적 안정을 위한 힐링 사운드
    6.  AI 자동 통합 모드: 실시간 FFT 주파수 분석을 통해 EQ 및 마스킹 볼륨을 자동 튜닝 (Dynamic EQ)


---

# Puri Sound

Raspberry Pi 기반 **소음 감지 → 마스킹 사운드 재생 → LED 동기화 → Firebase · 앱 연동** 프로젝트입니다.

번호별 폴더로 하드웨어 테스트부터 운영 파이프라인까지 구성합니다.

## 운영 구성 (현재)

| 위치 | 구성 | 역할 |
|------|------|------|
| **라즈베리파이** | `9_send_firebase` (`send_firebase.py`) | 마이크 분석 · Firebase 전송 · 마스킹 결정 · IPC 명령 — **systemd 부팅 자동** |
| **라즈베리파이** | `18_player_ai_control` (`player_run.py`) | 마스킹 재생 + **앱 음원 설정** |
| **라즈베리파이** | `19_led_ai_control` (`led_run.py`) | LED + **앱 LED 설정** |
| **다른 서버/PC** | `PuriSound_YAMNET` (`server.py`, `:5000`) | YAMNet 소리 분류 — 실시간 수집된 4초 오디오를 521종 음향 카테고리(발망치, 강아지 짖음 등)로 식별하여 회신하는 지능형 소음 분류기 (다른 서버에서 실행) |
| 선택 | `9/viewer.py` | 9번 로컬 상태 모니터 (안 켜도 파이프라인 동작) |
| 선택 | `13_noise_db` | Firebase `noiseEvents` 클라우드 뷰어 |
| 선택 | `17_server` | 로컬 재생 명령 API |

```
[다른 서버]  PuriSound_YAMNET/server.py (:5000)  ──────┐
                                                       │ HTTP POST /classify
[Pi systemd] 9 send_firebase  → Firebase noiseEvents   │
                 │ 마스킹 결정                         │
                 ▼                                     │
         /tmp/player_ai_command.json                   │
                 │                                     │
[Pi] 18 player_run  → 재생 → /tmp/player_ai_status.json│
[Pi] 19 led_run     → LED                              │
                                                       │
[앱] ──write──▶ Firebase users/{uid}/settings/app ◀── 18·19 poll
```

**한 줄:** Pi에서 **9 + 18 + 19**, 다른 서버에서 **`PuriSound_YAMNET`** 켜면 정상 동작.  
`viewer.py`는 확인용입니다. YAMNet이 꺼져 있어도 9는 FFT 분류만으로 동작합니다.

### 15 / 16 → 18 / 19

| 구버전 | 현재 | 차이 |
|--------|------|------|
| `15_player_ai` | `18_player_ai_control` | 15 + 앱(Firebase) **음원** 제어 |
| `16_led_ai` | `19_led_ai_control` | 16 + 앱(Firebase) **LED** 제어 |

앱 설정 경로 예: `users/{ownerId}/settings/app`  
(`autoMasking`, `noiseType`, `volume`, `ledMode`, `ledColor`, `ledBrightness` 등)

- `14_noise_ai`: 예전 “결정 전용” 분리 모듈. 지금은 9가 결정·IPC를 상당 부분 수행 → **보통 생략**
- `12_total`: 올인원 실험 — **미사용** (9와 마이크 충돌)

## 전체 폴더 흐름

```
[하드웨어 테스트]
  1_LED → 2_speaker → 3_mic → 4_loadtest

[소음 분석 · 클라우드]
  8_MIC_FFT (FFT 엔진) → 9_send_firebase (전송+결정+IPC, 부팅 자동)
  5_noise_client (YAMNet API 테스트 클라이언트 — 서버 아님)
  13_noise_db (Firebase 모니터링)

[마스킹 준비 · 수동 재생]
  6_FFT (MP3 FFT 사전 분석) → 10_masking (라이브러리·프로필)
  7_Player (수동 재생) + 11_led_connect (7번용 LED)

[운영 파이프라인 ]
  9 → 18 → 19   (+ PuriSound_YAMNET on another host)

[구 분리 구조 — 참고]
  9 → 14(결정) → 15(재생) → 16(LED)
```

## 폴더 구조

| 폴더 | 설명 |
|------|------|
| `1_LED` | NeoPixel LED (SPI) 테스트 · 공통 `config` / venv |
| `2_speaker` | 3.5mm 스피커 테스트 |
| `3_mic` | USB 마이크 테스트 |
| `4_loadtest` | LED+스피커+마이크 부하 테스트 |
| `5_noise_client` | YAMNet API **테스트 클라이언트** (서버 아님) |
| `6_FFT` | 마스킹 MP3 FFT 사전 분석 |
| `7_Player` | 마스킹 MP3 수동 재생 |
| `8_MIC_FFT` | 실시간 마이크 FFT (9가 import) |
| `9_send_firebase` |  감지·전송·마스킹 결정·IPC (`systemd` + `viewer.py`) |
| `10_masking` | 마스킹 MP3 · FFT 프로필 데이터 |
| `11_led_connect` | 7_player ↔ LED 동기화 프로토타입 |
| `12_total` | 올인원 실험 (**미사용**) |
| `13_noise_db` | Firebase `noiseEvents` 뷰어 |
| `14_noise_ai` | 구 마스킹 결정 전용 |
| `15_player_ai` | 구 재생기 (IPC만) |
| `16_led_ai` | 구 LED (재생 연동만) |
| `17_server` | 로컬 Flask 재생명령 API (선택) |
| `18_player_ai_control` |  운영 재생기 (15 + 앱 음원) |
| `19_led_ai_control` |  운영 LED (16 + 앱 LED) |
| `PuriSound_YAMNET/` |  **YAMNet 분류 서버** — 라즈베리파이의 연산 자원 한계를 보완하기 위한 실시간 소음 카테고리 분류 서버 |
| `legacy/` | Pi 3 구버전 통합 코드 |
| `memo/` | 개발 메모 · 폴더별 설명 · `py_roles.txt` |

## 요구 환경

- **OS**: Raspberry Pi OS (실행 대상), Windows에서도 코드·문서 편집 가능
- **Python**: 3.11+ 권장
- **하드웨어** (Pi)
  - USB 마이크
  - 3.5mm 자체 전원 앰프 스피커
  - NeoPixel LED — **SPI GPIO 10** (PWM GPIO 12는 오디오와 충돌)
- **네트워크**: Firebase · (선택) YAMNet 서버 접근

## 빠른 시작 (운영)

### 1) 9번 — 부팅 시 자동 (`send_firebase`)

```bash
# 최초 1회 (Pi, 경로가 /data 인 경우)
sudo cp /data/9_send_firebase/send-firebase.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now send-firebase.service

systemctl status send-firebase.service
```

상태 모니터 (선택):

```bash
cd /data/9_send_firebase
python3 viewer.py
```

수동 실행 예:

```bash
cd /data/9_send_firebase
.venv/bin/python3 send_firebase.py
python3 send_firebase.py --dry-run
python3 send_firebase.py --no-yamnet    # YAMNet 없이 FFT만
```

### 2) 18번 — 재생

```bash
cd /data/18_player_ai_control
./run.sh
# 또는
python3 player_run.py
```

### 3) 19번 — LED

```bash
cd /data/19_led_ai_control
./run.sh
# 또는
python3 led_run.py
```

### 4) YAMNet 서버 (`PuriSound_YAMNET` — 다른 PC/서버)

코드는 이 레포의 [`PuriSound_YAMNET/`](PuriSound_YAMNET/) 에 있습니다. **라즈베리파이가 아닌 머신**에서 실행하세요.

```bash
cd PuriSound_YAMNET
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux:   source .venv/bin/activate
pip install -r requirements.txt
python server.py
# → http://0.0.0.0:5000  (POST /classify, GET /health)
```

- 상세: [`PuriSound_YAMNET/README.md`](PuriSound_YAMNET/README.md)
- 9번 기본 URL 예: `http://172.16.11.110:5000` — 서버 IP가 다르면  
  `python3 send_firebase.py --yamnet-url http://<서버IP>:5000`
- 서버 OFF여도 9는 **FFT만**으로 동작
- `5_noise_client`는 테스트 클라이언트일 뿐, 서버가 아님
- 입력 WAV는 약 **4초** (서버 제약)

### 하드웨어 단위 테스트

```bash
cd 1_LED && ./venv/bin/python3 neopixel_test.py
cd 2_speaker && python3 speaker_test.py
cd 3_mic && python3 mic_test.py
```

## Firebase · 앱 설정

1. Firebase 서비스 계정 JSON 발급 → **로컬만** 배치  
   예: `9_send_firebase/firebase.json` (`.gitignore` 포함)
2. 9 · 13 · 14 · 18 · 19 등이 Admin SDK로 사용
3. 앱 UI는 `users/{uid}/settings/app` 에 설정을 쓰고, **18·19가 폴링**해 반영  
   (앱 ↔ Pi 소켓 직결이 아니라 **Firebase 경유**)

시크릿이 노출된 적이 있으면 키를 재발급하세요.

## 마스킹 사운드

- `10_masking/masking_sounds/`, `masking_fft_profiles.json` — 9/14 결정 엔진 후보
- `6_FFT/analyze_masking_fft.py` — FFT 프로필 생성
- 대용량 MP3는 필요 시 `.gitignore`에서 제외 가능

## 보안

| 항목 | 이유 |
|------|------|
| `github_pat.txt` | GitHub 토큰 |
| `**/firebase.json` | 서비스 계정 private key |
| `venv/`, `.venv/`, `__pycache__/` | 환경·캐시 |
| `*.log` | 런타임 로그 |

## 문서

상세는 `memo/` · YAMNet 폴더 README를 보세요.

| 파일 | 내용 |
|------|------|
| [`memo/memo.txt`](memo/memo.txt) | 전체 지도 · 운영 치트시트 |
| [`memo/py_roles.txt`](memo/py_roles.txt) | **전체 `.py` 파일별 역할** |
| `memo/1_LED.txt` … `memo/19_led_ai_control.txt` | 폴더별 설명 · 파일 역할 |
| [`PuriSound_YAMNET/README.md`](PuriSound_YAMNET/README.md) | YAMNet 서버 설치·API·연동 |
| `memo/legacy.txt` | 구버전 참고 |
| `memo/basic/` | 하드웨어·venv 트러블슈팅 |

## Git (변경분만)

이미 원격이 있으면 수정·추가된 파일만 커밋·푸시됩니다.

```bash
git status
git add -u                 # 추적 중 변경만
# 또는 git add memo README.md
git commit -m "메시지"
git push
```

## 라이선스

내부 프로젝트로 사용 중입니다. 공개 시 라이선스와 시크릿 제외를 다시 확인하세요.
