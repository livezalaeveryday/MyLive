"""Lazy adapter selection keeps application import/start offline-safe."""

from __future__ import annotations

from .base import Connector


class DefaultConnectorFactory:
    def create(self, platform: str, channel_id: str) -> Connector:
        if platform == "tiktok":
            from .tiktok import TikTokConnector

            return TikTokConnector(channel_id)
        if platform == "youtube":
            from .youtube import YouTubeConnector

            return YouTubeConnector(channel_id)
        raise ValueError(f"Unsupported platform: {platform}")
