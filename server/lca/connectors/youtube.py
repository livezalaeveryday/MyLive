"""YouTube adapter. Imports and network work occur only after start."""

from __future__ import annotations

import logging
import threading
from typing import Any

from .base import EventCallback, StatusCallback, ConnectorState
from ..models import ChatEvent, ValidationError


log = logging.getLogger(__name__)


class YouTubeConnector:
    def __init__(self, video_id: str):
        self.video_id = video_id
        self._chat: Any = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self, on_event: EventCallback, on_status: StatusCallback) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("YouTube connector is already running")
        self._stop_event.clear()

        def run() -> None:
            try:
                import pytchat

                video_id = self.video_id
                if video_id.startswith("@"):
                    from .youtube_resolver import resolve_live_video

                    on_status(ConnectorState.CONNECTING, "Finding the current YouTube Live…")
                    video_id = resolve_live_video(video_id)

                chat = pytchat.create(video_id=video_id, interruptable=False)
                self._chat = chat
                on_status(ConnectorState.CONNECTED, None)
                while not self._stop_event.is_set() and chat.is_alive():
                    for item in chat.get().sync_items():
                        if self._stop_event.is_set():
                            break
                        try:
                            item_type = str(getattr(item, "type", "textMessageEvent"))
                            is_support = item_type != "textMessageEvent"
                            amount = getattr(item, "amountString", None) or getattr(item, "amountDisplayString", None)
                            gift_name = getattr(item, "stickerName", None) or ("Super Chat" if "superChat" in item_type else None)
                            comment = str(getattr(item, "message", ""))
                            if is_support and not comment:
                                comment = f"{gift_name or item_type}{(' · ' + str(amount)) if amount else ''}"
                            on_event(
                                ChatEvent.create(
                                    platform="youtube",
                                    channel_id=self.video_id,
                                    author=str(item.author.name),
                                    avatar=getattr(item.author, "imageUrl", None),
                                    comment=comment,
                                    event_type="gift" if ("superChat" in item_type or "sticker" in item_type) else "youtube_event",
                                    gift_name=gift_name,
                                    gift_value=str(amount) if amount else None,
                                )
                            )
                        except ValidationError as exc:
                            log.warning("Dropped invalid YouTube event: %s", exc)
                    self._stop_event.wait(1)
                on_status(ConnectorState.DISCONNECTED, "Remote stream ended")
            except Exception as exc:
                log.warning("YouTube connector failed: %s", type(exc).__name__)
                on_status(ConnectorState.FAILED, "Unable to connect to YouTube Live")

        self._thread = threading.Thread(target=run, name=f"youtube-{self.video_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        chat = self._chat
        if chat is not None:
            try:
                chat.terminate()
            except Exception:
                log.warning("YouTube shutdown failed", exc_info=True)
