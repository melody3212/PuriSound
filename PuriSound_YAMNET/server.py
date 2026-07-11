import base64
import json
import queue
import threading
import time
from typing import Any

from flask import Flask, Response, jsonify, render_template_string, request

from yamnet_classifier import YamnetClassifier

app = Flask(__name__)
classifier = YamnetClassifier()

_audio_subscribers: list[queue.Queue[str]] = []
_audio_subscribers_lock = threading.Lock()


@app.get("/")
def index():
    return render_template_string(UPLOAD_PAGE)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "model": "YAMNet", "port": 5000})


def _subscribe_audio_events() -> queue.Queue[str]:
    subscriber: queue.Queue[str] = queue.Queue(maxsize=32)
    with _audio_subscribers_lock:
        _audio_subscribers.append(subscriber)
    return subscriber


def _unsubscribe_audio_events(subscriber: queue.Queue[str]) -> None:
    with _audio_subscribers_lock:
        if subscriber in _audio_subscribers:
            _audio_subscribers.remove(subscriber)


def _broadcast_audio_event(payload: dict[str, Any]) -> None:
    message = json.dumps(payload, ensure_ascii=False)
    with _audio_subscribers_lock:
        subscribers = list(_audio_subscribers)

    for subscriber in subscribers:
        try:
            subscriber.put_nowait(message)
        except queue.Full:
            try:
                subscriber.get_nowait()
            except queue.Empty:
                pass
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                pass


@app.get("/events")
def audio_events():
    subscriber = _subscribe_audio_events()

    def stream():
        try:
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                try:
                    message = subscriber.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {message}\n\n"
        finally:
            _unsubscribe_audio_events(subscriber)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


UPLOAD_PAGE = """
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>PuriSound YAMNet</title>
  <style>
    body { font-family: sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; }
    h1 { font-size: 1.4rem; }
    h2 { font-size: 1.1rem; margin-top: 32px; }
    form { margin: 24px 0; }
    button { padding: 8px 16px; cursor: pointer; margin-right: 8px; }
    pre { background: #f4f4f4; padding: 12px; overflow-x: auto; }
    .error { color: #c00; }
    .muted { color: #666; font-size: 0.9rem; }
    .live-box { background: #f8fafc; border: 1px solid #dbe3ee; border-radius: 8px; padding: 12px; }
    .status-on { color: #0a7; }
    .status-off { color: #888; }
    audio { width: 100%; margin-top: 8px; }
  </style>
</head>
<body>
  <h1>PuriSound YAMNet 분류</h1>
  <p>WAV 파일을 선택한 뒤 <strong>분류하기</strong>를 누르세요.</p>
  <form id="form">
    <input type="file" id="file" accept=".wav,audio/wav" required>
    <button type="submit">분류하기</button>
  </form>
  <audio id="preview" controls></audio>
  <div id="result"></div>

  <h2>실시간 청취</h2>
  <div class="live-box">
    <p class="muted">서버로 들어오는 4초 WAV를 받는 즉시 재생합니다.</p>
    <button type="button" id="listenToggle">실시간 청취 시작</button>
    <span id="listenStatus" class="status-off">대기 중</span>
    <audio id="livePlayer" controls></audio>
    <p id="liveMeta" class="muted"></p>
  </div>

  <script>
    const preview = document.getElementById("preview");
    const livePlayer = document.getElementById("livePlayer");
    const listenToggle = document.getElementById("listenToggle");
    const listenStatus = document.getElementById("listenStatus");
    const liveMeta = document.getElementById("liveMeta");

    let previewUrl = null;
    let liveSource = null;
    let playbackQueue = [];
    let isPlayingLive = false;

    function revokePreviewUrl() {
      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
        previewUrl = null;
      }
    }

    function playBlobImmediately(blob) {
      revokePreviewUrl();
      previewUrl = URL.createObjectURL(blob);
      preview.src = previewUrl;
      preview.play().catch(() => {});
    }

    document.getElementById("file").addEventListener("change", (e) => {
      const file = e.target.files[0];
      revokePreviewUrl();
      if (!file) {
        preview.removeAttribute("src");
        return;
      }
      previewUrl = URL.createObjectURL(file);
      preview.src = previewUrl;
    });

    document.getElementById("form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const file = document.getElementById("file").files[0];
      const result = document.getElementById("result");
      if (!file) return;

      playBlobImmediately(file);
      result.innerHTML = "분류 중...";

      const body = new FormData();
      body.append("file", file);
      try {
        const res = await fetch("/classify", { method: "POST", body });
        const data = await res.json();
        if (!res.ok) {
          result.innerHTML = '<p class="error">' + (data.error || "요청 실패") + "</p>";
          return;
        }
        const top = data.predictions?.[0];
        result.innerHTML =
          "<p><strong>결과:</strong> " + (top?.label || data.primary_label) +
          " (" + ((top?.score ?? data.primary_score) * 100).toFixed(1) + "%)</p>" +
          "<pre>" + JSON.stringify(data, null, 2) + "</pre>";
      } catch (err) {
        result.innerHTML = '<p class="error">서버 연결 실패: ' + err + "</p>";
      }
    });

    function base64ToBlob(base64, mimeType) {
      const binary = atob(base64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
      }
      return new Blob([bytes], { type: mimeType });
    }

    async function drainLiveQueue() {
      if (isPlayingLive || playbackQueue.length === 0) return;
      isPlayingLive = true;

      while (playbackQueue.length > 0) {
        const item = playbackQueue.shift();
        const blob = base64ToBlob(item.audio_base64, "audio/wav");
        const url = URL.createObjectURL(blob);
        livePlayer.src = url;

        liveMeta.textContent =
          (item.client || "unknown") + " · " +
          (item.label || "분류 중") + " · " +
          new Date(item.received_at_ms).toLocaleTimeString();

        await new Promise((resolve) => {
          const cleanup = () => {
            livePlayer.removeEventListener("ended", onEnded);
            livePlayer.removeEventListener("error", onEnded);
            URL.revokeObjectURL(url);
            resolve();
          };
          const onEnded = () => cleanup();
          livePlayer.addEventListener("ended", onEnded);
          livePlayer.addEventListener("error", onEnded);
          livePlayer.play().catch(onEnded);
        });
      }

      isPlayingLive = false;
    }

    function startLiveListen() {
      if (liveSource) return;

      liveSource = new EventSource("/events");
      listenStatus.textContent = "연결됨";
      listenStatus.className = "status-on";
      listenToggle.textContent = "실시간 청취 중지";

      liveSource.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.type !== "audio") return;
        playbackQueue.push(payload);
        drainLiveQueue();
      };

      liveSource.onerror = () => {
        listenStatus.textContent = "연결 끊김 · 재연결 중";
        listenStatus.className = "status-off";
      };
    }

    function stopLiveListen() {
      if (liveSource) {
        liveSource.close();
        liveSource = null;
      }
      playbackQueue = [];
      isPlayingLive = false;
      listenStatus.textContent = "대기 중";
      listenStatus.className = "status-off";
      listenToggle.textContent = "실시간 청취 시작";
      liveMeta.textContent = "";
    }

    listenToggle.addEventListener("click", () => {
      if (liveSource) {
        stopLiveListen();
      } else {
        startLiveListen();
      }
    });
  </script>
</body>
</html>
"""


@app.get("/classify")
def classify_page():
    return render_template_string(UPLOAD_PAGE)


@app.post("/classify")
def classify():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "WAV file is required (form field: file)"}), 400

    uploaded = request.files["file"]
    if not uploaded.filename:
        return jsonify({"success": False, "error": "Empty filename"}), 400

    if not uploaded.filename.lower().endswith(".wav"):
        return jsonify({"success": False, "error": "Only .wav files are supported"}), 400

    wav_bytes = uploaded.read()
    if not wav_bytes:
        return jsonify({"success": False, "error": "Uploaded file is empty"}), 400

    try:
        result = classifier.classify(wav_bytes)
        _broadcast_audio_event(
            {
                "type": "audio",
                "received_at_ms": int(time.time() * 1000),
                "client": request.remote_addr,
                "filename": uploaded.filename,
                "label": result.get("primary_label"),
                "score": result.get("primary_score"),
                "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
            }
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "error": f"Classification failed: {exc}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)