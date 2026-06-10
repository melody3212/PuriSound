"""
monitor.py — PuriSound 실시간 CMD 모니터
사용법: python monitor.py [서버주소]
예)    python monitor.py               # 기본: localhost:5000
       python monitor.py 192.168.0.91  # 라즈베리파이 IP
"""
import sys
import time
import json
import urllib.request
import urllib.error
import os

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
BASE = f"http://{HOST}:{PORT}"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# ── 터미널 너비 ────────────────────────────────────────────────────────────────
def term_width():
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 80

# ── dB → 막대 ─────────────────────────────────────────────────────────────────
def db_bar(db, width=20):
    pct = max(0.0, min(1.0, (db - 30) / 70))
    filled = int(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    color = "\033[91m" if db > 40 else "\033[94m"  # red / blue
    return f"{color}[{bar}]\033[0m"

# ── 신뢰도 → 막대 ──────────────────────────────────────────────────────────────
def conf_bar(pct, width=10):
    filled = int(min(pct, 100) / 100 * width)
    return "▓" * filled + "░" * (width - filled)

# ── API 호출 ──────────────────────────────────────────────────────────────────
def fetch(path, timeout=2):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

# ── 로그 표시 (최근 5건) ──────────────────────────────────────────────────────
SEEN_LOGS = set()

def print_new_logs(logs):
    new = []
    for log in reversed(logs[:10]):
        key = (log.get("timestamp"), log.get("event"))
        if key not in SEEN_LOGS:
            SEEN_LOGS.add(key)
            new.append(log)
    for log in reversed(new):
        ts  = (log.get("timestamp") or "")[:19].replace("T", " ")
        ev  = log.get("event", "")
        nt  = log.get("noise_type", "")
        db  = log.get("db")
        mn  = log.get("masking_noise", "")
        auto = "★ " if log.get("auto_matched") else ""

        if ev == "noise_detected":
            tag = "\033[93m[소음감지]\033[0m"
            print(f"  {ts}  {tag}  {nt}  {db:.1f}dB")
        elif ev == "masking_start":
            tag = "\033[92m[마스킹 시작]\033[0m"
            print(f"  {ts}  {tag}  {auto}{mn}  ← {nt}")
        elif ev == "masking_stop":
            tag = "\033[90m[마스킹 종료]\033[0m"
            print(f"  {ts}  {tag}  {nt}")

# ── 메인 루프 ─────────────────────────────────────────────────────────────────
def main():
    print(f"\033[96m PuriSound CMD 모니터  →  {BASE}\033[0m")
    print(" Ctrl+C 로 종료\n")

    prev_running = None
    interval = 0.5

    while True:
        d = fetch("/api/status")

        if d is None:
            sys.stdout.write("\r\033[91m  서버에 연결할 수 없습니다. 재시도 중...\033[0m          ")
            sys.stdout.flush()
            time.sleep(2)
            continue

        running = d.get("running", False)

        # 상태 전환 시 빈 줄
        if prev_running is not None and prev_running != running:
            print()
        prev_running = running

        if not running:
            sys.stdout.write("\r\033[90m  ■ 감지 시스템 중지됨  (대시보드에서 시작하세요)\033[0m        ")
            sys.stdout.flush()
            time.sleep(1)
            continue

        db        = d.get("db", 0.0)
        conf      = d.get("confidence", 0.0)
        label     = (d.get("label") or "")[:28]
        noise     = d.get("noise_type") or "—"
        masking   = d.get("is_masking", False)

        mask_str = ""
        if masking:
            scores = d.get("match_scores") or {}
            if scores:
                best = max(scores, key=scores.get)
                score_pct = int(scores[best] * 100)
                mask_str = f"\033[92m▶ {best[:30]}  ({score_pct}%)\033[0m"
            else:
                mask_str = "\033[92m▶ 마스킹 재생 중\033[0m"
        else:
            mask_str = "\033[90m○ 마스킹 없음\033[0m"

        line = (
            f"\r  {db_bar(db)}  {db:6.1f}dB  "
            f"{conf_bar(conf):10s}  {conf:5.1f}%  "
            f"\033[97m{label:<28}\033[0m  {noise:<12}  {mask_str}"
        )
        sys.stdout.write(line)
        sys.stdout.flush()

        # 새 이벤트 로그 확인 (2초마다)
        if int(time.time() * 2) % 4 == 0:
            logs_d = fetch("/api/logs")
            if logs_d:
                logs = logs_d.get("logs", [])
                if logs:
                    sys.stdout.write("\n")
                    print_new_logs(logs)

        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n모니터 종료.")
