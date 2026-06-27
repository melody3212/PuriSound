"""Simple test client for the YAMNet Flask API."""

import os
import sys
from pathlib import Path

import requests
from scipy.io import wavfile

_DATA_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DATA_ROOT))
from puri_env import DEFAULT_YAMNET_URL  # noqa: E402

BASE_URL = os.environ.get("PURI_YAMNET_URL", DEFAULT_YAMNET_URL).strip()


def make_2sec_wav(source_path: Path, output_path: Path) -> None:
    sample_rate, waveform = wavfile.read(source_path)
    target_samples = sample_rate * 2
    clipped = waveform[:target_samples]
    wavfile.write(output_path, sample_rate, clipped)


def main() -> None:
    sample = Path(__file__).resolve().parent / "test_sample.wav"
    if not sample.exists():
        print("test_sample.wav not found. Run the server test setup first.")
        sys.exit(1)

    wav_2s = Path(__file__).resolve().parent / "test_2sec.wav"
    make_2sec_wav(sample, wav_2s)

    with wav_2s.open("rb") as wav_file:
        response = requests.post(
            f"{BASE_URL}/classify",
            files={"file": ("test_2sec.wav", wav_file, "audio/wav")},
            timeout=30,
        )

    print("status:", response.status_code)
    print(response.json())


if __name__ == "__main__":
    main()