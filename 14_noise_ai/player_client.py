"""14_noise_ai → 15_player_ai 재생 명령 (Firebase playbackCommands + IPC 폴백)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from firebase_client import write_playback_command
from player_command import build_command, write_command

if TYPE_CHECKING:
    from masking_decider import MaskingDecision


class PlayerCommandWriter:
    def __init__(
        self,
        *,
        device_id: str | None = None,
        use_firestore: bool = True,
        publish_firebase: bool = True,
        write_ipc: bool = True,
    ) -> None:
        self._seq = 0
        self._device_id = device_id
        self._use_firestore = use_firestore
        self._publish_firebase = publish_firebase
        self._write_ipc = write_ipc

    def _destination_label(self) -> str:
        if self._publish_firebase and self._device_id:
            storage = (
                "playbackCommands/latest"
                if self._use_firestore
                else "playbackCommand"
            )
            parts = [f"Firebase devices/{self._device_id}/{storage}"]
        else:
            parts = []
        if self._write_ipc:
            parts.append("/tmp/player_ai_command.json")
        return " + ".join(parts) if parts else "(출력 없음)"

    def send_decision(self, decision: MaskingDecision) -> None:
        self._seq += 1

        if decision.action == "stop" or not decision.tracks:
            command = build_command(seq=self._seq, action="stop")
            self._publish(command)
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
        self._publish(command)

        names = ", ".join(
            f"{track.file_name} ({track.volume:.0%})" for track in decision.tracks
        )
        print(
            f"  → 명령 전송: 재생 — {names} ({self._destination_label()})",
            flush=True,
        )

    def _publish(self, command: dict[str, Any]) -> None:
        if self._publish_firebase:
            if not self._device_id:
                raise RuntimeError("Firebase 명령 전송에 device_id가 필요합니다.")
            write_playback_command(
                self._device_id,
                command,
                use_firestore=self._use_firestore,
            )

        if self._write_ipc:
            write_command(command)

    def close(self) -> None:
        return