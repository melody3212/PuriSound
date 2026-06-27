"""마스킹 재생 트랙 → LED RGB 매핑."""

from __future__ import annotations

NOISE_SPECTRUMS: dict[str, list[tuple[int, int, int]]] = {
    "brown": [
        (255, 235, 0),
        (255, 170, 0),
        (255, 95, 0),
        (170, 55, 0),
        (95, 35, 0),
    ],
    "pink": [
        (255, 210, 235),
        (255, 130, 195),
        (255, 50, 150),
        (210, 0, 110),
        (140, 0, 70),
    ],
    "white": [
        (245, 250, 255),
        (200, 235, 255),
        (130, 200, 255),
        (60, 140, 255),
        (0, 70, 210),
    ],
    "idle": [(0, 0, 0)],
}

NOISE_COLORS: dict[str, tuple[int, int, int]] = {
    noise_type: spectrum[len(spectrum) // 2]
    for noise_type, spectrum in NOISE_SPECTRUMS.items()
}

NOISE_LABELS: dict[str, str] = {
    "brown": "브라운(저음)",
    "pink": "핑크(중음)",
    "white": "화이트(고음)",
    "idle": "대기",
}


def noise_type_from_filename(name: str) -> str | None:
    lowered = name.lower()
    if "브라운" in name or "brown" in lowered:
        return "brown"
    if "핑크" in name or "pink" in lowered:
        return "pink"
    if "화이트" in name or "white" in lowered:
        return "white"
    return None


def rgb_for_noise_type(noise_type: str | None) -> tuple[int, int, int] | None:
    if not noise_type:
        return None
    return NOISE_COLORS.get(noise_type)


def spectrum_for_noise_type(noise_type: str | None) -> list[tuple[int, int, int]]:
    if not noise_type:
        return [(120, 120, 120)]
    return list(NOISE_SPECTRUMS.get(noise_type, [(120, 120, 120)]))


def scale_rgb(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    clamped = max(0.0, min(1.0, factor))
    return tuple(int(channel * clamped) for channel in rgb)


def boost_rgb(
    rgb: tuple[int, int, int],
    *,
    saturation: float = 1.85,
    gain: float = 1.25,
) -> tuple[int, int, int]:
    gray = sum(rgb) / 3.0
    boosted: list[int] = []
    for channel in rgb:
        value = gray + (channel - gray) * saturation
        value *= gain
        boosted.append(max(0, min(255, int(value))))
    return tuple(boosted)


def track_noise_type(track: dict, *, fallback: str | None = None) -> str | None:
    noise_type = track.get("noise_type")
    if isinstance(noise_type, str) and noise_type:
        return noise_type

    file_name = str(track.get("name") or track.get("file") or "")
    inferred = noise_type_from_filename(file_name)
    if inferred:
        return inferred
    return fallback