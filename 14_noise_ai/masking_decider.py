"""Firebase noiseEvent + 10_masking FFT 프로필 → 마스킹 트랙/음량 결정."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_DATA_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILES_JSON = _DATA_ROOT / "10_masking" / "masking_fft_profiles.json"
DEFAULT_SOUNDS_DIR = _DATA_ROOT / "10_masking" / "masking_sounds"

MAX_MASKING_TRACKS = 3
BAND_NAMES = ("low", "mid", "high")
BAND_LABELS = {"low": "저음", "mid": "중음", "high": "고음"}

NOISE_COLOR_TO_TYPE = {
    "브라운": "brown",
    "핑크": "pink",
    "화이트": "white",
}

Action = Literal["play", "stop", "hold"]


@dataclass(frozen=True)
class MaskingCandidate:
    path: Path
    file_name: str
    noise_type: str
    low: float
    mid: float
    high: float
    dominant_frequency_hz: int
    noise_color: str

    def band_ratios(self) -> dict[str, float]:
        return {"low": self.low, "mid": self.mid, "high": self.high}


@dataclass(frozen=True)
class MaskingTrack:
    file_name: str
    file_path: Path
    volume: float
    match_score: float
    fill_bands: tuple[str, ...]


@dataclass(frozen=True)
class MaskingDecision:
    action: Action
    tracks: tuple[MaskingTrack, ...]
    total_volume: float
    noise_type: str | None
    db: int
    masking_required: bool
    match_score: float
    reason: str
    deficient_bands: tuple[str, ...] = ()

    @property
    def file_name(self) -> str | None:
        return self.tracks[0].file_name if self.tracks else None

    @property
    def file_path(self) -> Path | None:
        return self.tracks[0].file_path if self.tracks else None

    @property
    def volume(self) -> float:
        return self.total_volume

    def track_names(self) -> list[str]:
        return [t.file_name for t in self.tracks]

    def summary_lines(self) -> list[str]:
        vol_pct = f"{self.total_volume * 100:.0f}%"
        if self.action == "play":
            lines = [
                "  결정     : ▶ 재생",
                f"  트랙 수   : {len(self.tracks)}개 (최대 {MAX_MASKING_TRACKS}개)",
            ]
            if self.deficient_bands:
                lines.append(f"  결핍 대역 : {', '.join(self.deficient_bands)}")
            for i, track in enumerate(self.tracks, start=1):
                bands = ", ".join(track.fill_bands) if track.fill_bands else "-"
                lines.append(
                    f"  트랙 {i}    : {track.file_name} "
                    f"({track.volume * 100:.0f}%, 채움={bands}, 점수={track.match_score:.4f})"
                )
            lines.extend(
                [
                    f"  총 음량   : {vol_pct} (mixer 합산 기준 {self.total_volume:.2f})",
                    f"  noiseType: {self.noise_type or '-'}",
                    f"  db       : {self.db}",
                    f"  매칭점수 : {self.match_score:.4f}",
                    f"  사유     : {self.reason}",
                ]
            )
            return lines
        if self.action == "stop":
            return [
                "  결정     : ■ 정지",
                f"  총 음량   : {vol_pct}",
                f"  db       : {self.db}",
                f"  사유     : {self.reason}",
            ]
        lines = [
            "  결정     : ⏸ 유지",
            f"  트랙 수   : {len(self.tracks)}개",
        ]
        for i, track in enumerate(self.tracks, start=1):
            lines.append(
                f"  트랙 {i}    : {track.file_name} ({track.volume * 100:.0f}%)"
            )
        lines.extend(
            [
                f"  총 음량   : {vol_pct}",
                f"  db       : {self.db}",
                f"  사유     : {self.reason}",
            ]
        )
        return lines


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
        noise_color = str(item.get("noise_color", ""))
        noise_type = NOISE_COLOR_TO_TYPE.get(noise_color, "pink")

        candidates.append(
            MaskingCandidate(
                path=mp3_path,
                file_name=file_name,
                noise_type=noise_type,
                low=float(match_bands.get("low", {}).get("ratio", 0.0)),
                mid=float(match_bands.get("mid", {}).get("ratio", 0.0)),
                high=float(match_bands.get("high", {}).get("ratio", 0.0)),
                dominant_frequency_hz=int(item.get("dominant_frequency_hz", 0)),
                noise_color=noise_color,
            )
        )

    if not candidates:
        raise RuntimeError(
            f"마스킹 후보 없음 — profiles={profiles_json}, sounds={sounds_dir}"
        )
    return candidates


def load_fill_threshold(profiles_json: Path = DEFAULT_PROFILES_JSON) -> float:
    data = json.loads(profiles_json.read_text(encoding="utf-8"))
    return float(data.get("fill_threshold", 0.15))


def mic_profile_from_event(event: dict[str, Any]) -> dict[str, float]:
    fft = event.get("fft")
    if isinstance(fft, dict):
        regions = fft.get("regions")
        if isinstance(regions, dict):
            return {
                "low": float(regions.get("저음역", 0.0)),
                "mid": float(regions.get("중음역", 0.0)),
                "high": float(regions.get("고음역", 0.0)),
            }

    noise_type = str(event.get("noiseType", "pink"))
    defaults = {
        "brown": {"low": 0.7, "mid": 0.25, "high": 0.05},
        "pink": {"low": 0.2, "mid": 0.6, "high": 0.2},
        "white": {"low": 0.1, "mid": 0.3, "high": 0.6},
    }
    return defaults.get(noise_type, defaults["pink"])


def normalize_profile(profile: dict[str, float]) -> dict[str, float]:
    total = sum(float(profile.get(band, 0.0)) for band in BAND_NAMES)
    if total <= 0.0:
        return {band: 1.0 / len(BAND_NAMES) for band in BAND_NAMES}
    return {band: float(profile.get(band, 0.0)) / total for band in BAND_NAMES}


def deficient_bands(
    target: dict[str, float],
    covered: dict[str, float],
) -> dict[str, float]:
    return {
        band: max(0.0, target[band] - covered[band])
        for band in BAND_NAMES
    }


def label_deficient_bands(need: dict[str, float], *, fill_threshold: float) -> list[str]:
    labels = [
        BAND_LABELS[band]
        for band in BAND_NAMES
        if need[band] >= fill_threshold
    ]
    if labels:
        return labels
    ranked = sorted(need.items(), key=lambda item: item[1], reverse=True)
    if ranked and ranked[0][1] > 0.0:
        return [BAND_LABELS[ranked[0][0]]]
    return []


def _fill_score(need: dict[str, float], candidate: MaskingCandidate) -> float:
    bands = candidate.band_ratios()
    return sum(need[band] * bands[band] for band in BAND_NAMES)


def _freq_bonus(
    target_hz: float,
    candidate: MaskingCandidate,
    *,
    scale: float,
) -> float:
    if target_hz <= 0 or candidate.dominant_frequency_hz <= 0:
        return 0.0
    log_diff = abs(math.log2(target_hz / candidate.dominant_frequency_hz))
    return max(0.0, 1.0 - log_diff / 4.0) * scale


def _bands_filled_by_candidate(
    need: dict[str, float],
    candidate: MaskingCandidate,
    *,
    fill_threshold: float,
) -> tuple[str, ...]:
    bands = candidate.band_ratios()
    filled = [
        BAND_LABELS[band]
        for band in BAND_NAMES
        if need[band] >= fill_threshold and bands[band] >= fill_threshold
    ]
    return tuple(filled)


def _candidate_pool(
    candidates: list[MaskingCandidate],
    *,
    noise_type: str | None,
    restrict_noise_type: bool,
) -> list[MaskingCandidate]:
    if restrict_noise_type and noise_type:
        typed = [c for c in candidates if c.noise_type == noise_type]
        if typed:
            return typed
    return candidates


def _profile_score(
    mic_profile: dict[str, float],
    candidate: MaskingCandidate,
    target_hz: float,
) -> float:
    band_score = _fill_score(mic_profile, candidate)
    return band_score + _freq_bonus(target_hz, candidate, scale=0.15)


def select_best_masking(
    mic_profile: dict[str, float],
    candidates: list[MaskingCandidate],
    *,
    noise_type: str | None = None,
    target_hz: float = 0.0,
) -> tuple[MaskingCandidate, float]:
    pool = _candidate_pool(candidates, noise_type=noise_type, restrict_noise_type=True)
    best = max(pool, key=lambda c: _profile_score(mic_profile, c, target_hz))
    score = _profile_score(mic_profile, best, target_hz)
    return best, score


def select_masking_stack(
    mic_profile: dict[str, float],
    candidates: list[MaskingCandidate],
    *,
    noise_type: str | None = None,
    target_hz: float = 0.0,
    max_tracks: int = MAX_MASKING_TRACKS,
    fill_threshold: float = 0.15,
) -> tuple[list[tuple[MaskingCandidate, float, tuple[str, ...]]], tuple[str, ...]]:
    """결핍 주파수 대역을 순차적으로 채우는 최대 max_tracks개 트랙을 선택합니다."""
    target = normalize_profile(mic_profile)
    covered = {band: 0.0 for band in BAND_NAMES}
    selected: list[tuple[MaskingCandidate, float, tuple[str, ...]]] = []
    used_files: set[str] = set()
    initial_need = deficient_bands(target, covered)
    deficient_labels = tuple(label_deficient_bands(initial_need, fill_threshold=fill_threshold))

    for index in range(max_tracks):
        need = deficient_bands(target, covered)
        if max(need.values()) < fill_threshold:
            break

        pool = _candidate_pool(
            candidates,
            noise_type=noise_type,
            restrict_noise_type=index == 0,
        )
        best: MaskingCandidate | None = None
        best_score = -1.0
        best_filled: tuple[str, ...] = ()

        for candidate in pool:
            if candidate.file_name in used_files:
                continue
            score = _fill_score(need, candidate)
            if index == 0:
                score += _freq_bonus(target_hz, candidate, scale=0.15)
            if score > best_score:
                best_score = score
                best = candidate
                best_filled = _bands_filled_by_candidate(
                    need,
                    candidate,
                    fill_threshold=fill_threshold,
                )

        if best is None or best_score <= 0.0:
            break

        selected.append((best, best_score, best_filled))
        used_files.add(best.file_name)
        bands = best.band_ratios()
        for band in BAND_NAMES:
            covered[band] = min(1.0, covered[band] + bands[band])

    if not selected:
        best, score = select_best_masking(
            mic_profile,
            candidates,
            noise_type=noise_type,
            target_hz=target_hz,
        )
        selected = [(best, score, tuple())]

    return selected, deficient_labels


def distribute_track_volumes(
    stack: list[tuple[MaskingCandidate, float, tuple[str, ...]]],
    total_volume: float,
) -> list[MaskingTrack]:
    if not stack or total_volume <= 0.0:
        return []

    scores = [max(item[1], 0.0) for item in stack]
    score_sum = sum(scores)
    tracks: list[MaskingTrack] = []

    if score_sum <= 0.0:
        per_track = round(total_volume / len(stack), 3)
        for candidate, score, filled in stack:
            tracks.append(
                MaskingTrack(
                    file_name=candidate.file_name,
                    file_path=candidate.path,
                    volume=per_track,
                    match_score=score,
                    fill_bands=filled,
                )
            )
        return tracks

    raw_volumes = [total_volume * (score / score_sum) for score in scores]
    rounded = [round(v, 3) for v in raw_volumes]
    drift = round(total_volume - sum(rounded), 3)
    if rounded:
        rounded[-1] = round(max(0.0, rounded[-1] + drift), 3)

    for (candidate, score, filled), volume in zip(stack, rounded, strict=True):
        tracks.append(
            MaskingTrack(
                file_name=candidate.file_name,
                file_path=candidate.path,
                volume=volume,
                match_score=score,
                fill_bands=filled,
            )
        )
    return tracks


def compute_volume(
    db: int,
    *,
    db_threshold: float = 40.0,
    db_full: float = 75.0,
    min_volume: float = 0.72,
    max_volume: float = 1.0,
) -> float:
    if db < db_threshold:
        return 0.0
    span = max(db_full - db_threshold, 1.0)
    t = (db - db_threshold) / span
    t = max(0.0, min(1.0, t))
    return round(min_volume + t * (max_volume - min_volume), 3)


def _tracks_from_candidates(
    current_files: list[str],
    candidates: list[MaskingCandidate],
    volume: float,
) -> list[MaskingTrack]:
    by_name = {c.file_name: c for c in candidates}
    per_track = round(volume / max(len(current_files), 1), 3)
    tracks: list[MaskingTrack] = []
    for file_name in current_files:
        candidate = by_name.get(file_name)
        if candidate is None:
            continue
        tracks.append(
            MaskingTrack(
                file_name=candidate.file_name,
                file_path=candidate.path,
                volume=per_track,
                match_score=0.0,
                fill_bands=(),
            )
        )
    return tracks


def _same_track_set(current_files: list[str], new_files: list[str]) -> bool:
    return sorted(current_files) == sorted(new_files)


def decide_masking(
    event: dict[str, Any],
    candidates: list[MaskingCandidate],
    *,
    db_threshold: float = 40.0,
    db_full: float = 75.0,
    min_volume: float = 0.72,
    max_volume: float = 1.0,
    current_files: list[str] | None = None,
    current_noise_type: str | None = None,
    hold_sec: float = 30.0,
    seconds_since_switch: float = 0.0,
    max_tracks: int = MAX_MASKING_TRACKS,
    fill_threshold: float = 0.15,
) -> MaskingDecision:
    db = event_db(event)
    masking_required = event.get("maskingRequired") is True
    noise_type = str(event.get("noiseType") or "") or None
    active_files = list(current_files or [])

    if not masking_required:
        quiet = False
        fft = event.get("fft")
        if isinstance(fft, dict) and fft.get("label") == "조용함":
            quiet = True
        reason = "마스킹 불필요 (조용함)" if quiet else "마스킹 불필요"
        if active_files:
            return MaskingDecision(
                action="stop",
                tracks=(),
                total_volume=0.0,
                noise_type=noise_type,
                db=db,
                masking_required=False,
                match_score=0.0,
                reason=reason,
            )
        return MaskingDecision(
            action="hold",
            tracks=(),
            total_volume=0.0,
            noise_type=noise_type,
            db=db,
            masking_required=False,
            match_score=0.0,
            reason=reason,
        )

    total_volume = compute_volume(
        db,
        db_threshold=db_threshold,
        db_full=db_full,
        min_volume=min_volume,
        max_volume=max_volume,
    )
    if total_volume <= 0.0:
        reason = f"db {db} < 임계 {db_threshold:.0f} — 재생 안 함"
        if active_files:
            return MaskingDecision(
                action="stop",
                tracks=(),
                total_volume=0.0,
                noise_type=noise_type,
                db=db,
                masking_required=True,
                match_score=0.0,
                reason=reason,
            )
        return MaskingDecision(
            action="hold",
            tracks=(),
            total_volume=0.0,
            noise_type=noise_type,
            db=db,
            masking_required=True,
            match_score=0.0,
            reason=reason,
        )

    mic_profile = mic_profile_from_event(event)
    target_hz = event_frequency_hz(event)
    stack, deficient_labels = select_masking_stack(
        mic_profile,
        candidates,
        noise_type=noise_type,
        target_hz=target_hz,
        max_tracks=max_tracks,
        fill_threshold=fill_threshold,
    )
    tracks = distribute_track_volumes(stack, total_volume)
    new_files = [track.file_name for track in tracks]
    match_score = max((track.match_score for track in tracks), default=0.0)

    if (
        active_files
        and not _same_track_set(active_files, new_files)
        and current_noise_type == noise_type
        and seconds_since_switch < hold_sec
    ):
        hold_tracks = _tracks_from_candidates(active_files, candidates, total_volume)
        return MaskingDecision(
            action="hold",
            tracks=tuple(hold_tracks),
            total_volume=total_volume,
            noise_type=noise_type,
            db=db,
            masking_required=True,
            match_score=match_score,
            reason=f"노이즈 타입 동일 — {hold_sec:.0f}초 홀드 중",
            deficient_bands=deficient_labels,
        )

    if active_files and _same_track_set(active_files, new_files):
        refreshed = tuple(
            MaskingTrack(
                file_name=track.file_name,
                file_path=track.file_path,
                volume=track.volume,
                match_score=track.match_score,
                fill_bands=track.fill_bands,
            )
            for track in tracks
        )
        return MaskingDecision(
            action="hold",
            tracks=refreshed,
            total_volume=total_volume,
            noise_type=noise_type,
            db=db,
            masking_required=True,
            match_score=match_score,
            reason="동일 트랙 조합 유지 (음량만 갱신)",
            deficient_bands=deficient_labels,
        )

    if active_files:
        reason = (
            f"트랙 조합 교체 — 결핍 {', '.join(deficient_labels) or '?'} / "
            f"{len(tracks)}개 레이어"
        )
    else:
        reason = (
            f"마스킹 시작 — 결핍 {', '.join(deficient_labels) or '?'} / "
            f"{len(tracks)}개 레이어"
        )

    return MaskingDecision(
        action="play",
        tracks=tuple(tracks),
        total_volume=total_volume,
        noise_type=noise_type,
        db=db,
        masking_required=True,
        match_score=match_score,
        reason=reason,
        deficient_bands=deficient_labels,
    )


def event_db(event: dict[str, Any]) -> int:
    db_value = event.get("db")
    if isinstance(db_value, (int, float)):
        return int(db_value)

    fft = event.get("fft")
    if isinstance(fft, dict):
        rms_dbfs = fft.get("rmsDbfs")
        if isinstance(rms_dbfs, (int, float)):
            return int(max(20, min(120, round(100.0 + float(rms_dbfs)))))
    return 0


def event_frequency_hz(event: dict[str, Any]) -> float:
    freq = event.get("frequencyHz")
    if isinstance(freq, (int, float)):
        return float(freq)

    fft = event.get("fft")
    if isinstance(fft, dict):
        for key in ("dominantHz", "centroidHz"):
            value = fft.get(key)
            if isinstance(value, (int, float)) and float(value) > 0:
                return float(value)
    return 0.0