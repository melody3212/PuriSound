"""9_send_firebase → 15_player_ai·17_server 재생 명령 전송."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import requests
from firebase_admin import db, firestore
from player_command import build_command, write_command
from server_client import DEFAULT_SERVER_URL, post_playback_command

if TYPE_CHECKING:
    from masking_decider import MaskingDecision

PLAYBACK_COMMAND_DOC = "latest"


def write_playback_command(
    device_id: str,
    command: dict[str, Any],
    *,
    use_firestore: bool,
) -> None:
    """마스킹 결정 → 15번 재생기용 Firebase 명령 기록."""
    if use_firestore:
        (
            firestore.client()
            .collection("devices")
            .document(device_id)
            .collection("playbackCommands")
            .document(PLAYBACK_COMMAND_DOC)
            .set(command)
        )
        return
    db.reference(f"devices/{device_id}/playbackCommand").set(command)


class PlayerCommandWriter:
    def __init__(
        self,
        *,
        device_id: str | None = None,
        use_firestore: bool = True,
        publish_firebase: bool = True,
        write_ipc: bool = True,
        server_url: str | None = DEFAULT_SERVER_URL,
        publish_server: bool = True,
    ) -> None:
        self._seq = 0
        self._device_id = device_id
        self._use_firestore = use_firestore
        self._publish_firebase = publish_firebase
        self._write_ipc = write_ipc
        self._server_url = server_url.rstrip("/") if server_url else None
        self._publish_server = publish_server

    def _destination_label(self) -> str:
        parts: list[str] = []
        if self._publish_firebase and self._device_id:
            storage = (
                "playbackCommands/latest"
                if self._use_firestore
                else "playbackCommand"
            )
            parts.append(f"Firebase devices/{self._device_id}/{storage}")
        if self._publish_server and self._server_url:
            parts.append(f"17_server {self._server_url}/api/playback-commands")
        if self._write_ipc:
            parts.append("/tmp/player_ai_command.json")
        return " + ".join(parts) if parts else "(출력 없음)"

    def send_decision(
        self,
        decision: MaskingDecision,
        *,
        noise_event_id: str | None = None,
        detected_at: Any = None,
    ) -> None:
        self._seq += 1

        if decision.action == "stop" or not decision.tracks:
            command = build_command(seq=self._seq, action="stop")
            self._publish(
                command,
                decision=decision,
                noise_event_id=noise_event_id,
                detected_at=detected_at,
            )
            print(
                f"  → 명령 전송: 정지 ({self._destination_label()})",
                flush=True,
            )
            return

        tracks = [
            {
                "path": str(track.file_path),
                "name": track.file_name,
                "volume": track.volume,
                "noise_type": decision.noise_type,
            }
            for track in decision.tracks
        ]
        command = build_command(seq=self._seq, action="play", tracks=tracks)
        self._publish(
            command,
            decision=decision,
            noise_event_id=noise_event_id,
            detected_at=detected_at,
        )

        names = ", ".join(
            f"{track.file_name} ({track.volume:.0%})" for track in decision.tracks
        )
        print(
            f"  → 명령 전송: 재생 — {names} ({self._destination_label()})",
            flush=True,
        )

    def _publish(
        self,
        command: dict[str, Any],
        *,
        decision: MaskingDecision,
        noise_event_id: str | None = None,
        detected_at: Any = None,
    ) -> None:
        if self._publish_firebase:
            if not self._device_id:
                raise RuntimeError("Firebase 명령 전송에 device_id가 필요합니다.")
            write_playback_command(
                self._device_id,
                command,
                use_firestore=self._use_firestore,
            )

        if self._publish_server and self._server_url:
            if not self._device_id:
                raise RuntimeError("17_server 저장에 device_id가 필요합니다.")
            try:
                post_playback_command(
                    self._server_url,
                    device_id=self._device_id,
                    command=command,
                    decision=decision,
                    noise_event_id=noise_event_id,
                    detected_at=detected_at,
                )
            except requests.RequestException as exc:
                print(f"  [17_server 저장 실패] {exc}", flush=True)

        if self._write_ipc:
            write_command(command)