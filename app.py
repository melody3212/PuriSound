"""
app.py — YAMNet 소음 감지 시스템 Flask 제어 대시보드
실행: python app.py
  - 터미널: YAMNet 실시간 출력 유지
  - 브라우저: http://<라즈베리파이IP>:5000
"""
import os
import sys
import json
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from urllib.parse import quote

from flask import Flask, jsonify, request, render_template, Response, send_from_directory
import yamnet_revised as _ym
from yamnet_revised import RealtimeSoundClassifier, LOG_FILE

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CAP_DIR  = os.path.join(BASE_DIR, "captured_sounds")

app = Flask(__name__)

# ─── 전역 상태 ────────────────────────────────────────────────────────────────
_classifier = None   # RealtimeSoundClassifier | None
_thread = None       # threading.Thread | None

CONFIG = {
    "amplify":        _ym.AMPLIFY,
    "confidence_min": _ym.CONFIDENCE_MIN,
    "db_threshold":   _ym.DB_THRESHOLD,
}


def _is_running():
    return (
        _classifier is not None
        and not _classifier._stop_event.is_set()
        and _thread is not None
        and _thread.is_alive()
    )


def _run_thread():
    try:
        _classifier.run()
    except Exception as e:
        print(f"\n[대시보드] 감지 스레드 오류: {e}", file=sys.stderr)


# ─── REST API ─────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    running = _is_running()
    if running:
        c = _classifier
        with c.lock:
            st = c.state
            profile = _ym.NOISE_PROFILES.get(st.current_noise_type or "", {})
            data = {
                "running": True,
                "db": round(c.current_db, 1),
                "confidence": round(c.current_confidence * 100, 1),
                "label": c.current_label,
                "noise_type": st.current_noise_type,
                "is_masking": st.is_masking,
                "masking_noise": profile.get("masking") if st.is_masking else None,
            }
    else:
        data = {
            "running": False,
            "db": 0.0,
            "confidence": 0.0,
            "label": "",
            "noise_type": None,
            "is_masking": False,
            "masking_noise": None,
        }
    data["config"] = CONFIG
    return jsonify(data)


@app.route("/api/start", methods=["POST"])
def api_start():
    global _classifier, _thread
    if _is_running():
        return jsonify({"ok": False, "msg": "이미 실행 중입니다."})

    _ym.AMPLIFY        = CONFIG["amplify"]
    _ym.CONFIDENCE_MIN = CONFIG["confidence_min"]
    _ym.DB_THRESHOLD   = CONFIG["db_threshold"]

    try:
        _classifier = RealtimeSoundClassifier()
        _thread = threading.Thread(target=_run_thread, daemon=True)
        _thread.start()
        print("\n[대시보드] 감지 시스템 시작됨")
        return jsonify({"ok": True, "msg": "감지 시스템이 시작되었습니다."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global _classifier
    if not _is_running():
        return jsonify({"ok": False, "msg": "실행 중이 아닙니다."})
    _classifier.stop()
    print("\n[대시보드] 감지 시스템 중지 요청됨")
    return jsonify({"ok": True, "msg": "감지 시스템이 중지됩니다."})


@app.route("/api/config", methods=["POST"])
def api_config():
    body = request.get_json(silent=True) or {}
    if "amplify" in body:
        v = float(body["amplify"])
        if 0.5 <= v <= 10.0:
            CONFIG["amplify"] = v
            _ym.AMPLIFY = v
    if "confidence_min" in body:
        v = float(body["confidence_min"])
        if 0.0 < v <= 1.0:
            CONFIG["confidence_min"] = v
            _ym.CONFIDENCE_MIN = v
    if "db_threshold" in body:
        v = float(body["db_threshold"])
        if 10.0 <= v <= 100.0:
            CONFIG["db_threshold"] = v
            _ym.DB_THRESHOLD = v
    return jsonify({"ok": True, "config": CONFIG})


@app.route("/api/profiles", methods=["GET"])
def api_profiles_get():
    return jsonify({"profiles": _ym.NOISE_PROFILES})


@app.route("/api/profiles", methods=["POST"])
def api_profiles_post():
    """소음 유형별 프로파일 일괄 업데이트"""
    body = request.get_json(silent=True) or {}
    profiles = body.get("profiles", {})
    for noise_type, updates in profiles.items():
        if noise_type not in _ym.NOISE_PROFILES:
            continue
        p = _ym.NOISE_PROFILES[noise_type]
        if "masking"   in updates: p["masking"]   = str(updates["masking"])
        if "fade_in"   in updates: p["fade_in"]   = float(updates["fade_in"])
        if "fade_out"  in updates: p["fade_out"]  = float(updates["fade_out"])
        if "start_sec" in updates: p["start_sec"] = float(updates["start_sec"])
        if "stop_sec"  in updates: p["stop_sec"]  = float(updates["stop_sec"])
    return jsonify({"ok": True, "profiles": _ym.NOISE_PROFILES})


@app.route("/api/masking/play", methods=["POST"])
def api_masking_play():
    if not _is_running():
        return jsonify({"ok": False, "msg": "감지 시스템이 실행 중이 아닙니다."})
    body = request.get_json(silent=True) or {}
    noise_name = body.get("noise", "화이트")
    fade_in    = float(body.get("fade_in", 1.0))
    if noise_name not in ("브라운", "핑크", "화이트"):
        return jsonify({"ok": False, "msg": "noise 는 브라운/핑크/화이트 중 하나여야 합니다."})
    _classifier.masking_play(noise_name, fade_in)
    print(f"\n[대시보드] 수동 마스킹 재생: {noise_name}")
    return jsonify({"ok": True, "msg": f"{noise_name} 노이즈 재생 시작"})


@app.route("/api/masking/stop", methods=["POST"])
def api_masking_stop():
    if not _is_running():
        return jsonify({"ok": False, "msg": "감지 시스템이 실행 중이 아닙니다."})
    body = request.get_json(silent=True) or {}
    fade_out = float(body.get("fade_out", 1.0))
    _classifier.masking_stop_manual(fade_out)
    print("\n[대시보드] 수동 마스킹 정지")
    return jsonify({"ok": True, "msg": "마스킹 정지됨"})


@app.route("/api/logs")
def api_logs():
    logs = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-50:]:
                try:
                    logs.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
    return jsonify({"logs": list(reversed(logs))})


@app.route("/api/stream")
def api_stream():
    """SSE — 500ms 간격으로 실시간 상태 전송"""
    def generate():
        while True:
            running = _is_running()
            if running:
                c = _classifier
                with c.lock:
                    st = c.state
                    data = {
                        "running": True,
                        "db": round(c.current_db, 1),
                        "confidence": round(c.current_confidence * 100, 1),
                        "label": c.current_label,
                        "noise_type": st.current_noise_type,
                        "is_masking": st.is_masking,
                    }
            else:
                data = {
                    "running": False, "db": 0.0, "confidence": 0.0,
                    "label": "", "noise_type": None, "is_masking": False,
                }
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/captures")
def api_captures():
    """captured_sounds 폴더의 PNG+WAV 목록 반환 (최신순 30건)"""
    captures = []
    if not os.path.exists(CAP_DIR):
        return jsonify({"captures": []})
    try:
        for noise_type in sorted(os.listdir(CAP_DIR)):
            type_dir = os.path.join(CAP_DIR, noise_type)
            if not os.path.isdir(type_dir):
                continue
            bases = {}
            for fname in sorted(os.listdir(type_dir)):
                base, ext = os.path.splitext(fname)
                if ext.lower() not in (".png", ".wav"):
                    continue
                if base not in bases:
                    bases[base] = {"name": base, "noise_type": noise_type}
                # URL 경로: 공백·한글 등을 percent-encode, '/' 는 유지
                safe_path = quote(noise_type, safe="") + "/" + quote(fname, safe="")
                if ext.lower() == ".png":
                    bases[base]["image"] = safe_path
                else:
                    bases[base]["audio"] = safe_path
            captures.extend(bases.values())
    except Exception as e:
        print(f"[캡처 목록 오류] {e}", file=sys.stderr)
    captures.sort(key=lambda x: x["name"], reverse=True)
    return jsonify({"captures": captures[:30]})


@app.route("/captures/<path:filename>")
def serve_capture(filename):
    return send_from_directory(CAP_DIR, filename)


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    if not os.path.exists("yamnet.onnx") or not os.path.exists("yamnet_class_map.csv"):
        print("에러: yamnet.onnx 또는 yamnet_class_map.csv 가 없습니다.")
        sys.exit(1)

    print("=" * 55)
    print("  LG PuriSound — YAMNet 소음 감지 제어 서버")
    print("  접속 주소: http://0.0.0.0:5000")
    print("  (라즈베리파이: http://<IP주소>:5000)")
    print("  종료: Ctrl+C")
    print("=" * 55)

    app.run(host="0.0.0.0", port=5000, debug=False,
            threaded=True, use_reloader=False)
