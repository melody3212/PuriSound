# Puri Sound

LG DX School 프로젝트 — **VOC 데이터 분석·서비스 기획**과 Raspberry Pi 기반 **소음 감지 → 마스킹 재생 → LED → Firebase·앱 연동** 구현을 한 저장소에 담습니다.

> 기획·수치의 기준 문서: 팀 포트폴리오 `puri_sound_overview.html` (로컬 백업)  
> 이 README는 그 내용을 레포 구조에 맞게 정리한 운영·재현 가이드입니다.

## 목차

1. [한 줄 정의](#한-줄-정의)
2. [데이터 분석 & 서비스 기획](#데이터-분석--서비스-기획)
3. [하드웨어·운영 파이프라인](#하드웨어운영-파이프라인)
4. [환경 설정](#환경-설정)
5. [빠른 시작](#빠른-시작)
6. [Firebase · 앱](#firebase--앱)
7. [보안](#보안)
8. [문서·외부 자료](#문서외부-자료)

---

## 한 줄 정의

주변 소음을 실시간으로 감지·분석하고, 유형·주파수에 맞는 대응 사운드(Brown / Pink / White 및 자연음)와 LED 피드백을 제공해 **수면·집중·휴식**을 지키는 생활 밀착형 스마트홈 서비스입니다.

| 레이어 | 내용 |
|--------|------|
| **기획 (BX/CX)** | VOC 71,233건 분석 → 액터 3종 → ThinQ **5+1 모드** |
| **디바이스** | Pi: `9` 감지·결정 + `18` 재생 + `19` LED / 외부: YAMNet 서버 |
| **앱** | Flutter(ThinQ 프로토타입) → Firebase `settings/app` → 18·19 폴링 |

---

## 데이터 분석 & 서비스 기획

상세 복제본: [`data_analyze/README.md`](data_analyze/README.md)

### 수집·전처리 규모 (overview 기준)

| 단계 | 규모 | 설명 |
|------|------|------|
| 크롤링 원본 | **71,233건** | 유튜브·블라인드·네이버 카페·지식인·당근마켓 |
| 전처리 완료 | **약 58,566건** | 특수문자·이모지·짧은 글 제거, 동의어(층소·발망치→층간소음), 불용어 (2023~2026) |
| 액터(K-Means) 분석 | **58,424건** | 전처리 후 클러스터링 직전 추가 정제분. 액터 비중·건수의 분모 |

### 임베딩·클러스터링 (최종 채택)

overview와 동일:

1. **KoSentenceBERT** 768차원 문맥 벡터  
2. **PCA 150차원** → **UMAP 5차원** (10차원 대비 분리 우수)  
3. **K-Means K=3** (Elbow·Silhouette) → 핵심 액터 3종  

> `data_analyze/notebooks/embedding/kosentence_bert_k5.ipynb` 등 **K=5 실험 노트북**은 탐색용입니다. **서비스 기획·액터 정의의 기준은 K=3** 입니다.

### 액터 (K=3, 58,424건 기준)

| 액터 | 정의 | 비중 | 건수 | 대표 키워드 |
|------|------|------|------|-------------|
| **ACTOR 0** | 층간소음 피해 주민 — 발망치·아이 뜀·새벽 | **45.5%** | 26,670 | 층간소음, 들리다, 소리, 뛰다, 새벽, 아이 |
| **ACTOR 1** | 커뮤니티 갈등 중재 운영진 — 저격·욕설 중재 | **25.63%** | 15,002 | 공격, 안내, 게시판, 경고, 욕설 |
| **ACTOR 2** | 반려동물·생활소음 피해 — 짖음·이웃 눈치 | **28.6%** | 16,752 | 뛰다, 강아지, 짖다 |

### 서비스 기획 — Pain / CX / 5+1 모드

* **Pain**: 소음을 물리적으로 100% 막을 수 없다는 무력감, 대면·게시판 갈등, 생활·펫 소음 스트레스  
* **CX 목표**: 완전 제거가 아니라 **맞춤 대응 사운드·자동 개입**으로 통제감·안식 회복, 비대면으로 분쟁 소지 완화  
* **5+1 모드 (앱 UX)**: 층간소음 마스킹 · 외부 소음 차폐 · 집중 몰입 · 딥슬립 케어 · 펫 홈 케어 + **AI 통합**

#### 액터 → 모드 매핑

| 액터 | 핵심 페인 | 우선 모드 |
|------|-----------|-----------|
| 0 | 발망치·새벽 충격음 | 층간소음 마스킹, 딥슬립, AI 자동 |
| 1 | 게시판 갈등 중재 부담 (간접 이해관계자) | AI 자동, 취약 시간대 딥슬립·집중 (피해 가구 선제 대응 → 분쟁 유입 완화) |
| 2 | 강아지·생활소음·눈치 | 외부 소음 차폐, 펫 홈 케어, 집중 몰입 |

#### 앱 모드(5+1) ↔ 디바이스 구현 매핑

기획 모드 이름과 파이썬 변수명이 1:1이 아닙니다. **앱/문서 = UX 모드**, **Pi = `noiseType` + 음원 버전 + autoMasking**.

| ThinQ / 기획 모드 | 디바이스 쪽 (대략) | 비고 |
|-------------------|-------------------|------|
| 층간소음 마스킹 | `noiseType=brown` (저음역 위주 버전) | 충격·저주파 |
| 외부 소음 차폐 | `noiseType=pink` (+ 자연음 계열 파일) | 환경·고주 성분 |
| 집중 몰입 | `pink` / `white` | 주간 생활소음 |
| 딥슬립 케어 | `brown` 소프트 버전 + 볼륨 페이드 | 취약 시간대 |
| 펫 홈 케어 | 진정 계열 음원 (`pink`/`white` 등) | 원인·피해 양측 |
| AI 통합 | 9번 FFT·YAMNet → 마스킹 결정 → 18 재생 | `autoMasking=true` 일 때 자동 |

### 폴더 (`data_analyze/`)

* `notebooks/crawling/` — 예시: 유튜브·레몬테라스 (전체 채널 크롤러는 Drive)  
* `notebooks/embedding/` — 임베딩·UMAP·LDA·K 실험  
* `notebooks/analysis/` — IPA·기회영역  

---

## 하드웨어·운영 파이프라인

### 운영 구성 (이 레포 기준)

| 위치 | 구성 | 역할 |
|------|------|------|
| **Pi** | `9_send_firebase` | 마이크·FFT·(선택)YAMNet · Firebase 전송 · 마스킹 결정 · IPC — **systemd 자동** |
| **Pi** | `18_player_ai_control` | 마스킹 재생 + 앱 음원 설정 폴링 |
| **Pi** | `19_led_ai_control` | LED + 앱 LED 설정 폴링 |
| **다른 PC** | `PuriSound_YAMNET` | YAMNet 521종 분류 (`:5000`) |
| 폴백 | `15_player_ai` / `16_led_ai` | IPC만 (앱 설정 없음) |
| 선택 | `viewer.py`, `13_noise_db`, `17_server` | 모니터·뷰어·로컬 API |

```
[YAMNet PC] server.py :5000  ──POST /classify──┐
                                               │
[Pi] 9 send_firebase → Firebase noiseEvents    │
        │ 마스킹 결정                          │
        ▼                                      │
 /tmp/player_ai_command.json                   │
        │                                      │
[Pi] 18 player_run → 재생  (음원: 10_masking 또는 로컬 masking_sounds)
[Pi] 19 led_run    → LED
                                               │
[앱] write → users/{uid}/settings/app ← poll 18·19
```

**한 줄:** Pi에서 **9 + 18 + 19**, 다른 호스트에서 **YAMNet** → 동작. YAMNet OFF여도 9는 **FFT만**으로 동작.

#### 음원 경로

- 카탈로그·FFT 프로필: `10_masking/masking_sounds/`, `masking_fft_profiles.json`
- **18번은 자체 `masking_sounds`가 없어도 됨** → 상위 `10_masking/masking_sounds` 를 폴백으로 사용
- `15_player_ai/masking_sounds` 는 구버전 복사본(중복 MP3 가능)

#### 15/16 → 18/19

| 구버전 | 운영 | 차이 |
|--------|------|------|
| `15_player_ai` | `18_player_ai_control` | + Firebase 앱 음원 |
| `16_led_ai` | `19_led_ai_control` | + Firebase 앱 LED |

- `14_noise_ai`: 구 결정 전용 → 지금은 9가 담당, 보통 생략  
- `12_total`: 올인원 실험 → **미사용** (9와 마이크 충돌)

### 폴더 구조

| 폴더 | 설명 |
|------|------|
| `1_LED` … `4_loadtest` | 하드웨어 단위 테스트 |
| `5_noise_client` | YAMNet **테스트 클라이언트** (서버 아님) |
| `6_FFT` / `10_masking` | 마스킹 MP3 FFT 분석·라이브러리 |
| `7_player` / `11_led_connect` | 수동 재생·LED 프로토타입 |
| `8_MIC_FFT` | 실시간 FFT (9가 import) |
| `9_send_firebase` | 운영 감지·전송·결정·IPC |
| `12_total` | 미사용 올인원 |
| `13_noise_db` | Firebase 이벤트 뷰어 |
| `14_noise_ai` | 구 결정 모듈 |
| `15_player_ai` / `16_led_ai` | 구 재생·LED |
| `17_server` | 로컬 재생 명령 API (선택) |
| `18_player_ai_control` / `19_led_ai_control` | **운영** 재생·LED |
| `PuriSound_YAMNET/` | YAMNet 서버 |
| `data_analyze/` | VOC 분석 노트북 |
| `legacy/` | Pi3 구코드 |
| `memo/` | **로컬 전용** (gitignore, IP·경로 메모) |
| `puri_env.py`, `.env.example` | 공통 환경변수 |

---

## 환경 설정

```bash
cp .env.example .env
# .env 값을 채운 뒤 사용 (커밋 금지)
```

| 변수 | 설명 |
|------|------|
| `PURI_YAMNET_URL` | YAMNet URL (예: `http://<SERVER_IP>:5000`) |
| `PURI_FIREBASE_DB_URL` | Realtime Database URL |
| `PURI_DEVICE_ID` | 디바이스 ID |
| `PURI_OWNER_ID` | 앱 사용자 uid |
| `PURI_DEVICE_NAME` | 표시 이름 |

- Firebase 서비스 계정: `9_send_firebase/firebase.json` 등 **로컬만** (gitignore)  
- `send-firebase.service` 기본값: 경로 `/data/...`, 사용자 `hwchoi` → **본인 Pi에 맞게 수정**

---

## 빠른 시작

### 의존성

폴더에 `requirements.txt`가 있으면 해당 폴더에서:

```bash
cd 9_send_firebase
python3 -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`8_MIC_FFT`, `13_noise_db`, `15`~`19`, `PuriSound_YAMNET` 등도 동일.

### 9 — 감지 (부팅 자동 예시)

```bash
# service 파일의 User / WorkingDirectory / ExecStart 수정 후
sudo cp /data/9_send_firebase/send-firebase.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now send-firebase.service
```

```bash
python3 send_firebase.py
python3 send_firebase.py --dry-run
python3 send_firebase.py --no-yamnet
python3 send_firebase.py --yamnet-url http://<SERVER_IP>:5000
python3 viewer.py   # 선택
```

### 18 / 19 — 재생 · LED

```bash
cd 18_player_ai_control && ./run.sh   # 또는 python3 player_run.py
cd 19_led_ai_control && ./run.sh
```

### YAMNet (다른 머신 권장)

```bash
cd PuriSound_YAMNET
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py   # :5000  GET /health  POST /classify
```

상세: [`PuriSound_YAMNET/README.md`](PuriSound_YAMNET/README.md)

### 하드웨어 스모크

```bash
cd 1_LED && ./venv/bin/python3 neopixel_test.py
cd 2_speaker && python3 speaker_test.py
cd 3_mic && python3 mic_test.py
```

LED는 **SPI GPIO 10** (PWM GPIO 12는 오디오와 충돌).

---

## Firebase · 앱

1. 서비스 계정 JSON → 로컬 `firebase.json` (커밋 금지)  
2. 앱이 `users/{uid}/settings/app` 에 설정 write → **18·19 폴링**  
3. Flutter 프로토타입: 별도 레포 예) [suuuhyuni/LG-PuriSound](https://github.com/suuuhyuni/LG-PuriSound) (이 레포에는 앱 소스 없음)

### `settings/app` 예시

```json
{
  "autoMasking": true,
  "noiseType": "brown",
  "noiseVersion": 1,
  "volume": 0.7,
  "ledMode": "noise",
  "ledColor": "#4FC3F7",
  "ledBrightness": 0.5
}
```

| 필드 | 의미 |
|------|------|
| `autoMasking` | false면 9의 자동 재생 명령을 18이 무시·정지 |
| `noiseType` | `brown` / `pink` / `white` |
| `noiseVersion` | 타입별 음원 버전 (카탈로그) |
| `volume` | 0~1 (일부 클라이언트는 `volumn` 오타 키도 허용) |
| `ledMode` / `ledColor` / `ledBrightness` | 19번 LED |

---

## 보안

| 항목 | 처리 |
|------|------|
| `.env`, `**/firebase.json` | gitignore |
| `memo/` | gitignore (내부 IP·경로) |
| API 키 | **코드·노트북에 하드코딩 금지** → 환경변수 |
| YouTube 수집 | `YOUTUBE_API_KEY` 환경변수. 과거에 키가 커밋된 적 있으면 **콘솔에서 키 폐기·재발급** |

---

## 문서·외부 자료

| 자료 | 내용 |
|------|------|
| [`data_analyze/README.md`](data_analyze/README.md) | 분석·기획 상세 |
| [`PuriSound_YAMNET/README.md`](PuriSound_YAMNET/README.md) | YAMNet API |
| [Drive 크롤링 코드](https://drive.google.com/drive/folders/1RPz5aWZ2aFgLKgWM7nP5zahUVbhfOVFu) | 선택 — 연구 재현용 |
| 로컬 `puri_sound_overview.html` | 기획·아키텍처·트러블슈팅 기준 문서 |
| `memo/` (로컬) | 폴더별 치트시트 |

## 라이선스

내부·과제 프로젝트 용도입니다. 공개 배포 시 라이선스와 시크릿·대용량 음원 라이선스를 재확인하세요.
