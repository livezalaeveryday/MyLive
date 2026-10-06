from __future__ import annotations

from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from lca.config import Settings
from lca.models import ChatEvent, ValidationError, normalize_channel_id


class SettingsTests(unittest.TestCase):
    def test_defaults_allow_lan_and_cors_is_closed(self) -> None:
        settings = Settings.from_env({"LOCAL_AUTH_TOKEN": "t" * 32})
        self.assertEqual("0.0.0.0", settings.bind_host)
        self.assertEqual((), settings.cors_origins)

    def test_wildcard_cors_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Wildcard"):
            Settings.from_env({"LOCAL_AUTH_TOKEN": "t" * 32, "CORS_ORIGINS": "*"})

    def test_short_auth_token_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 24"):
            Settings.from_env({"LOCAL_AUTH_TOKEN": "short"})

    def test_unknown_runtime_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "RUNTIME_ROLE"):
            Settings.from_env({"LOCAL_AUTH_TOKEN": "t" * 32, "RUNTIME_ROLE": "both"})


class ModelTests(unittest.TestCase):
    def test_stream_identifiers_are_normalized_without_network_resolution(self) -> None:
        self.assertEqual("creator.name", normalize_channel_id("tiktok", "@creator.name"))
        self.assertEqual("dQw4w9WgXcQ", normalize_channel_id("youtube", "https://youtu.be/dQw4w9WgXcQ"))
        self.assertEqual("dQw4w9WgXcQ", normalize_channel_id("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_untrusted_domains_and_invalid_identifiers_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            normalize_channel_id("tiktok", "https://example.com/@creator/live")
        with self.assertRaises(ValidationError):
            normalize_channel_id("youtube", "https://example.com/@channel-handle")
        with self.assertRaises(ValidationError):
            normalize_channel_id("tiktok", "x")

    def test_youtube_handle_is_supported(self) -> None:
        self.assertEqual("@channel-handle", normalize_channel_id("youtube", "@channel-handle"))

    def test_normalized_event_preserves_text_but_rejects_script_avatar(self) -> None:
        event = ChatEvent.create(
            event_id=str(uuid.uuid4()),
            platform="tiktok",
            channel_id="creator",
            author="<img src=x onerror=alert(1)>",
            comment="<script>alert(1)</script>",
        )
        self.assertEqual("<script>alert(1)</script>", event.comment)
        with self.assertRaisesRegex(ValidationError, "http or https"):
            ChatEvent.create(
                platform="tiktok",
                channel_id="creator",
                author="safe",
                comment="safe",
                avatar="javascript:alert(1)",
            )


if __name__ == "__main__":
    unittest.main()
