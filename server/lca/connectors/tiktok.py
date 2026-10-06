"""TikTok adapter. Imports and network work occur only after start."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from .base import EventCallback, StatusCallback, ConnectorState
from ..models import ChatEvent, ValidationError


log = logging.getLogger(__name__)


class TikTokConnector:
    def __init__(self, channel_id: str):
        self.channel_id = channel_id
        self._client: Any = None
        self._thread: threading.Thread | None = None

    def start(self, on_event: EventCallback, on_status: StatusCallback) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("TikTok connector is already running")

        def run() -> None:
            try:
                from TikTokLive import TikTokLiveClient
                from TikTokLive.events import CommentEvent, ConnectEvent, GiftEvent

                client = TikTokLiveClient(unique_id=self.channel_id)
                self._client = client

                @client.on(ConnectEvent)
                async def on_connect(event: Any) -> None:
                    del event
                    on_status(ConnectorState.CONNECTED, None)

                @client.on(CommentEvent)
                async def on_comment(event: Any) -> None:
                    try:
                        user = event.user
                        author = getattr(user, "nickname", None) or getattr(user, "unique_id", "Unknown")
                        avatar = None
                        avatar_thumb = getattr(user, "avatar_thumb", None)
                        avatar_list = getattr(avatar_thumb, "url_list", None)
                        if avatar_list:
                            avatar = avatar_list[0]
                        on_event(
                            ChatEvent.create(
                                platform="tiktok",
                                channel_id=self.channel_id,
                                author=str(author),
                                avatar=avatar,
                                comment=str(event.comment),
                            )
                        )
                    except ValidationError as exc:
                        log.warning("Dropped invalid TikTok event: %s", exc)
                    except Exception:
                        log.exception("TikTok event handling failed")

                @client.on(GiftEvent)
                async def on_gift(event: Any) -> None:
                    try:
                        user = event.user
                        author = getattr(user, "nickname", None) or getattr(user, "unique_id", "Unknown")
                        gift = getattr(event, "gift", None)
                        gift_name = getattr(gift, "name", None) or getattr(event, "gift_name", None) or "Gift"
                        count = getattr(event, "repeat_count", None) or getattr(event, "count", None) or 1
                        on_event(ChatEvent.create(
                            platform="tiktok", channel_id=self.channel_id, author=str(author),
                            avatar=None, comment=f"ส่ง {gift_name} x{count}",
                            event_type="gift", gift_name=str(gift_name), gift_count=count,
                        ))
                    except Exception:
                        log.exception("TikTok gift handling failed")

                asyncio.set_event_loop(asyncio.new_event_loop())
                client.run()
                on_status(ConnectorState.DISCONNECTED, "Remote stream ended")
            except Exception as exc:
                log.warning("TikTok connector failed: %s", type(exc).__name__)
                on_status(ConnectorState.FAILED, "Unable to connect to TikTok LIVE")

        self._thread = threading.Thread(target=run, name=f"tiktok-{self.channel_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        client = self._client
        if client is None:
            return
        for method_name in ("disconnect", "stop"):
            method = getattr(client, method_name, None)
            if callable(method):
                try:
                    result = method()
                    if asyncio.iscoroutine(result):
                        result.close()
                except Exception:
                    log.warning("TikTok shutdown method failed", exc_info=True)
                break
