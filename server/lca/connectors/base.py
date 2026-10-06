"""Interfaces isolating external platform clients from the product core."""

from __future__ import annotations

from enum import StrEnum
from typing import Callable, Protocol

from ..models import ChatEvent


class ConnectorState(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"
    DISCONNECTED = "disconnected"


EventCallback = Callable[[ChatEvent], None]
StatusCallback = Callable[[ConnectorState, str | None], None]


class Connector(Protocol):
    def start(self, on_event: EventCallback, on_status: StatusCallback) -> None:
        """Start without blocking until the remote stream ends."""

    def stop(self) -> None:
        """Request connector shutdown."""


class ConnectorFactory(Protocol):
    def create(self, platform: str, channel_id: str) -> Connector:
        """Build a connector without initiating a network call."""
