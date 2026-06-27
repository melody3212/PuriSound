#!/usr/bin/env python3
"""마이크 노이즈 데이터를 4초마다 Firebase에 전송합니다.

8_MIC_FFT의 FFT 분석(데시벨, 음역, 분류)과 5_noise_client의 YAMNet API 분류를 결합합니다.
"""

from __future__ import annotations

import argparse
import atexit
import io
import os
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_VENV_PYTHON = _SCRIPT_DIR / ".venv" / "bin" / "python3"
_PIDFILE = _SCRIPT_DIR / ".send_firebase.pid"
_MIC_RETRY_SECONDS = 12.0
_MIC_RETRY_INTERVAL = 1.0


def _release_single_instance() -> None:
    try:
        if _PIDFILE.exists() and int(_PIDFILE.read_text().strip()) == os.getpid():
            _PIDFILE.unlink()
    except (ValueError, OSError):
        pass


def acquire_single_instance() -> None:
    """중복 실행을 막습니다. USB 마이크는 한 프로세스만 사용할 수 있습니다."""
    if _PIDFILE.exists():
        try:
            old_pid = int(_PIDFILE.read_text().strip())
            os.kill(old_pid, 0)
            print(
                f"\nsend_firebase가 이미 실행 중입니다 (PID {old_pid}).\n"
                "  USB 마이크는 동시에 하나의 프로그램만 사용할 수 있습니다.\n"
                "  기존 프로세스를 종료한 뒤 다시 실행하세요:\n"
                f"    kill {old_pid}\n",
                file=sys.stderr,
            )
            raise SystemExit(1)
        except ProcessLookupError:
            _PIDFILE.unlink(missing_ok=True)
        except ValueError:
            _PIDFILE.unlink(missing_ok=True)

    _PIDFILE.write_text(str(os.getpid()))
    atexit.register(_release_single_instance)


def pick_input_device_with_retry(
    pa: pyaudio.PyAudio,
    preferred: str | None,
    retry_seconds: float = _MIC_RETRY_SECONDS,
) -> int:
    """캡처 직후 마이크가 잠깐 안 보일 때 재시도합니다."""
    deadline = time.monotonic() + retry_seconds
    while True:
        if list_input_devices(pa):
            return pick_default_input_device(pa, preferred)
        if time.monotonic() >= deadline:
            break
        print(
            "마이크 장치 대기 중... (다른 프로세스가 해제될 때까지 재시도)",
            flush=True,
        )
        time.sleep(_MIC_RETRY_INTERVAL)
    return pick_default_input_device(pa, preferred)


def _ensure_project_venv() -> None:
    """시스템 python3 실행 시 프로젝트 venv로 자동 전환합니다."""
    if os.environ.get("SEND_FIREBASE_VENV") == "1":
        return
    if not _VENV_PYTHON.is_file():
        return
    if Path(sys.executable).resolve() == _VENV_PYTHON.resolve():
        return
    try:
        import firebase_admin  # noqa: F401
    except ModuleNotFoundError:
        os.environ["SEND_FIREBASE_VENV"] = "1"
        os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), *sys.argv])


_ensure_project_venv()

try:
    import firebase_admin
except ModuleNotFoundError:
    print(
        "필요한 패키지가 없습니다. 아래 중 하나로 실행하세요:\n"
        f"  {_VENV_PYTHON} {Path(__file__).name}\n"
        "  ./.venv/bin/python3 send_firebase.py\n"
        "또는: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise
import numpy as np
import pyaudio
import requests
from firebase_admin import credentials, db, firestore
from scipy.signal import butter, sosfiltfilt

# 8_MIC_FFT 분석 모듈 재사용
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "8_MIC_FFT"))
from noise_analyzer import (  # noqa: E402
    CHUNK,
    NOISE_CATEGORIES,
    REGIONS,
    NoiseProfile,
    classify_noise,
    list_input_devices,
    pick_default_input_device,
    resolve_sample_rate,
)
from masking_decider import (  # noqa: E402
    DEFAULT_PROFILES_JSON,
    DEFAULT_SOUNDS_DIR,
    MAX_MASKING_TRACKS,
    MaskingDecision,
    decide_masking,
    load_candidates,
    load_fill_threshold,
)
from player_client import PlayerCommandWriter  # noqa: E402
from server_client import DEFAULT_SERVER_URL, check_server  # noqa: E402

_DATA_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DATA_ROOT))
from puri_env import (  # noqa: E402
    DEFAULT_DB_URL,
    DEFAULT_DEVICE_ID,
    DEFAULT_DEVICE_NAME,
    DEFAULT_OWNER_ID,
    DEFAULT_YAMNET_URL,
)

RECORD_SECONDS = 4.0
SEND_INTERVAL = 4.0
DBFS_TO_DB_OFFSET = 100.0

REGION_TO_NOISE_TYPE = {
    "저음역": "brown",
    "중음역": "pink",
    "고음역": "white",
}

NOISE_TYPE_LABELS = {
    "brown": "충격음 감지 · 저주파",
    "pink": "교통·실외기 지속음",
    "white": "팬·환기 고역 잡음",
}

YAMNET_KO_LABELS = {
    "Silence": "조용함",
    "Speech": "음성·대화 감지",
    "Music": "음악 감지",
    "Animal": "동물 소리 감지",
    "Domestic animals, pets": "반려동물 소리 감지",
    "Inside, small room": "실내 잔향",
    "Inside, large room or hall": "넓은 실내 공간",
    "Traffic noise, roadway noise": "교통·실외기 지속음",
    "Mechanical fan": "팬·환기 고역 잡음",
    "White noise": "백색 소음",
}


YAMNET_GAIN_DB = 10.0
HIGHPASS_CUTOFF_HZ = 120.0
WIND_SCORE_THRESHOLD = 0.55

WIND_YAMNET_LABELS = frozenset(
    {
        "Wind",
        "Gust, wind",
        "Rustling leaves",
        "Wind noise (microphone)",
        "Microphone noise",
    }
)

EXCLUDED_YAMNET_LABELS = frozenset(
    {
        "Stomach rumble",
    }
)


def audio_peak_dbfs(audio: np.ndarray) -> float:
    peak = float(np.max(np.abs(audio)))
    return 20 * np.log10(peak + 1e-12)


def highpass_audio(
    audio: np.ndarray,
    sample_rate: int,
    cutoff_hz: float = HIGHPASS_CUTOFF_HZ,
) -> np.ndarray:
    """저주파 바람/험 노이즈를 줄여 일상 소음 분석에 맞춥니다."""
    if cutoff_hz <= 0:
        return audio
    nyquist = sample_rate / 2
    if cutoff_hz >= nyquist - 1:
        return audio
    sos = butter(4, cutoff_hz / nyquist, btype="high", output="sos")
    filtered = sosfiltfilt(sos, audio.astype(np.float64))
    return filtered.astype(np.float32)


def wind_noise_score(
    audio: np.ndarray,
    sample_rate: int,
    metrics: dict[str, float | str],
    yamnet: dict[str, Any] | None = None,
) -> tuple[float, str]:
    """바람성 노이즈일 가능성 점수(0~1)와 근거를 반환합니다."""
    audio64 = audio.astype(np.float64)
    audio64 = audio64 - np.mean(audio64)
    rms = float(np.sqrt(np.mean(audio64**2)))
    peak = float(np.max(np.abs(audio64)))
    crest = peak / (rms + 1e-12)

    flatness = float(metrics["flatness"])
    low_ratio = float(metrics["region_저음역"])
    centroid = float(metrics["centroid_hz"])

    score = 0.0
    reasons: list[str] = []

    if flatness >= 0.22:
        score += 0.30
        reasons.append(f"flatness={flatness:.2f}")
    if low_ratio >= 0.68:
        score += 0.30
        reasons.append(f"저음={low_ratio * 100:.0f}%")
    if centroid < 400 and flatness >= 0.15:
        score += 0.15
        reasons.append(f"centroid={centroid:.0f}Hz")
    if crest >= 4.0 and flatness >= 0.18:
        score += 0.15
        reasons.append(f"crest={crest:.1f}")

    if yamnet and "error" not in yamnet:
        primary = str(yamnet.get("primary_label", ""))
        if primary in WIND_YAMNET_LABELS:
            score += 0.45
            reasons.append(f"YAMNet={primary}")
        else:
            for pred in yamnet.get("predictions", [])[:5]:
                if not isinstance(pred, dict):
                    continue
                label = str(pred.get("label", ""))
                pred_score = float(pred.get("score", 0.0))
                if label in WIND_YAMNET_LABELS and pred_score >= 0.25:
                    score += 0.35
                    reasons.append(f"YAMNet={label} {pred_score * 100:.0f}%")
                    break

    return min(1.0, score), ", ".join(reasons) if reasons else "none"


def remove_dc_offset(audio: np.ndarray) -> np.ndarray:
    return (audio.astype(np.float32) - float(np.mean(audio))).astype(np.float32)


def soft_limit_audio(audio: np.ndarray, drive: float = 1.5) -> np.ndarray:
    """증폭 후 하드 클리핑 대신 왜곡을 줄입니다."""
    return (np.tanh(audio.astype(np.float64) * drive) / np.tanh(drive)).astype(np.float32)


def prepare_analysis_audio(
    audio: np.ndarray,
    sample_rate: int,
    wind_filter: bool,
) -> np.ndarray:
    enhanced = remove_dc_offset(audio)
    if wind_filter:
        enhanced = highpass_audio(enhanced, sample_rate)
    return enhanced


def apply_yamnet_gain(audio: np.ndarray, gain_db: float = YAMNET_GAIN_DB) -> np.ndarray:
    """YAMNet 전송 전 고정 dB 보정을 적용합니다."""
    gain = 10 ** (gain_db / 20)
    return soft_limit_audio(audio * gain)


def audio_to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def check_yamnet_server(base_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """YAMNet 서버 연결 가능 여부를 확인합니다."""
    url = base_url.rstrip("/")
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code < 500:
            return True, f"켜짐 ({url})"
        return False, f"응답 오류 ({url}, HTTP {response.status_code})"
    except requests.ConnectionError:
        return False, f"꺼짐 ({url}, 연결 실패)"
    except requests.Timeout:
        return False, f"꺼짐 ({url}, 응답 시간 초과)"
    except requests.RequestException as exc:
        return False, f"꺼짐 ({url}, {exc})"


def sanitize_yamnet_result(yamnet: dict[str, Any] | None) -> dict[str, Any] | None:
    """제외 라벨을 빼고 primary/predictions를 다시 계산합니다."""
    if not yamnet or "error" in yamnet:
        return yamnet

    filtered: list[dict[str, Any]] = []
    for pred in yamnet.get("predictions", []):
        if not isinstance(pred, dict):
            continue
        label = str(pred.get("label", ""))
        if label in EXCLUDED_YAMNET_LABELS:
            continue
        filtered.append(pred)

    for index, pred in enumerate(filtered, start=1):
        pred["rank"] = index

    result = dict(yamnet)
    result["predictions"] = filtered
    if filtered:
        result["primary_label"] = filtered[0].get("label", result.get("primary_label"))
        result["primary_score"] = filtered[0].get("score", result.get("primary_score"))
    else:
        result["primary_label"] = None
        result["primary_score"] = 0.0
    return result


def yamnet_primary_prediction(
    yamnet: dict[str, Any] | None,
) -> tuple[str | None, float]:
    if not yamnet or "error" in yamnet:
        return None, 0.0

    for pred in yamnet.get("predictions", []):
        if not isinstance(pred, dict):
            continue
        label = str(pred.get("label", ""))
        if label and label not in EXCLUDED_YAMNET_LABELS:
            return label, float(pred.get("score", 0.0))

    primary = yamnet.get("primary_label")
    if primary and primary not in EXCLUDED_YAMNET_LABELS:
        return str(primary), float(yamnet.get("primary_score", 0.0))
    return None, 0.0


def yamnet_status_line(
    yamnet_url: str | None,
    yamnet: dict[str, Any] | None,
) -> str:
    if not yamnet_url:
        return "  YAMNet   : 사용 안 함 (--no-yamnet)"

    if yamnet is None:
        return "  YAMNet   : 꺼짐 (오디오 부족, FFT 분류만 사용)"

    if "error" in yamnet:
        err = str(yamnet["error"])
        if len(err) > 48:
            err = err[:45] + "..."
        return f"  YAMNet   : 꺼짐 ({err}) → FFT 분류 사용"

    primary, score = yamnet_primary_prediction(yamnet)
    if not primary:
        return "  YAMNet   : 켜짐 — (유효 분류 없음, FFT 분류 사용)"
    line = f"  YAMNet   : 켜짐 — {primary} ({score * 100:.0f}%)"
    input_peak = yamnet.get("_input_peak_dbfs")
    if input_peak is not None:
        line += f"  [입력 {input_peak:.0f} dBFS, +{YAMNET_GAIN_DB:.0f} dB 보정]"
    return line


def yamnet_top5_lines(yamnet: dict[str, Any] | None) -> list[str]:
    if not yamnet or "error" in yamnet:
        return []

    lines: list[str] = []
    for pred in yamnet.get("predictions", [])[:5]:
        if not isinstance(pred, dict):
            continue
        rank = pred.get("rank", len(lines) + 1)
        label = pred.get("label", "?")
        score = float(pred.get("score", 0.0))
        lines.append(f"  YAMNet #{rank}: {label} ({score * 100:.1f}%)")
    return lines


def classify_yamnet(
    audio: np.ndarray,
    sample_rate: int,
    base_url: str,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """5_noise_client 방식으로 YAMNet API에 4초 오디오를 전송해 분류합니다."""
    target_samples = int(sample_rate * RECORD_SECONDS)
    clipped = audio[:target_samples]
    if clipped.size < target_samples // 2:
        return None

    input_peak_db = audio_peak_dbfs(clipped)
    yamnet_audio = apply_yamnet_gain(clipped)
    wav_bytes = audio_to_wav_bytes(yamnet_audio, sample_rate)
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/classify",
            files={"file": ("sample.wav", wav_bytes, "audio/wav")},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload["_input_peak_dbfs"] = round(input_peak_db, 1)
            payload["_yamnet_gain_db"] = YAMNET_GAIN_DB
            return payload
        return {
            "results": payload,
            "_input_peak_dbfs": round(input_peak_db, 1),
            "_yamnet_gain_db": YAMNET_GAIN_DB,
        }
    except (requests.RequestException, ValueError) as exc:
        return {"error": str(exc)}


def region_ratios(metrics: dict[str, float | str]) -> dict[str, float]:
    return {region: float(metrics[f"region_{region}"]) for region in REGIONS}


def derive_noise_type(regions: dict[str, float]) -> str:
    dominant_region = max(regions, key=regions.get)
    if regions[dominant_region] < 0.35:
        return "pink"
    return REGION_TO_NOISE_TYPE[dominant_region]


def dbfs_to_db(rms_db: float) -> int:
    return int(max(20, min(120, round(DBFS_TO_DB_OFFSET + rms_db))))


def derive_frequency_hz(metrics: dict[str, float | str], noise_type: str) -> float:
    dominant = float(metrics["dominant_hz"])
    centroid = float(metrics["centroid_hz"])
    if dominant >= 40:
        return round(dominant, 1)
    if centroid >= 40:
        return round(centroid, 1)
    defaults = {"brown": 64.0, "pink": 520.0, "white": 4000.0}
    return defaults[noise_type]


def derive_yamnet_label(
    profile: NoiseProfile,
    noise_type: str,
    yamnet: dict[str, Any] | None,
) -> str:
    if profile.label == "조용함":
        return "조용함"

    if yamnet and "error" not in yamnet:
        primary, _ = yamnet_primary_prediction(yamnet)
        if primary and primary in YAMNET_KO_LABELS:
            return YAMNET_KO_LABELS[primary]
        if primary:
            return primary

    if " · " in profile.label:
        return profile.label.replace(" · ", " ")

    return NOISE_TYPE_LABELS[noise_type]


def derive_confidence(
    profile: NoiseProfile,
    yamnet: dict[str, Any] | None,
) -> float:
    scores = [profile.confidence]
    if yamnet and "error" not in yamnet:
        _, yamnet_score = yamnet_primary_prediction(yamnet)
        if yamnet_score > 0:
            scores.append(yamnet_score)
    return round(min(0.95, max(scores)), 2)


def _round_metric(value: float | str, digits: int = 4) -> float | str:
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def serialize_fft_for_event(
    profile: NoiseProfile,
    metrics: dict[str, float | str],
) -> dict[str, Any]:
    """Firebase noiseEvents에 저장할 FFT 주파수 전체 프로필을 직렬화합니다."""
    regions = {
        region: _round_metric(float(metrics.get(f"region_{region}", 0.0)))
        for region in REGIONS
    }
    bands: list[dict[str, Any]] = []
    for cat_id, region, sub_label, low_hz, high_hz, _desc in NOISE_CATEGORIES:
        key = f"cat_{cat_id}"
        if key not in metrics:
            continue
        bands.append(
            {
                "id": cat_id,
                "region": region,
                "subLabel": sub_label,
                "lowHz": float(low_hz),
                "highHz": float(high_hz),
                "ratio": _round_metric(float(metrics[key])),
            }
        )

    return {
        "label": profile.label,
        "detail": profile.detail,
        "confidence": _round_metric(profile.confidence),
        "rmsDbfs": _round_metric(float(metrics.get("rms_db", 0.0))),
        "peak": _round_metric(float(metrics.get("peak", 0.0))),
        "centroidHz": _round_metric(float(metrics.get("centroid_hz", 0.0)), 1),
        "dominantHz": _round_metric(float(metrics.get("dominant_hz", 0.0)), 1),
        "flatness": _round_metric(float(metrics.get("flatness", 0.0))),
        "humScore": _round_metric(float(metrics.get("hum_score", 0.0))),
        "regions": regions,
        "bands": bands,
    }


def serialize_yamnet_for_event(
    yamnet: dict[str, Any] | None,
    yamnet_url: str | None,
) -> dict[str, Any]:
    """Firebase noiseEvents에 저장할 YAMNet 원본 판정을 직렬화합니다."""
    if not yamnet_url:
        return {"status": "disabled"}

    if yamnet is None:
        return {"status": "unavailable"}

    if "error" in yamnet:
        return {"status": "error", "error": str(yamnet["error"])}

    primary, score = yamnet_primary_prediction(yamnet)
    predictions: list[dict[str, Any]] = []
    for pred in yamnet.get("predictions", [])[:5]:
        if not isinstance(pred, dict):
            continue
        predictions.append(
            {
                "rank": int(pred.get("rank", len(predictions) + 1)),
                "label": str(pred.get("label", "?")),
                "score": round(float(pred.get("score", 0.0)), 4),
            }
        )

    payload: dict[str, Any] = {
        "status": "online",
        "primaryLabel": primary,
        "primaryScore": round(float(score), 4) if score else 0.0,
        "predictions": predictions,
    }
    input_peak = yamnet.get("_input_peak_dbfs")
    if input_peak is not None:
        payload["inputPeakDbfs"] = float(input_peak)
    gain_db = yamnet.get("_yamnet_gain_db")
    if gain_db is not None:
        payload["gainDb"] = float(gain_db)
    return payload


def build_noise_event(
    profile: NoiseProfile,
    metrics: dict[str, float | str],
    device_id: str,
    owner_id: str,
    yamnet: dict[str, Any] | None,
    yamnet_url: str | None = None,
) -> dict[str, Any]:
    regions = region_ratios(metrics)
    noise_type = derive_noise_type(regions)
    db_value = dbfs_to_db(float(metrics["rms_db"]))
    frequency_hz = derive_frequency_hz(metrics, noise_type)
    yamnet_label = derive_yamnet_label(profile, noise_type, yamnet)
    confidence = derive_confidence(profile, yamnet)
    quiet = profile.label == "조용함"

    return {
        "deviceId": device_id,
        "ownerId": owner_id,
        "detectedAt": datetime.now(timezone.utc),
        "db": db_value,
        "frequencyHz": frequency_hz,
        "noiseType": noise_type,
        "yamnetLabel": yamnet_label,
        "fft": serialize_fft_for_event(profile, metrics),
        "yamnet": serialize_yamnet_for_event(yamnet, yamnet_url),
        "confidence": confidence,
        "maskingRequired": not quiet,
    }


def init_firebase(
    cred_path: Path,
    database_url: str | None,
    use_firestore: bool,
) -> None:
    if firebase_admin._apps:
        return
    cred = credentials.Certificate(str(cred_path))
    options: dict[str, str] = {}
    if not use_firestore and database_url:
        options["databaseURL"] = database_url
    firebase_admin.initialize_app(cred, options or None)


def push_noise_event(device_id: str, event: dict[str, Any], use_firestore: bool) -> str:
    if use_firestore:
        client = firestore.client()
        doc_ref = client.collection("devices").document(device_id).collection(
            "noiseEvents"
        ).document()
        event["noiseEventId"] = doc_ref.id
        doc_ref.set(event)

        client.collection("devices").document(device_id).update(
            {
                "decibel": event["db"],
                "updatedAt": datetime.now(timezone.utc),
                "connectionStatus": "connected",
            }
        )
        return doc_ref.id

    path = f"devices/{device_id}/noiseEvents"
    ref = db.reference(path)
    new_ref = ref.push(event)
    key = new_ref.key or ""
    if key:
        new_ref.update({"noiseEventId": key})
    db.reference(f"devices/{device_id}").update(
        {
            "decibel": event["db"],
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "connectionStatus": "connected",
        }
    )
    return key


def fft_summary_lines(fft_data: dict[str, Any] | None) -> list[str]:
    if not fft_data:
        return []

    lines = [
        f"  FFT      : {fft_data.get('label', '-')} "
        f"(centroid {fft_data.get('centroidHz', '-')} Hz, "
        f"dominant {fft_data.get('dominantHz', '-')} Hz)",
    ]
    regions = fft_data.get("regions") or {}
    if regions:
        region_text = ", ".join(
            f"{name} {float(ratio) * 100:.0f}%"
            for name, ratio in regions.items()
        )
        lines.append(f"  음역     : {region_text}")

    bands = fft_data.get("bands") or []
    top_bands = sorted(
        (band for band in bands if isinstance(band, dict)),
        key=lambda band: float(band.get("ratio", 0.0)),
        reverse=True,
    )[:3]
    for band in top_bands:
        lines.append(
            "  대역     : "
            f"{band.get('id')} {band.get('subLabel')} "
            f"({band.get('lowHz')}-{band.get('highHz')} Hz) "
            f"{float(band.get('ratio', 0.0)) * 100:.1f}%"
        )
    return lines


def format_masking_decision_log(decision: MaskingDecision | None) -> list[str]:
    if decision is None:
        return []
    return ["  마스킹 결정", *decision.summary_lines()]


def apply_masking_decision(
    decision: MaskingDecision,
    writer: PlayerCommandWriter,
    state: dict[str, Any],
    now: float,
    *,
    noise_event_id: str | None = None,
    detected_at: Any = None,
) -> None:
    if decision.action == "play":
        state["current_files"] = decision.track_names()
        state["current_noise_type"] = decision.noise_type
        state["last_switch_at"] = now
    elif decision.action == "stop":
        state["current_files"] = []
        state["current_noise_type"] = None
    elif decision.action == "hold" and decision.tracks:
        state["current_files"] = decision.track_names()

    writer.send_decision(
        decision,
        noise_event_id=noise_event_id,
        detected_at=detected_at,
    )


def format_local_log(
    event: dict[str, Any],
    key: str,
    device_id: str,
    yamnet_url: str | None,
    yamnet: dict[str, Any] | None,
    masking_decision: MaskingDecision | None = None,
) -> str:
    lines = [
        "",
        "─" * 50,
        f"  noiseEvents 전송  │  {event['detectedAt']}",
        f"  path     : devices/{device_id}/noiseEvents/{key}",
        yamnet_status_line(yamnet_url, yamnet),
        *yamnet_top5_lines(yamnet),
        f"  db       : {event['db']}",
        f"  주파수   : {event['frequencyHz']} Hz",
        *fft_summary_lines(event.get("fft")),
        f"  noiseType: {event['noiseType']}",
        f"  분류     : {event['yamnetLabel']} ({event['confidence'] * 100:.0f}%)",
        f"  마스킹   : {'필요' if event['maskingRequired'] else '불필요'}",
        *format_masking_decision_log(masking_decision),
    ]
    lines.append("─" * 50)
    return "\n".join(lines)


def format_wind_skip_log(
    score: float,
    reason: str,
    yamnet: dict[str, Any] | None = None,
) -> str:
    lines = [
        "",
        f"  [바람소리 감지] score={score:.2f} ({reason})",
        "  → 일상 노이즈가 아니어서 이번 전송을 생략합니다.",
        *yamnet_top5_lines(yamnet),
    ]
    return "\n".join(lines)


def record_and_send_loop(
    mic_device: int | str | None,
    sample_rate: int,
    send_interval: float,
    cred_path: Path,
    database_url: str | None,
    device_id: str,
    owner_id: str,
    yamnet_url: str | None,
    dry_run: bool,
    use_firestore: bool,
    wind_filter: bool,
    *,
    masking_enabled: bool = True,
    profiles_json: Path = DEFAULT_PROFILES_JSON,
    sounds_dir: Path = DEFAULT_SOUNDS_DIR,
    db_threshold: float = 40.0,
    db_full: float = 75.0,
    min_volume: float = 0.72,
    max_volume: float = 1.0,
    hold_sec: float = 30.0,
    max_tracks: int = MAX_MASKING_TRACKS,
    no_ipc: bool = False,
    server_url: str | None = DEFAULT_SERVER_URL,
    publish_server: bool = True,
) -> None:
    if not dry_run:
        init_firebase(cred_path, database_url, use_firestore)

    masking_candidates = None
    fill_threshold = 0.15
    player_writer: PlayerCommandWriter | None = None
    masking_state: dict[str, Any] = {
        "current_files": [],
        "current_noise_type": None,
        "last_switch_at": 0.0,
    }

    if masking_enabled:
        masking_candidates = load_candidates(
            profiles_json=profiles_json,
            sounds_dir=sounds_dir,
        )
        fill_threshold = load_fill_threshold(profiles_json)
        player_writer = PlayerCommandWriter(
            device_id=device_id,
            use_firestore=use_firestore,
            publish_firebase=not dry_run,
            write_ipc=not no_ipc,
            server_url=server_url,
            publish_server=publish_server,
        )

    pa = pyaudio.PyAudio()
    stream = None

    try:
        preferred = str(mic_device) if mic_device is not None else None
        index = pick_input_device_with_retry(pa, preferred)
        info = pa.get_device_info_by_index(index)
        device_name = str(info.get("name", f"device-{index}"))
        sample_rate = resolve_sample_rate(pa, index, sample_rate)

        samples_needed = int(sample_rate * RECORD_SECONDS)
        buffer: list[np.ndarray] = []
        buffered = 0

        # USB 마이크 버퍼 언더런을 줄이기 위해 20ms 버퍼 사용
        frames_per_buffer = max(CHUNK, int(sample_rate * 0.02))
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=index,
            frames_per_buffer=frames_per_buffer,
        )

        print(f"마이크 녹음 시작: [{index}] {device_name}")
        print(
            f"샘플레이트 {sample_rate} Hz, "
            f"{RECORD_SECONDS:.0f}초 녹음 · {send_interval:.0f}초마다 Firebase 전송"
        )
        if yamnet_url:
            online, status_msg = check_yamnet_server(yamnet_url)
            state = "켜짐" if online else "꺼짐"
            print(f"YAMNet 서버: {state} — {status_msg}")
            if not online:
                print("  → YAMNet 미연결 시 FFT 분류만 사용합니다.")
        else:
            print("YAMNet 서버: 사용 안 함 (--no-yamnet)")
        if wind_filter:
            print(
                f"바람 필터: 켜짐 (고역통과 {HIGHPASS_CUTOFF_HZ:.0f}Hz, "
                f"score>={WIND_SCORE_THRESHOLD:.2f} 생략)"
            )
        else:
            print("바람 필터: 꺼짐 (--no-wind-filter)")
        if masking_enabled and masking_candidates is not None:
            print(
                f"마스킹: 켜짐 ({len(masking_candidates)}개 후보, "
                f"최대 {max_tracks}트랙, 결핍 임계 {fill_threshold:.2f})"
            )
            print(
                f"  프로필: {profiles_json}\n"
                f"  사운드: {sounds_dir}\n"
                f"  db 임계: {db_threshold:.0f} | 음량: "
                f"{min_volume:.0%}~{max_volume:.0%} | 홀드: {hold_sec:.0f}s"
            )
            if dry_run:
                if not no_ipc:
                    print("  명령: /tmp/player_ai_command.json (IPC만)")
            else:
                storage = (
                    "playbackCommands/latest"
                    if use_firestore
                    else "playbackCommand"
                )
                print(f"  명령: Firebase devices/{device_id}/{storage}")
                if not no_ipc:
                    print("  IPC 폴백: /tmp/player_ai_command.json")
            if publish_server and server_url:
                online, status_msg = check_server(server_url)
                state = "켜짐" if online else "꺼짐"
                print(f"  17_server: {state} — {status_msg}")
                if online:
                    print(f"  저장 경로: {server_url.rstrip('/')}/api/playback-commands")
            else:
                print("  17_server: 사용 안 함 (--no-server)")
        else:
            print("마스킹: 꺼짐 (--no-masking)")
        if dry_run:
            print("모드: dry-run (Firebase 전송 없음)")
        elif use_firestore:
            target_name = (
                DEFAULT_DEVICE_NAME if device_id == DEFAULT_DEVICE_ID else device_id
            )
            print(f"Firebase 대상: {target_name}")
            print(f"  경로: devices/{device_id}/noiseEvents")
        else:
            print(f"Firebase RTDB: devices/{device_id}/noiseEvents")
        print("종료: Ctrl+C\n")

        last_yamnet_online: bool | None = None

        start = time.monotonic()
        last_send = start

        while True:
            raw = stream.read(CHUNK, exception_on_overflow=False)
            chunk = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            buffer.append(chunk)
            buffered += chunk.size

            now = time.monotonic()
            if buffered >= samples_needed and (now - last_send) >= send_interval:
                merged = np.concatenate(buffer)
                # 최신 4초만 사용 (앞쪽 오래된 버퍼를 쓰면 지연이 누적됨)
                raw_audio = merged[-samples_needed:]
                buffer = []
                buffered = 0

                analysis_audio = prepare_analysis_audio(
                    raw_audio, sample_rate, wind_filter
                )
                profile, metrics = classify_noise(analysis_audio, sample_rate)
                if yamnet_url:
                    online, status_msg = check_yamnet_server(yamnet_url)
                    if last_yamnet_online is not None and online != last_yamnet_online:
                        state = "켜짐" if online else "꺼짐"
                        print(f"\n[YAMNet 상태 변경] {state} — {status_msg}", flush=True)
                    last_yamnet_online = online
                    yamnet = (
                        classify_yamnet(analysis_audio, sample_rate, yamnet_url)
                        if online
                        else {"error": "서버 연결 불가"}
                    )
                    yamnet = sanitize_yamnet_result(yamnet)
                else:
                    yamnet = None

                if wind_filter:
                    wind_score, wind_reason = wind_noise_score(
                        raw_audio, sample_rate, metrics, yamnet
                    )
                    if wind_score >= WIND_SCORE_THRESHOLD:
                        print(
                            format_wind_skip_log(wind_score, wind_reason, yamnet),
                            flush=True,
                        )
                        last_send = now
                        continue

                event = build_noise_event(
                    profile, metrics, device_id, owner_id, yamnet, yamnet_url
                )

                if dry_run:
                    key = "dry-run"
                else:
                    key = push_noise_event(device_id, event, use_firestore)

                masking_decision: MaskingDecision | None = None
                if (
                    masking_enabled
                    and masking_candidates is not None
                    and player_writer is not None
                ):
                    seconds_since_switch = (
                        now - masking_state["last_switch_at"]
                        if masking_state["last_switch_at"]
                        else hold_sec
                    )
                    masking_decision = decide_masking(
                        event,
                        masking_candidates,
                        db_threshold=db_threshold,
                        db_full=db_full,
                        min_volume=min_volume,
                        max_volume=max_volume,
                        current_files=masking_state["current_files"],
                        current_noise_type=masking_state["current_noise_type"],
                        hold_sec=hold_sec,
                        seconds_since_switch=seconds_since_switch,
                        max_tracks=max_tracks,
                        fill_threshold=fill_threshold,
                    )
                    apply_masking_decision(
                        masking_decision,
                        player_writer,
                        masking_state,
                        now,
                        noise_event_id=None if key == "dry-run" else key,
                        detected_at=event.get("detectedAt"),
                    )

                print(
                    format_local_log(
                        event,
                        key,
                        device_id,
                        yamnet_url,
                        yamnet,
                        masking_decision,
                    ),
                    flush=True,
                )

                last_send = now
            elif buffered < samples_needed:
                time.sleep(0.01)

    except KeyboardInterrupt:
        print("\n전송을 종료합니다.")
    finally:
        if stream is not None:
            stream.stop_stream()
            stream.close()
        pa.terminate()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="마이크 노이즈(데시벨·음역·분류)를 4초마다 Firebase에 전송합니다.",
    )
    parser.add_argument(
        "--device",
        "-d",
        help="입력 장치 이름 일부 또는 인덱스 (예: 'USB', 1)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="입력 장치 목록을 출력하고 종료",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=48_000,
        help="샘플레이트 Hz (기본 48000)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=SEND_INTERVAL,
        help=f"Firebase 전송 주기 초 (기본 {SEND_INTERVAL})",
    )
    parser.add_argument(
        "--cred",
        type=Path,
        default=Path(__file__).resolve().parent / "firebase.json",
        help="Firebase 서비스 계정 JSON 경로",
    )
    parser.add_argument(
        "--device-id",
        default=DEFAULT_DEVICE_ID,
        help=f"Firestore devices 문서 ID (기본 {DEFAULT_DEVICE_ID})",
    )
    parser.add_argument(
        "--owner-id",
        default=DEFAULT_OWNER_ID,
        help=f"디바이스 소유자 uid (기본 {DEFAULT_OWNER_ID})",
    )
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DB_URL,
        help=f"Realtime Database URL (--rtdb 사용 시, 기본 {DEFAULT_DB_URL})",
    )
    parser.add_argument(
        "--rtdb",
        action="store_true",
        help="Firestore 대신 Realtime Database 사용",
    )
    parser.add_argument(
        "--yamnet-url",
        default=DEFAULT_YAMNET_URL,
        help=f"YAMNet API URL (비우면 YAMNet 분류 생략, 기본 {DEFAULT_YAMNET_URL})",
    )
    parser.add_argument(
        "--no-yamnet",
        action="store_true",
        help="YAMNet API 분류를 사용하지 않음",
    )
    parser.add_argument(
        "--no-wind-filter",
        action="store_true",
        help="바람소리 필터/전송 생략을 사용하지 않음",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Firebase 전송 없이 로컬 분석만 수행",
    )
    parser.add_argument(
        "--no-masking",
        action="store_true",
        help="10_masking 기반 마스킹 결정·재생 명령을 사용하지 않음",
    )
    parser.add_argument(
        "--profiles-json",
        type=Path,
        default=DEFAULT_PROFILES_JSON,
        help="10_masking FFT 프로필 JSON",
    )
    parser.add_argument(
        "--sounds-dir",
        type=Path,
        default=DEFAULT_SOUNDS_DIR,
        help="10_masking 마스킹 MP3 폴더",
    )
    parser.add_argument(
        "--db-threshold",
        type=float,
        default=40.0,
        help="마스킹 재생 시작 dB (기본 40)",
    )
    parser.add_argument(
        "--db-full",
        type=float,
        default=75.0,
        help="최대 음량에 도달하는 dB (기본 75)",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=0.72,
        help="최소 재생 음량 (기본 0.72)",
    )
    parser.add_argument(
        "--max-volume",
        type=float,
        default=1.0,
        help="최대 재생 음량 (기본 1.0)",
    )
    parser.add_argument(
        "--hold-sec",
        type=float,
        default=30.0,
        help="동일 noiseType 시 트랙 교체 최소 대기 초 (기본 30)",
    )
    parser.add_argument(
        "--max-tracks",
        type=int,
        default=MAX_MASKING_TRACKS,
        help=f"결핍 대역 채움용 최대 트랙 수 (기본 {MAX_MASKING_TRACKS})",
    )
    parser.add_argument(
        "--no-ipc",
        action="store_true",
        help="재생 명령을 /tmp/player_ai_command.json에 기록하지 않음",
    )
    parser.add_argument(
        "--server-url",
        default=DEFAULT_SERVER_URL,
        help=f"17_server URL (기본 {DEFAULT_SERVER_URL})",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="17_server에 마스킹 명령을 저장하지 않음",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    pa = pyaudio.PyAudio()
    if args.list_devices:
        print("입력 장치 목록:")
        for index, info in list_input_devices(pa):
            default = (
                " (default)"
                if index == pa.get_default_input_device_info()["index"]
                else ""
            )
            print(f"  [{index}] {info['name']}{default}")
        pa.terminate()
        return 0
    pa.terminate()

    if not args.dry_run and not args.cred.exists():
        print(f"Firebase 인증 파일을 찾을 수 없습니다: {args.cred}")
        return 1

    acquire_single_instance()

    device: int | str | None = args.device
    if args.device is not None:
        try:
            device = int(args.device)
        except ValueError:
            device = args.device

    yamnet_url = None if args.no_yamnet else args.yamnet_url

    if not 1 <= args.max_tracks <= MAX_MASKING_TRACKS:
        print(f"--max-tracks는 1~{MAX_MASKING_TRACKS} 사이여야 합니다.")
        return 1

    record_and_send_loop(
        mic_device=device,
        sample_rate=args.rate,
        send_interval=args.interval,
        cred_path=args.cred,
        database_url=args.database_url,
        device_id=args.device_id,
        owner_id=args.owner_id,
        yamnet_url=yamnet_url,
        dry_run=args.dry_run,
        use_firestore=not args.rtdb,
        wind_filter=not args.no_wind_filter,
        masking_enabled=not args.no_masking,
        profiles_json=args.profiles_json.resolve(),
        sounds_dir=args.sounds_dir.resolve(),
        db_threshold=args.db_threshold,
        db_full=args.db_full,
        min_volume=args.min_volume,
        max_volume=args.max_volume,
        hold_sec=args.hold_sec,
        max_tracks=args.max_tracks,
        no_ipc=args.no_ipc,
        server_url=None if args.no_server else args.server_url,
        publish_server=not args.no_server,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())