"""Flask application factory and transport boundaries."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Mapping

from .config import Settings


log = logging.getLogger(__name__)


def create_app(
    overrides: Mapping[str, Any] | None = None,
    *,
    connector_factory: Any = None,
    control_plane: Any = None,
) -> Any:
    settings = Settings.from_env(overrides)
    if settings.runtime_role == "control-plane":
        from .control_api import create_control_plane_app

        return create_control_plane_app(settings, overrides)

    # Keep the Connector transport and platform adapters outside the
    # control-plane import graph.  The Home Server image can therefore omit
    # Socket.IO and Connector modules entirely, and importing/starting its
    # WSGI app cannot initialize a platform adapter or network client.
    import engineio.async_drivers.threading  # noqa: F401
    from flask import Flask, g, jsonify, request, send_from_directory
    from flask_cors import CORS
    from flask_socketio import SocketIO, join_room
    from werkzeug.exceptions import HTTPException

    from .auth import AuthenticationError, LocalSessionAuth
    from .connectors import DefaultConnectorFactory
    from .control_plane import LocalControlPlane
    from .models import ChatEvent, ValidationError
    from .streams import StreamConflict, StreamForbidden, StreamNotFound, StreamService

    def _json_body() -> dict[str, Any]:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValidationError("Request body must be a JSON object")
        return payload

    static_folder = Path(__file__).resolve().parent.parent / "static"
    app = Flask(__name__, static_folder=None)
    app.config.update(TESTING=settings.testing, JSON_SORT_KEYS=False)

    app.extensions["lca.settings"] = settings

    @app.after_request
    def security_headers(response: Any):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/health/live")
    def health_live():
        return jsonify({"status": "live", "service": "live-chat-aggregator", "role": settings.runtime_role})

    if settings.cors_origins:
        CORS(
            app,
            resources={
                r"/api/*": {"origins": list(settings.cors_origins)},
                r"/(connect|disconnect|highlight)": {"origins": list(settings.cors_origins)},
            },
            allow_headers=["Content-Type", "X-LCA-Token", "X-LCA-Session"],
            methods=["GET", "POST", "OPTIONS"],
        )

    socketio_options: dict[str, Any] = {"async_mode": "threading"}
    if settings.cors_origins:
        socketio_options["cors_allowed_origins"] = list(settings.cors_origins)
    socketio = SocketIO(app, **socketio_options)
    auth = LocalSessionAuth(settings.local_auth_token, settings.device_id)
    streams = StreamService(
        socketio,
        connector_factory or DefaultConnectorFactory(),
        control_plane or LocalControlPlane(),
    )
    app.extensions["lca.auth"] = auth
    app.extensions["lca.streams"] = streams
    app.extensions["socketio"] = socketio

    @app.get("/")
    def index():
        return send_from_directory(static_folder, "index.html")

    @app.get("/health/ready")
    def health_ready():
        status_code = 503 if settings.maintenance_mode else 200
        return jsonify({"status": "maintenance" if settings.maintenance_mode else "ready", "role": "connector"}), status_code

    def maintenance_guard() -> tuple[Any, int] | None:
        if settings.maintenance_mode:
            return jsonify({"status": "error", "message": "Service is in maintenance mode"}), 503
        return None

    @app.get("/api/status")
    @auth.required
    def api_status():
        return jsonify(
            {
                "status": "maintenance" if settings.maintenance_mode else "ready",
                "deviceId": g.security_context.device_id,
                "streams": streams.list_for(g.security_context),
            }
        )

    @app.get("/api/streams")
    @auth.required
    def list_streams():
        return jsonify({"status": "success", "streams": streams.list_for(g.security_context)})

    def connect_stream():
        blocked = maintenance_guard()
        if blocked:
            return blocked
        payload = _json_body()
        record = streams.connect(g.security_context, payload.get("platform"), payload.get("channelId"))
        return jsonify(
            {"status": record.state.value, "stream": record.public(), "message": "Connection request accepted"}
        ), 202

    app.add_url_rule("/api/streams", "create_stream", auth.required(connect_stream), methods=["POST"])
    app.add_url_rule("/connect", "legacy_connect_stream", auth.required(connect_stream), methods=["POST"])

    def disconnect_stream():
        payload = _json_body()
        record = streams.disconnect(g.security_context, payload.get("platform"), payload.get("channelId"))
        return jsonify({"status": "success", "stream": record.public(), "message": "Disconnected"})

    app.add_url_rule("/api/streams/disconnect", "disconnect_stream", auth.required(disconnect_stream), methods=["POST"])
    app.add_url_rule("/disconnect", "legacy_disconnect_stream", auth.required(disconnect_stream), methods=["POST"])

    @app.post("/api/events/test")
    @auth.required
    def test_event():
        payload = _json_body()
        platform = payload.get("platform")
        if not isinstance(platform, str) or platform not in {"youtube", "tiktok"}:
            raise ValidationError("platform must be youtube or tiktok")
        if platform == "tiktok":
            event = ChatEvent.create(
                platform="tiktok", channel_id="test_creator", author="TEST TikTok",
                comment="[TEST] Rose ×10 — simulated gift", event_type="gift",
                gift_name="[TEST] Rose", gift_id="5655", gift_count=10,
                gift_image_url="https://p16-webcast.tiktokcdn.com/img/maliva/webcast-va/eba3a9bb85c33e017f3648eaf88d7189~tplv-obj.webp",
            )
        else:
            event = ChatEvent.create(
                platform="youtube", channel_id="dQw4w9WgXcQ", author="TEST YouTube",
                comment="[TEST] Simulated Super Chat — no payment", event_type="gift",
                gift_name="[TEST] Super Chat (simulated)", gift_count=1, gift_value="THB 100 (fake)",
            )
        socketio.emit("chat_message", event.to_dict(), to=g.security_context.room)
        return jsonify({"status": "success", "id": event.id, "message": "Simulated test event sent"})

    def highlight_message():
        payload = _json_body()
        action = str(payload.get("action", "")).strip().lower()
        if action == "clear":
            event_payload: dict[str, Any] = {"action": "clear"}
        elif action == "show":
            event = ChatEvent.from_mapping(payload.get("msg"))
            event_payload = {"action": "show", "id": event.id, "msg": event.to_dict()}
        else:
            raise ValidationError("action must be show or clear")
        socketio.emit("highlight_message", event_payload, to=g.security_context.room)
        return jsonify({"status": "success"})

    app.add_url_rule("/api/highlight", "highlight_message", auth.required(highlight_message), methods=["POST"])
    app.add_url_rule("/highlight", "legacy_highlight_message", auth.required(highlight_message), methods=["POST"])

    @socketio.on("connect")
    def socket_connect(socket_auth: Any = None):
        try:
            payload = socket_auth if isinstance(socket_auth, dict) else {}
            context = auth.authenticate(payload.get("token"), payload.get("sessionId"))
        except AuthenticationError:
            return False
        join_room(context.room)
        return True

    @socketio.on("overlay_scale")
    def overlay_scale(payload: Any = None):
        """Broadcast a per-overlay zoom change to every open OBS/browser view."""
        payload = payload if isinstance(payload, dict) else {}
        overlay = str(payload.get("overlay", "")).strip().lower()
        try:
            scale = float(payload.get("scale", 1))
            if not math.isfinite(scale):
                return
            scale = max(0.5, min(10, math.floor(scale * 10 + 0.5) / 10))
        except (TypeError, ValueError):
            return
        if overlay not in {"chat", "highlight", "vertical"} or not 0.5 <= scale <= 10:
            return
        try:
            context = auth.authenticate(payload.get("token"), payload.get("sessionId"))
        except AuthenticationError:
            return
        socketio.emit("overlay_scale", {"overlay": overlay, "scale": scale}, to=context.room)

    @app.errorhandler(ValidationError)
    def handle_validation_error(exc: ValidationError):
        return jsonify({"status": "error", "message": str(exc)}), 400

    @app.errorhandler(StreamConflict)
    def handle_conflict(exc: StreamConflict):
        return jsonify({"status": "error", "message": str(exc)}), 409

    @app.errorhandler(StreamNotFound)
    def handle_not_found(exc: StreamNotFound):
        return jsonify({"status": "error", "message": str(exc)}), 404

    @app.errorhandler(StreamForbidden)
    def handle_forbidden(exc: StreamForbidden):
        return jsonify({"status": "error", "message": str(exc)}), 403

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        if isinstance(exc, HTTPException):
            return exc
        if settings.testing:
            raise exc
        log.exception("Unhandled application error")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

    @app.get("/<path:path>")
    def static_asset(path: str):
        return send_from_directory(static_folder, path)

    return app
