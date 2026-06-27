#!/usr/bin/env python3
"""폴더 안의 MP3 파일 중 하나만 선택해 3.5mm 잭으로 재생합니다."""

import atexit
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

# pygame/SDL 초기화 전에 3.5mm 잭(bcm2835 Headphones) 지정
os.environ.setdefault("SDL_AUDIODRIVER", "alsa")
os.environ.setdefault("AUDIODEV", "plughw:CARD=Headphones,DEV=0")

import pygame

AUDIO_DEVICE = "3.5mm 잭 (bcm2835 Headphones)"
MAX_TRACKS = 3
LOCK_PATH = Path("/tmp/mp3_one.lock")
STATE_PATH = Path("/tmp/mp3_one_state.json")
_lock_file = None
_shutting_down = False


def noise_type_from_filename(name: str) -> str | None:
    lowered = name.lower()
    if "브라운" in name or "brown" in lowered:
        return "brown"
    if "핑크" in name or "pink" in lowered:
        return "pink"
    if "화이트" in name or "white" in lowered:
        return "white"
    return None


def list_mp3_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.mp3"))[:MAX_TRACKS]


def print_track_list(mp3_files: list[Path]) -> None:
    print("재생 가능한 음원:")
    for i, mp3_path in enumerate(mp3_files, start=1):
        noise = noise_type_from_filename(mp3_path.name) or "unknown"
        print(f"  {i}. {mp3_path.name} ({noise})")


def write_play_state(mp3_path: Path) -> None:
    noise_type = noise_type_from_filename(mp3_path.name)
    state = {
        "pid": os.getpid(),
        "updated_at": time.time(),
        "tracks": [{"file": mp3_path.name, "noise_type": noise_type}],
        "primary_noise_type": noise_type,
    }
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def clear_play_state() -> None:
    try:
        STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def stop_audio() -> None:
    try:
        if pygame.mixer.get_init():
            pygame.mixer.stop()
            pygame.mixer.quit()
    except pygame.error:
        pass


def release_lock() -> None:
    global _lock_file
    if _lock_file is None:
        return
    try:
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
        _lock_file.close()
    except OSError:
        pass
    _lock_file = None
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def shutdown(exit_code: int = 0) -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    stop_audio()
    clear_play_state()
    release_lock()
    raise SystemExit(exit_code)


def handle_exit(signum, _frame) -> None:
    name = signal.Signals(signum).name
    print(f"\n종료 신호 수신 ({name}), 재생을 중지합니다.", flush=True)
    shutdown(0)


def acquire_lock() -> None:
    global _lock_file
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        print(
            "이미 다른 mp3_one.py가 재생 중입니다.\n"
            "  종료: pkill -x mp3_one.py",
            file=sys.stderr,
        )
        sys.exit(1)

    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _lock_file = lock_file


def play_one_mp3(directory: Path, track_number: int) -> None:
    mp3_files = list_mp3_files(directory)
    if not mp3_files:
        print(f"MP3 파일이 없습니다: {directory}")
        sys.exit(1)

    if track_number < 1 or track_number > len(mp3_files):
        print(f"잘못된 번호입니다: {track_number} (1~{len(mp3_files)} 중 선택)", file=sys.stderr)
        print_track_list(mp3_files)
        sys.exit(1)

    mp3_path = mp3_files[track_number - 1]

    acquire_lock()
    atexit.register(stop_audio)
    atexit.register(release_lock)
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=8192)

    print(f"출력 장치: {AUDIO_DEVICE}")
    print(f"재생 폴더: {directory}")
    print(f"선택 번호: {track_number}")
    print(f"PID: {os.getpid()} (종료 시 이 프로세스를 종료해야 소리가 멈춥니다)")

    channel: pygame.mixer.Channel | None = None
    try:
        print(f"로딩 중: {mp3_path.name}")
        sound = pygame.mixer.Sound(str(mp3_path))
        channel = pygame.mixer.Channel(0)
        channel.play(sound, loops=-1)
        print(f"재생 중: {mp3_path.name}")

        write_play_state(mp3_path)

        while not _shutting_down and channel.get_busy():
            time.sleep(0.1)
    finally:
        stop_audio()
        release_lock()

    print("재생 완료")


def parse_args(argv: list[str], directory: Path) -> tuple[Path, int | None]:
    folder = directory
    track_number: int | None = None

    args = argv[1:]
    if not args:
        return folder, None

    if len(args) == 1:
        if args[0].isdigit():
            return folder, int(args[0])
        return Path(args[0]).resolve(), None

    folder = Path(args[0]).resolve()
    if args[1].isdigit():
        return folder, int(args[1])

    return folder, None


if __name__ == "__main__":
    default_folder = Path(__file__).resolve().parent
    folder, track_number = parse_args(sys.argv, default_folder)
    mp3_files = list_mp3_files(folder)

    if track_number is None:
        print("사용법: python mp3_one.py <번호>  또는  python mp3_one.py <폴더> <번호>")
        print_track_list(mp3_files)
        sys.exit(1)

    play_one_mp3(folder, track_number)