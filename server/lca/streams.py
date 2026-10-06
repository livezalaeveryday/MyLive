"""Session-isolated stream lifecycle service."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any
import uuid

from flask_socketio import SocketIO

from .auth import SecurityContext
from .connectors import Connector, ConnectorFactory, ConnectorState
from .control_plane import ControlPlane
from .models import ChatEvent, normalize_channel_id, normalize_platform


class StreamError(RuntimeError):
    pass


class StreamConflict(StreamError):
    pass


class StreamNotFound(StreamError):
    pass


class StreamForbidden(StreamError):
    pass


@dataclass(slots=True)
class StreamRecord:
    stream_id: str
    session_id: str
    platform: str
    channel_id: str
    connector: Connector
    state: ConnectorState = ConnectorState.CONNECTING
    detail: str | None = None
    created_at: float = 0.0
    reconnect_attempt: int = 0
    reconnect_scheduled: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "streamId": self.stream_id,
            "platform": self.platform,
            "channelId": self.channel_id,
            "state": self.state.value,
            "detail": self.detail,
            "createdAt": self.created_at,
        }


class StreamService:
    def __init__(self, socketio: SocketIO, factory: ConnectorFactory, control_plane: ControlPlane):
        self._socketio = socketio
        self._factory = factory
        self._control_plane = control_plane
        self._records: dict[tuple[str, str, str], StreamRecord] = {}
        self._lock = threading.RLock()

    def connect(self, context: SecurityContext, platform: Any, channel_id: Any) -> StreamRecord:
        normalized_platform = normalize_platform(platform)
        normalized_channel = normalize_channel_id(normalized_platform, channel_id)
        decision = self._control_plane.authorize_stream(context.device_id, normalized_platform)
        if not decision.allowed:
            raise StreamForbidden(decision.reason or "Feature is not available for this device")

        key = (context.session_id, normalized_platform, normalized_channel)
        with self._lock:
            if key in self._records:
                raise StreamConflict("Stream already exists in this session")
            connector = self._factory.create(normalized_platform, normalized_channel)
            record = StreamRecord(
                stream_id=str(uuid.uuid4()),
                session_id=context.session_id,
                platform=normalized_platform,
                channel_id=normalized_channel,
                connector=connector,
                created_at=time.time(),
            )
            self._records[key] = record

        def on_event(event: ChatEvent) -> None:
            with self._lock:
                current = self._records.get(key)
                if current is not record:
                    return
                if event.platform != record.platform or event.channel_id != record.channel_id:
                    return
            self._socketio.emit("chat_message", event.to_dict(), to=context.room)

        def on_status(state: ConnectorState, detail: str | None) -> None:
            with self._lock:
                current = self._records.get(key)
                if current is not record:
                    return
                record.state = state
                record.detail = detail
                if state == ConnectorState.CONNECTED:
                    record.reconnect_attempt = 0
                    record.reconnect_scheduled = False
                payload = record.public()
            self._socketio.emit("stream_status", payload, to=context.room)
            if state in {ConnectorState.DISCONNECTED, ConnectorState.FAILED}:
                self._schedule_reconnect(key, record, context, on_event, on_status)

        try:
            connector.start(on_event, on_status)
        except Exception:
            with self._lock:
                self._records.pop(key, None)
            try:
                connector.stop()
            except Exception:
                pass
            raise
        return record

    def _schedule_reconnect(
        self,
        key: tuple[str, str, str],
        record: StreamRecord,
        context: SecurityContext,
        on_event: Any,
        on_status: Any,
    ) -> None:
        with self._lock:
            if self._records.get(key) is not record or record.reconnect_scheduled:
                return
            record.reconnect_scheduled = True
            record.reconnect_attempt += 1
            attempt = record.reconnect_attempt
            delay = min(60, 3 * (2 ** min(attempt - 1, 4)))
            record.state = ConnectorState.CONNECTING
            record.detail = f"Reconnecting in {delay}s (attempt {attempt})"
            payload = record.public()
        self._socketio.emit("stream_status", payload, to=context.room)

        def reconnect() -> None:
            with self._lock:
                if self._records.get(key) is not record:
                    return
                record.reconnect_scheduled = False
                connector = record.connector
            try:
                connector.start(on_event, on_status)
            except Exception as exc:
                with self._lock:
                    if self._records.get(key) is not record:
                        return
                    record.state = ConnectorState.FAILED
                    record.detail = f"Reconnect failed: {type(exc).__name__}"
                self._socketio.emit("stream_status", record.public(), to=context.room)
                self._schedule_reconnect(key, record, context, on_event, on_status)

        threading.Timer(delay, reconnect).start()

    def disconnect(self, context: SecurityContext, platform: Any, channel_id: Any) -> StreamRecord:
        normalized_platform = normalize_platform(platform)
        normalized_channel = normalize_channel_id(normalized_platform, channel_id)
        key = (context.session_id, normalized_platform, normalized_channel)
        with self._lock:
            record = self._records.pop(key, None)
        if record is None:
            raise StreamNotFound("Stream not found in this session")
        try:
            record.connector.stop()
        finally:
            record.state = ConnectorState.DISCONNECTED
            record.detail = "Disconnected by user"
            self._socketio.emit("stream_status", record.public(), to=context.room)
        return record

    def list_for(self, context: SecurityContext) -> list[dict[str, Any]]:
        with self._lock:
            records = [record.public() for key, record in self._records.items() if key[0] == context.session_id]
        return sorted(records, key=lambda item: item["createdAt"])

    def shutdown(self) -> None:
        with self._lock:
            records = list(self._records.values())
            self._records.clear()
        for record in records:
            try:
                record.connector.stop()
            except Exception:
                pass
