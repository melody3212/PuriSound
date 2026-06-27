"""노이즈 타입(brown/pink/white) → LED RGB 매핑."""

from __future__ import annotations

NOISE_COLORS: dict[str, tuple[int, int, int]] = {
    "idle": (40, 90, 210),
    "brown": (165, 82, 42),
    "pink": (255, 105, 180),
    "white": (255, 255, 255),
}

NOISE_LABELS: dict[str, str] = {
    "idle": "대기(시작)",
    "brown": "브라운(저음)",
    "pink": "핑크(중음)",
    "white": "화이트(고음)",
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


def rgb_for_noise_type(noise_type: str) -> tuple[int, int, int] | None:
    return NOISE_COLORS.get(noise_type)