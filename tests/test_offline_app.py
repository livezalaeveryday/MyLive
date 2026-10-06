from __future__ import annotations

from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from lca import create_app
from lca.models import ChatEvent
from tests.support import FakeConnectorFactory


TOKEN = "offline-test-token-1234567890"
SESSION_A = "session_A_123456"
SESSION_B = "session_B_123456"


class OfflineApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = FakeConnectorFactory()
        self.app = create_app(
            {
                "TESTING": True,
                "LOCAL_AUTH_TOKEN": TOKEN,
                "DEVICE_ID": "offline-test-device",
                "CORS_ORIGINS": (),
            },
            connector_factory=self.factory,
        )
        self.http = self.app.test_client()
        self.socketio = self.app.extensions["socketio"]

    def headers(self, session_id: str = SESSION_A) -> dict[str, str]:
        return {"X-LCA-Token": TOKEN, "X-LCA-Session": session_id}

    def test_health_and_readiness_are_available_offline(self) -> None:
        self.assertEqual(200, self.http.get("/health/live").status_code)
        self.assertEqual({"status": "ready", "role": "connector"}, self.http.get("/health/ready").get_json())

    def test_api_requires_token_and_valid_session(self) -> None:
        self.assertEqual(401, self.http.get("/api/streams").status_code)
        bad_session = {"X-LCA-Token": TOKEN, "X-LCA-Session": "bad"}
        self.assertEqual(401, self.http.get("/api/streams", headers=bad_session).status_code)
        self.assertEqual(200, self.http.get("/api/streams", headers=self.headers()).status_code)

    def test_socket_rejects_invalid_device_token(self) -> None:
        socket_client = self.socketio.test_client(
            self.app, auth={"token": "wrong-token", "sessionId": SESSION_A}
        )
        self.assertFalse(socket_client.is_connected())

    def test_connect_returns_truthful_connecting_state(self) -> None:
        response = self.http.post(
            "/api/streams",
            headers=self.headers(),
            json={"platform": "tiktok", "channelId": "@creator"},
        )
        self.assertEqual(202, response.status_code)
        self.assertEqual("connecting", response.get_json()["stream"]["state"])
        self.assertEqual(1, len(self.factory.created))

    def test_invalid_stream_identifier_is_rejected_before_factory(self) -> None:
        response = self.http.post(
            "/api/streams",
            headers=self.headers(),
            json={"platform": "youtube", "channelId": "https://example.com/@network-resolved-handle"},
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual([], self.factory.created)

    def test_youtube_handle_is_accepted_by_factory(self) -> None:
        response = self.http.post(
            "/api/streams", headers=self.headers(),
            json={"platform": "youtube", "channelId": "@network-resolved-handle"},
        )
        self.assertEqual(202, response.status_code)
        self.assertEqual(1, len(self.factory.created))

    def test_streams_and_events_are_isolated_by_session_room(self) -> None:
        socket_a = self.socketio.test_client(
            self.app, auth={"token": TOKEN, "sessionId": SESSION_A}
        )
        socket_b = self.socketio.test_client(
            self.app, auth={"token": TOKEN, "sessionId": SESSION_B}
        )
        self.assertTrue(socket_a.is_connected())
        self.assertTrue(socket_b.is_connected())

        response = self.http.post(
            "/api/streams",
            headers=self.headers(SESSION_A),
            json={"platform": "tiktok", "channelId": "creator"},
        )
        self.assertEqual(202, response.status_code)
        self.assertEqual([], self.http.get("/api/streams", headers=self.headers(SESSION_B)).get_json()["streams"])

        event = ChatEvent.create(
            event_id=str(uuid.uuid4()),
            platform="tiktok",
            channel_id="creator",
            author="<b>attacker</b>",
            comment="<script>alert(1)</script>",
        )
        self.factory.created[0].emit(event)
        received_a = [item for item in socket_a.get_received() if item["name"] == "chat_message"]
        received_b = [item for item in socket_b.get_received() if item["name"] == "chat_message"]
        self.assertEqual(1, len(received_a))
        self.assertEqual([], received_b)
        self.assertEqual("<script>alert(1)</script>", received_a[0]["args"][0]["comment"])
        socket_a.disconnect()
        socket_b.disconnect()

    def test_highlight_is_validated_and_isolated(self) -> None:
        socket_a = self.socketio.test_client(self.app, auth={"token": TOKEN, "sessionId": SESSION_A})
        socket_b = self.socketio.test_client(self.app, auth={"token": TOKEN, "sessionId": SESSION_B})
        message = {
            "id": str(uuid.uuid4()),
            "platform": "youtube",
            "channelId": "dQw4w9WgXcQ",
            "author": "viewer",
            "comment": "hello",
            "timestamp": 1,
        }
        response = self.http.post(
            "/api/highlight",
            headers=self.headers(SESSION_A),
            json={"action": "show", "msg": message},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len([item for item in socket_a.get_received() if item["name"] == "highlight_message"]))
        self.assertEqual([], [item for item in socket_b.get_received() if item["name"] == "highlight_message"])
        socket_a.disconnect()
        socket_b.disconnect()

    def test_default_cors_does_not_reflect_untrusted_origin(self) -> None:
        response = self.http.get("/health/live", headers={"Origin": "https://attacker.example"})
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_unknown_static_asset_remains_not_found(self) -> None:
        self.assertEqual(404, self.http.get("/does-not-exist.js").status_code)


class MaintenanceTests(unittest.TestCase):
    def test_maintenance_fails_readiness_and_new_connections(self) -> None:
        app = create_app(
            {"TESTING": True, "LOCAL_AUTH_TOKEN": TOKEN, "MAINTENANCE_MODE": True},
            connector_factory=FakeConnectorFactory(),
        )
        client = app.test_client()
        headers = {"X-LCA-Token": TOKEN, "X-LCA-Session": SESSION_A}
        self.assertEqual(503, client.get("/health/ready").status_code)
        self.assertEqual(
            503,
            client.post("/api/streams", headers=headers, json={"platform": "tiktok", "channelId": "creator"}).status_code,
        )


class ControlPlaneRoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(
            {"TESTING": True, "LOCAL_AUTH_TOKEN": TOKEN, "RUNTIME_ROLE": "control-plane"}
        )
        self.client = self.app.test_client()

    def test_control_plane_is_explicitly_no_go(self) -> None:
        response = self.client.get("/health/ready")
        self.assertEqual(503, response.status_code)
        self.assertEqual("production_authentication_not_implemented", response.get_json()["reason"])
        info = self.client.get("/api/control-plane/info")
        self.assertEqual(
            {"role": "control-plane", "productionReady": False, "capabilities": []},
            info.get_json(),
        )

    def test_control_plane_does_not_expose_connector_surfaces(self) -> None:
        self.assertEqual(404, self.client.get("/").status_code)
        self.assertEqual(404, self.client.get("/overlay.html").status_code)
        self.assertEqual(404, self.client.post("/connect", json={}).status_code)
        self.assertEqual(404, self.client.post("/highlight", json={}).status_code)
        self.assertEqual(404, self.client.get("/socket.io/").status_code)
        self.assertNotIn("socketio", self.app.extensions)


if __name__ == "__main__":
    unittest.main()
