"""10_masking FFT 프로필과 실시간 마이크 분석을 매칭해 최적 마스킹 MP3를 선택합니다."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from noise_colors import noise_type_from_filename

DEFAULT_PROFILES_JSON = Path("/data/10_masking/masking_fft_profiles.json")
DEFAULT_SOUNDS_DIR = Path("/data/6_FFT/masking_sounds")


@dataclass(frozen=True)
class MaskingCandidate:
    path: Path
    file_name: str
    noise_type: str | None
    low: float
    mid: float
    high: float


def load_candidates(
    profiles_json: Path = DEFAULT_PROFILES_JSON,
    sounds_dir: Path = DEFAULT_SOUNDS_DIR,
) -> list[MaskingCandidate]:
    data = json.loads(profiles_json.read_text(encoding="utf-8"))
    candidates: list[MaskingCandidate] = []

    for item in data.get("profiles", []):
        file_name = str(item["file"])
        mp3_path = sounds_dir / file_name
        if not mp3_path.is_file():
            continue

        match_bands = item.get("match_bands", {})
        candidates.append(
            MaskingCandidate(
                path=mp3_path,
                file_name=file_name,
                noise_type=noise_type_from_filename(file_name),
                low=float(match_bands.get("low", {}).get("ratio", 0.0)),
                mid=float(match_bands.get("mid", {}).get("ratio", 0.0)),
                high=float(match_bands.get("high", {}).get("ratio", 0.0)),
            )
        )

    if not candidates:
        raise RuntimeError(
            f"마스킹 후보 없음 — profiles={profiles_json}, sounds={sounds_dir}"
        )
    return candidates


def mic_profile_from_metrics(metrics: dict[str, float | str]) -> dict[str, float]:
    return {
        "low": float(metrics.get("region_저음역", 0.0)),
        "mid": float(metrics.get("region_중음역", 0.0)),
        "high": float(metrics.get("region_고음역", 0.0)),
    }


def _score_profile(
    mic_profile: dict[str, float],
    candidate: MaskingCandidate,
) -> float:
    return (
        mic_profile["low"] * candidate.low
        + mic_profile["mid"] * candidate.mid
        + mic_profile["high"] * candidate.high
    )


def score_masking(
    mic_profile: dict[str, float],
    candidate: MaskingCandidate,
) -> float:
    return _score_profile(mic_profile, candidate)


def select_best_masking(
    mic_profile: dict[str, float],
    candidates: list[MaskingCandidate],
    noise_type: str | None = None,
) -> MaskingCandidate:
    pool = candidates
    if noise_type:
        typed = [c for c in candidates if c.noise_type == noise_type]
        if typed:
            pool = typed

    return max(pool, key=lambda c: _score_profile(mic_profile, c))


def should_switch_track(
    *,
    current: MaskingCandidate | None,
    new_candidate: MaskingCandidate,
    current_noise_type: str | None,
    new_noise_type: str,
    now: float,
    last_switch_at: float,
    hold_sec: float,
) -> bool:
    """노이즈 타입(brown/pink/white)이 바뀐 경우에만 교체. 타입 흔들림은 hold_sec로 완화."""
    if current is None:
        return True
    if current.path == new_candidate.path:
        return False
    if current_noise_type == new_noise_type:
        return False
    if (now - last_switch_at) < hold_sec:
        return False
    return True