"""Validated domain models shared by connectors and transports."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
import uuid


PLATFORMS = frozenset({"tiktok", "youtube"})
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TIKTOK_ID_PATTERN = re.compile(r"^(?=.{2,24}$)[A-Za-z0-9_](?:[A-Za-z0-9._]*[A-Za-z0-9_])?$")
YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
SAFE_IMAGE_SCHEMES = frozenset({"http", "https"})


class ValidationError(ValueError):
    """Raised when data cannot safely enter the domain layer."""


def validate_session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValidationError("sessionId must be 8-64 URL-safe characters")
    return session_id


def normalize_platform(value: Any) -> str:
    platform = str(value or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValidationError("platform must be tiktok or youtube")
    return platform


def _youtube_id_from_url(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            candidate = parts[1] if len(parts) == 2 and parts[0] in {"live", "shorts"} else ""
    else:
        return None
    return candidate if YOUTUBE_VIDEO_ID_PATTERN.fullmatch(candidate) else None


def normalize_channel_id(platform: Any, value: Any) -> str:
    normalized_platform = normalize_platform(platform)
    channel_id = str(value or "").strip()
    if not channel_id or len(channel_id) > 256:
        raise ValidationError("channelId is required and must be at most 256 characters")

    if normalized_platform == "tiktok":
        parsed = urlparse(channel_id)
        if parsed.scheme in {"http", "https"}:
            if (parsed.hostname or "").lower() not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
                raise ValidationError("TikTok URL must use the tiktok.com domain")
            parts = [part for part in parsed.path.split("/") if part]
            channel_id = parts[0] if parts and parts[0].startswith("@") else ""
        channel_id = channel_id.removeprefix("@").strip()
        if not TIKTOK_ID_PATTERN.fullmatch(channel_id):
            raise ValidationError("TikTok username must contain 2-24 letters, numbers, dots, or underscores")
        return channel_id

    if YOUTUBE_VIDEO_ID_PATTERN.fullmatch(channel_id):
        return channel_id
    video_id = _youtube_id_from_url(channel_id)
    if video_id:
        return video_id
    parsed = urlparse(channel_id)
    if channel_id.startswith("@"):
        handle = channel_id[1:]
    elif parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        parts = [part for part in parsed.path.split("/") if part]
        handle = parts[0][1:] if parts and parts[0].startswith("@") else ""
    else:
        handle = ""
    if handle and re.fullmatch(r"[A-Za-z0-9._-]{2,60}", handle):
        return "@" + handle
    raise ValidationError("YouTube requires an @handle, 11-character video ID, or supported video URL")


def _bounded_text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{name} cannot be empty")
    if len(normalized) > maximum:
        raise ValidationError(f"{name} must be at most {maximum} characters")
    return normalized


def safe_avatar_url(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValidationError("avatar must be a valid URL")
    parsed = urlparse(value)
    if parsed.scheme.lower() not in SAFE_IMAGE_SCHEMES or not parsed.netloc:
        raise ValidationError("avatar must use http or https")
    return value


def safe_gift_image_url(value: Any) -> str | None:
    """Accept only event-supplied TikTok CDN HTTPS URLs; discard invalid images."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        return None
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme.lower() != "https" or parsed.username is not None
                or parsed.password is not None or ":" in parsed.netloc
                or not (host == "tiktokcdn.com" or host.endswith(".tiktokcdn.com"))):
            return None
    except ValueError:
        return None
    return value


@dataclass(frozen=True, slots=True)
class ChatEvent:
    id: str
    platform: str
    channel_id: str
    author: str
    comment: str
    timestamp: float
    avatar: str | None = None
    is_highlighted: bool = False
    event_type: str = "comment"
    gift_name: str | None = None
    gift_count: int | None = None
    gift_value: str | None = None
    gift_id: str | None = None
    gift_image_url: str | None = None

    @classmethod
    def create(
        cls,
        *,
        platform: Any,
        channel_id: Any,
        author: Any,
        comment: Any,
        avatar: Any = None,
        event_id: Any = None,
        timestamp: Any = None,
        event_type: Any = "comment",
        gift_name: Any = None,
        gift_count: Any = None,
        gift_value: Any = None,
        gift_id: Any = None,
        gift_image_url: Any = None,
    ) -> "ChatEvent":
        normalized_platform = normalize_platform(platform)
        normalized_channel = normalize_channel_id(normalized_platform, channel_id)
        normalized_id = str(event_id or uuid.uuid4())
        try:
            uuid.UUID(normalized_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("id must be a UUID") from exc
        try:
            normalized_timestamp = float(time.time() if timestamp is None else timestamp)
        except (TypeError, ValueError) as exc:
            raise ValidationError("timestamp must be numeric") from exc
        normalized_event_type = str(event_type or "comment").strip().lower()[:40] or "comment"
        normalized_gift_count = None
        if gift_count not in {None, ""}:
            try:
                normalized_gift_count = max(1, int(gift_count))
            except (TypeError, ValueError):
                normalized_gift_count = None
        return cls(
            id=normalized_id,
            platform=normalized_platform,
            channel_id=normalized_channel,
            author=_bounded_text("author", author, 100),
            comment=_bounded_text("comment", comment, 2000),
            timestamp=normalized_timestamp,
            avatar=safe_avatar_url(avatar),
            event_type=normalized_event_type,
            gift_name=str(gift_name).strip()[:200] if gift_name else None,
            gift_count=normalized_gift_count,
            gift_value=str(gift_value).strip()[:100] if gift_value else None,
            gift_image_url=safe_gift_image_url(gift_image_url),
            gift_id=str(gift_id) if re.fullmatch(r'[1-9]\d{0,19}', str(gift_id)) else None,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ChatEvent":
        if not isinstance(payload, Mapping):
            raise ValidationError("msg must be an object")
        return cls.create(
            platform=payload.get("platform"),
            channel_id=payload.get("channelId"),
            author=payload.get("author"),
            comment=payload.get("comment"),
            avatar=payload.get("avatar"),
            event_id=payload.get("id"),
            timestamp=payload.get("timestamp"),
            event_type=payload.get("eventType", "comment"),
            gift_name=payload.get("giftName"),
            gift_count=payload.get("giftCount"),
            gift_value=payload.get("giftValue"),
            gift_id=payload.get("giftId"),
            gift_image_url=payload.get("giftImageUrl"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "channelId": self.channel_id,
            "author": self.author,
            "avatar": self.avatar,
            "comment": self.comment,
            "timestamp": self.timestamp,
            "isHighlighted": self.is_highlighted,
            "eventType": self.event_type,
            "giftName": self.gift_name,
            "giftCount": self.gift_count,
            "giftValue": self.gift_value,
            "giftId": self.gift_id,
            "giftImageUrl": self.gift_image_url,
        }
