#!/usr/bin/env python3
"""send_firebase.py 동작 상태를 로컬에서 실시간으로 확인합니다."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

_SCRIPT_DIR = Path(__file__).resolve().parent
_PIDFILE = _SCRIPT_DIR / ".send_firebase.pid"
_LOG_PATH = _SCRIPT_DIR / "send_firebase.log"
_SERVICE_NAME = "send-firebase.service"

_DATA_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DATA_ROOT))
from puri_env import DEFAULT_YAMNET_URL  # noqa: E402

DEFAULT_SERVER_URL = "http://127.0.0.1:5000"
DEFAULT_STALE_SEC = 20.0
DEFAULT_INTERVAL = 2.0

_SEPARATOR = "─" * 50
_SEND_RE = re.compile(r"noiseEvents 전송\s*│\s*(.+)")
_PATH_RE = re.compile(r"path\s+:\s*devices/.+/noiseEvents/(\S+)")
_DB_RE = re.compile(r"db\s+:\s*(\d+)")
_LABEL_RE = re.compile(r"분류\s+:\s*(.+)")
_MASKING_RE = re.compile(r"마스킹\s+:\s*(.+)")
_DECISION_RE = re.compile(r"결정\s+:\s*(.+)")
_YAMNET_RE = re.compile(r"YAMNet\s+:\s*(.+)")
_MIC_RE = re.compile(r"마이크 녹음 시작:\s*(.+)")
_ERROR_RE = re.compile(r"\[(.+실패|오류[^\]]*)\]")

running = True


def stop(_signum=None, _frame=None) -> None:
    global running
    running = False


@dataclass
class ProcessStatus:
    alive: bool = False
    pid: int | None = None
    cmdline: str | None = None
    started_at: float | None = None
    pidfile_stale: bool = False


@dataclass
class ServiceStatus:
    active: bool | None = None
    enabled: bool | None = None
    state: str = "확인 불가"


@dataclass
class LogSnapshot:
    log_exists: bool = False
    log_mtime: float | None = None
    mic_device: str | None = None
    last_send_at: datetime | None = None
    event_id: str | None = None
    db: int | None = None
    label: str | None = None
    masking: str | None = None
    decision: str | None = None
    yamnet_line: str | None = None
    recent_errors: list[str] = field(default_factory=list)
    wind_skips: int = 0


@dataclass
class HealthReport:
    level: str
    message: str
    process: ProcessStatus
    service: ServiceStatus
    log: LogSnapshot
    yamnet_online: bool | None = None
    yamnet_detail: str = ""
    server_online: bool | None = None
    server_detail: str = ""


def parse_detected_at(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_log_tail(path: Path, max_bytes: int = 98_304) -> str:
    if not path.is_file():
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def _parse_block(block: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for line in block.splitlines():
        match = _SEND_RE.search(line)
        if match:
            data["detected_at"] = parse_detected_at(match.group(1))
            continue
        for key, pattern, cast in (
            ("event_id", _PATH_RE, str),
            ("db", _DB_RE, int),
            ("label", _LABEL_RE, str),
            ("masking", _MASKING_RE, str),
            ("decision", _DECISION_RE, str),
            ("yamnet_line", _YAMNET_RE, str),
        ):
            match = pattern.search(line)
            if match:
                data[key] = cast(match.group(1).strip())
    return data


def parse_log_snapshot(path: Path) -> LogSnapshot:
    snapshot = LogSnapshot(log_exists=path.is_file())
    if not snapshot.log_exists:
        return snapshot

    stat = path.stat()
    snapshot.log_mtime = stat.st_mtime

    text = read_log_tail(path)
    if not text:
        return snapshot

    mic_match = None
    for match in _MIC_RE.finditer(text):
        mic_match = match
    if mic_match:
        snapshot.mic_device = mic_match.group(1).strip()

    snapshot.wind_skips = text.count("[바람소리 감지]")

    blocks = text.split(_SEPARATOR)
    for block in reversed(blocks):
        if "noiseEvents 전송" not in block:
            continue
        parsed = _parse_block(block)
        snapshot.last_send_at = parsed.get("detected_at")
        snapshot.event_id = parsed.get("event_id")
        snapshot.db = parsed.get("db")
        snapshot.label = parsed.get("label")
        snapshot.masking = parsed.get("masking")
        snapshot.decision = parsed.get("decision")
        snapshot.yamnet_line = parsed.get("yamnet_line")
        break

    seen: set[str] = set()
    for line in reversed(text.splitlines()[-40:]):
        match = _ERROR_RE.search(line)
        if not match:
            continue
        msg = line.strip()
        if msg in seen:
            continue
        seen.add(msg)
        snapshot.recent_errors.append(msg)
        if len(snapshot.recent_errors) >= 5:
            break
    snapshot.recent_errors.reverse()
    return snapshot


def read_process_status(pidfile: Path) -> ProcessStatus:
    status = ProcessStatus()
    if not pidfile.is_file():
        return status

    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        status.pidfile_stale = True
        return status

    status.pid = pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        status.pidfile_stale = True
        return status
    except PermissionError:
        status.alive = True
        return status

    status.alive = True
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if cmdline_path.is_file():
        raw = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        status.cmdline = raw.strip() or None
        if status.cmdline and "send_firebase" not in status.cmdline:
            status.alive = False

    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        parts = stat_path.read_text().split()
        if len(parts) > 21:
            start_ticks = int(parts[21])
            clk_tck = os.sysconf("SC_CLK_TCK")
            boot_time = _boot_time()
            if boot_time is not None:
                status.started_at = boot_time + start_ticks / clk_tck

    return status


def _boot_time() -> float | None:
    try:
        text = Path("/proc/stat").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return None


def read_service_status(service_name: str) -> ServiceStatus:
    status = ServiceStatus()
    try:
        active = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        enabled = subprocess.run(
            ["systemctl", "is-enabled", service_name],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return status

    active_state = active.stdout.strip()
    enabled_state = enabled.stdout.strip()

    if active_state == "active":
        status.active = True
        status.state = "active"
    elif active_state:
        status.active = False
        status.state = active_state
    else:
        status.active = False
        status.state = active.stderr.strip() or "inactive"

    if enabled_state == "enabled":
        status.enabled = True
    elif enabled_state in {"disabled", "masked", "static"}:
        status.enabled = False
    return status


def check_http_service(base_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    url = base_url.rstrip("/")
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code < 500:
            return True, f"켜짐 ({url})"
        return False, f"응답 오류 (HTTP {response.status_code})"
    except requests.ConnectionError:
        return False, "꺼짐 (연결 실패)"
    except requests.Timeout:
        return False, "꺼짐 (응답 시간 초과)"
    except requests.RequestException as exc:
        return False, f"꺼짐 ({exc})"


def _age_seconds(value: float | datetime | None, now: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return max(0.0, now - value.timestamp())
    return max(0.0, now - value)


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}초 전"
    return _format_duration(seconds) + " 전"


def build_health_report(
    *,
    pidfile: Path,
    log_path: Path,
    stale_sec: float,
    check_yamnet: bool,
    yamnet_url: str,
    check_server: bool,
    server_url: str,
) -> HealthReport:
    now = time.time()
    process = read_process_status(pidfile)
    service = read_service_status(_SERVICE_NAME)
    log = parse_log_snapshot(log_path)

    yamnet_online: bool | None = None
    yamnet_detail = ""
    if check_yamnet:
        yamnet_online, yamnet_detail = check_http_service(yamnet_url)

    server_online: bool | None = None
    server_detail = ""
    if check_server:
        server_online, server_detail = check_http_service(server_url)

    log_age = _age_seconds(log.log_mtime, now)
    send_age = _age_seconds(log.last_send_at, now)

    if not process.alive:
        if process.pidfile_stale:
            message = "PID 파일만 남아 있습니다. send_firebase가 중지된 상태입니다."
        else:
            message = "send_firebase 프로세스가 실행 중이 아닙니다."
        level = "DOWN"
    elif log_age is None or log_age > stale_sec:
        message = f"로그 갱신이 {log_age or 0:.0f}초 전입니다. 전송 루프가 멈췄을 수 있습니다."
        level = "STALE"
    elif send_age is not None and send_age > stale_sec * 2:
        message = (
            f"프로세스는 살아 있으나 마지막 Firebase 전송이 "
            f"{send_age:.0f}초 전입니다."
        )
        level = "WARN"
    elif log.recent_errors:
        message = "전송은 진행 중이나 최근 오류가 있습니다."
        level = "WARN"
    else:
        message = "정상 동작 중입니다."
        level = "OK"

    return HealthReport(
        level=level,
        message=message,
        process=process,
        service=service,
        log=log,
        yamnet_online=yamnet_online,
        yamnet_detail=yamnet_detail,
        server_online=server_online,
        server_detail=server_detail,
    )


def _level_symbol(level: str) -> str:
    return {
        "OK": "●",
        "WARN": "▲",
        "STALE": "▲",
        "DOWN": "■",
    }.get(level, "?")


def _level_label(level: str) -> str:
    return {
        "OK": "정상",
        "WARN": "주의",
        "STALE": "지연",
        "DOWN": "중지",
    }.get(level, level)


def _bool_text(value: bool | None, true_text: str, false_text: str) -> str:
    if value is True:
        return true_text
    if value is False:
        return false_text
    return "확인 불가"


def format_report(report: HealthReport) -> str:
    now = time.time()
    lines = [
        "=== send_firebase 상태 뷰어 ===",
        f"갱신: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"[{_level_symbol(report.level)}] 전체 상태: {_level_label(report.level)} — {report.message}",
        "",
        "프로세스",
    ]

    if report.process.alive and report.process.pid:
        uptime = _format_duration(_age_seconds(report.process.started_at, now))
        lines.append(f"  PID      : {report.process.pid} (실행 중)")
        lines.append(f"  가동 시간 : {uptime}")
    elif report.process.pidfile_stale and report.process.pid:
        lines.append(f"  PID 파일 : {report.process.pid} (프로세스 없음)")
    else:
        lines.append("  PID      : 없음")

    if report.service.active is not None:
        enabled = _bool_text(report.service.enabled, "enabled", "disabled")
        lines.append(f"  systemd  : {report.service.state} ({enabled})")
    else:
        lines.append("  systemd  : 확인 불가")

    lines.extend(["", "마이크 / 전송"])
    lines.append(f"  장치     : {report.log.mic_device or '-'}")
    if report.log.last_send_at:
        lines.append(
            "  마지막 전송: "
            f"{_format_age(_age_seconds(report.log.last_send_at, now))} "
            f"({report.log.last_send_at.isoformat()})"
        )
    else:
        lines.append("  마지막 전송: 없음")
    lines.append(f"  이벤트 ID : {report.log.event_id or '-'}")
    lines.append(
        "  측정값   : "
        f"db {report.log.db if report.log.db is not None else '-'} | "
        f"분류 {report.log.label or '-'} | "
        f"마스킹 {report.log.masking or '-'}"
    )
    lines.append(f"  결정     : {report.log.decision or '-'}")
    if report.log.yamnet_line:
        lines.append(f"  YAMNet   : {report.log.yamnet_line}")
    if report.log.wind_skips:
        lines.append(f"  바람 필터: 최근 로그에 생략 {report.log.wind_skips}회")

    lines.extend(["", "연동 서비스"])
    if report.yamnet_online is not None:
        lines.append(f"  YAMNet   : {report.yamnet_detail}")
    if report.server_online is not None:
        lines.append(f"  17_server: {report.server_detail}")

    lines.extend(["", "최근 경고"])
    if report.log.recent_errors:
        for item in report.log.recent_errors:
            lines.append(f"  · {item}")
    else:
        lines.append("  · 없음")

    lines.extend(["", "종료: Ctrl+C"])
    return "\n".join(lines)


def render(report: HealthReport, *, clear: bool) -> None:
    text = format_report(report)
    if clear and sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
    print(text, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="send_firebase.py 프로세스·로그·연동 서비스 상태를 표시합니다.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"갱신 주기 초 (기본 {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=_LOG_PATH,
        help=f"send_firebase 로그 경로 (기본 {_LOG_PATH})",
    )
    parser.add_argument(
        "--pidfile",
        type=Path,
        default=_PIDFILE,
        help=f"PID 파일 경로 (기본 {_PIDFILE})",
    )
    parser.add_argument(
        "--stale-sec",
        type=float,
        default=DEFAULT_STALE_SEC,
        help=f"로그 미갱신 허용 초 (기본 {DEFAULT_STALE_SEC})",
    )
    parser.add_argument(
        "--yamnet-url",
        default=DEFAULT_YAMNET_URL,
        help=f"YAMNet 서버 URL (기본 {DEFAULT_YAMNET_URL})",
    )
    parser.add_argument(
        "--no-yamnet",
        action="store_true",
        help="YAMNet 서버 상태 확인 생략",
    )
    parser.add_argument(
        "--server-url",
        default=DEFAULT_SERVER_URL,
        help=f"17_server URL (기본 {DEFAULT_SERVER_URL})",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="17_server 상태 확인 생략",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="한 번만 출력하고 종료",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="화면을 지우지 않고 이어서 출력",
    )
    return parser


def evaluate_exit_code(level: str) -> int:
    return 0 if level == "OK" else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    last_level = "DOWN"
    while running:
        report = build_health_report(
            pidfile=args.pidfile,
            log_path=args.log,
            stale_sec=args.stale_sec,
            check_yamnet=not args.no_yamnet,
            yamnet_url=args.yamnet_url,
            check_server=not args.no_server,
            server_url=args.server_url,
        )
        last_level = report.level
        render(report, clear=not args.no_clear and not args.once)

        if args.once:
            break

        deadline = time.monotonic() + args.interval
        while running and time.monotonic() < deadline:
            time.sleep(0.1)

    if not args.once:
        print("\n종료합니다.")
    return evaluate_exit_code(last_level)


if __name__ == "__main__":
    raise SystemExit(main())