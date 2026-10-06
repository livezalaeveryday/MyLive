from __future__ import annotations

from typing import Any

from lca.connectors import ConnectorState


class FakeConnector:
    def __init__(self, platform: str, channel_id: str):
        self.platform = platform
        self.channel_id = channel_id
        self.on_event: Any = None
        self.on_status: Any = None
        self.stopped = False

    def start(self, on_event: Any, on_status: Any) -> None:
        self.on_event = on_event
        self.on_status = on_status

    def stop(self) -> None:
        self.stopped = True

    def connected(self) -> None:
        self.on_status(ConnectorState.CONNECTED, None)

    def emit(self, event: Any) -> None:
        self.on_event(event)


class FakeConnectorFactory:
    def __init__(self):
        self.created: list[FakeConnector] = []

    def create(self, platform: str, channel_id: str) -> FakeConnector:
        connector = FakeConnector(platform, channel_id)
        self.created.append(connector)
        return connector
