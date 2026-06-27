#!/usr/bin/env python3
"""마스킹 mp3/wav FFT 프로필을 미리 분석해 캐시 파일로 저장합니다."""

import sys
from pathlib import Path

from audio_utils import (
    analyze_files,
    default_cache_path,
    default_masking_folder,
    list_masking_files,
    save_profiles_cache,
)


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else default_masking_folder()
    cache_path = Path(sys.argv[2]) if len(sys.argv) > 2 else default_cache_path()

    files = list_masking_files(folder)
    if not files:
        print(f"{folder} 에 오디오 파일이 없습니다.")
        sys.exit(1)

    print(f"분석 대상: {len(files)}개 ({folder})")
    profiles = analyze_files(files, show_progress=True)
    save_profiles_cache(profiles, cache_path)
    print(f"저장 완료: {cache_path} ({len(profiles)}개)")


if __name__ == "__main__":
    main()