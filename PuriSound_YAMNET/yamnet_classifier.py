import csv
from pathlib import Path

import numpy as np
import onnxruntime as ort
from scipy import signal
from scipy.io import wavfile

SAMPLE_RATE = 16000
TARGET_DURATION_SEC = 4.0
DURATION_TOLERANCE_SEC = 0.05
TOP_K = 10

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "models" / "yamnet.onnx"
CLASS_MAP_PATH = BASE_DIR / "data" / "yamnet_class_map.csv"


class YamnetClassifier:
    def __init__(self):
        self.session = ort.InferenceSession(
            str(MODEL_PATH),
            providers=["CPUExecutionProvider"],
        )
        self.class_names = self._load_class_names()

    def _load_class_names(self) -> list[str]:
        names: list[str] = []
        with CLASS_MAP_PATH.open(encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                names.append(row["display_name"])
        return names

    def _to_mono(self, waveform: np.ndarray) -> np.ndarray:
        if waveform.ndim == 1:
            return waveform
        return waveform.mean(axis=1)

    def _normalize(self, waveform: np.ndarray, dtype: np.dtype) -> np.ndarray:
        if np.issubdtype(dtype, np.floating):
            peak = np.max(np.abs(waveform))
            if peak > 1.0:
                waveform = waveform / peak
            return waveform.astype(np.float32)

        info = np.iinfo(dtype)
        return (waveform.astype(np.float32) / max(abs(info.min), info.max))

    def _resample(self, waveform: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate == SAMPLE_RATE:
            return waveform

        target_length = int(
            round(len(waveform) * SAMPLE_RATE / sample_rate)
        )
        return signal.resample(waveform, target_length).astype(np.float32)

    def _validate_duration(self, waveform: np.ndarray) -> float:
        duration = len(waveform) / SAMPLE_RATE
        min_duration = TARGET_DURATION_SEC - DURATION_TOLERANCE_SEC
        max_duration = TARGET_DURATION_SEC + DURATION_TOLERANCE_SEC

        if duration < min_duration or duration > max_duration:
            raise ValueError(
                f"WAV duration must be {TARGET_DURATION_SEC:.1f}s "
                f"(+/- {DURATION_TOLERANCE_SEC:.2f}s). Received {duration:.3f}s."
            )
        return duration

    def _prepare_waveform(self, wav_bytes: bytes) -> tuple[np.ndarray, float]:
        import io

        sample_rate, waveform = wavfile.read(io.BytesIO(wav_bytes))
        waveform = self._to_mono(waveform)
        waveform = self._normalize(waveform, waveform.dtype)
        waveform = self._resample(waveform, sample_rate)
        duration = self._validate_duration(waveform)

        target_samples = int(TARGET_DURATION_SEC * SAMPLE_RATE)
        waveform = waveform[:target_samples]
        return waveform.astype(np.float32), duration

    def classify(self, wav_bytes: bytes) -> dict:
        waveform, duration = self._prepare_waveform(wav_bytes)

        scores, embeddings, spectrogram = self.session.run(
            None,
            {"waveform": waveform},
        )

        mean_scores = scores.mean(axis=0)
        top_indices = np.argsort(mean_scores)[::-1][:TOP_K]

        predictions = [
            {
                "rank": rank + 1,
                "class_index": int(class_index),
                "label": self.class_names[class_index],
                "score": round(float(mean_scores[class_index]), 6),
            }
            for rank, class_index in enumerate(top_indices)
        ]

        primary = predictions[0]

        return {
            "success": True,
            "duration_sec": round(duration, 3),
            "sample_rate_hz": SAMPLE_RATE,
            "num_frames": int(scores.shape[0]),
            "primary_label": primary["label"],
            "primary_score": primary["score"],
            "predictions": predictions,
            "embedding_dim": int(embeddings.shape[1]),
            "spectrogram_shape": [int(spectrogram.shape[0]), int(spectrogram.shape[1])],
        }