"""Strict `/v1` HTTP boundary for the Home Server control plane."""

from __future__ import annotations

from datetime import datetime
import ipaddress
import json
import math
import re
from typing import Any, Callable, Mapping
import uuid

from flask import Flask, Response, g, jsonify, make_response, request
from werkzeug.exceptions import HTTPException

from .config import Settings
from .control_config import ControlPlaneSettings
from .control_repository import RepositoryError
from .control_security import utc_now
from .control_service import (
    CsrfFailed,
    DeviceContext,
    NotReady,
    ServiceError,
    UserContext,
    ControlPlaneRuntime,
    validate_idempotency_key,
)


FORBIDDEN_KEYS = frozenset(
    {
        "chat", "chats", "comment", "comments", "message", "messages", "text", "content",
        "transcript", "viewer", "viewer_id", "viewer_name", "author", "author_id", "username",
        "nickname", "avatar", "avatar_url", "channel", "channel_id", "channel_name", "room",
        "room_id", "live_id", "stream_key", "tiktok_id", "youtube_channel",
    }
)
QUERY_CREDENTIAL_KEYS = frozenset(
    {"token", "access_token", "refresh_token", "session", "key", "code", "password", "secret", "authorization"}
)
IDENTITY_HEADERS = frozenset(
    {"x-tenant-id", "x-user-id", "x-role", "tenant-id", "user-id", "role", "x-lca-tenant-id", "x-lca-user-id"}
)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
REFRESH_COOKIE = "lca_refresh"


class ApiProblem(RuntimeError):
    def __init__(self, status: int, code: str, details: Mapping[str, Any] | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.details = dict(details or {})


class DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("Non-finite number")


def _normal_key(value: str) -> str:
    return value.strip().casefold().replace("-", "_")


def _walk_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if _normal_key(key) in FORBIDDEN_KEYS or _walk_forbidden(child):
                return True
    elif isinstance(value, list):
        return any(_walk_forbidden(child) for child in value)
    return False


def _coarse_network(value: str | None) -> str:
    try:
        address = ipaddress.ip_address(value or "")
        prefix = 24 if address.version == 4 else 64
        return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
    except ValueError:
        return "unknown-network"


def create_control_plane_app(base_settings: Settings, overrides: Mapping[str, Any] | None = None) -> Flask:
    control_settings = ControlPlaneSettings.from_env(
        overrides,
        bind_host=base_settings.bind_host,
        maintenance_mode=base_settings.maintenance_mode,
        testing=base_settings.testing,
    )
    runtime = ControlPlaneRuntime.build(control_settings)
    app = Flask(__name__, static_folder=None)
    app.config.update(TESTING=base_settings.testing, JSON_SORT_KEYS=False)
    app.extensions["lca.settings"] = base_settings
    app.extensions["lca.control_settings"] = control_settings
    app.extensions["lca.control_runtime"] = runtime
    clock: Callable[[], datetime] = (overrides or {}).get("CLOCK", utc_now)  # type: ignore[assignment]

    def now() -> datetime:
        return clock()

    def error_response(status: int, code: str, details: Mapping[str, Any] | None = None) -> Response:
        response = jsonify(
            {"error": {"code": code, "request_id": getattr(g, "request_id", str(uuid.uuid4())), "details": dict(details or {})}}
        )
        response.status_code = status
        if status == 429:
            response.headers["Retry-After"] = str(response.get_json()["error"]["details"]["retry_after_seconds"])
        return response

    @app.before_request
    def request_boundary() -> Response | None:
        g.request_id = str(uuid.uuid4())
        if not request.path.startswith("/v1"):
            return None
        if any(name.casefold() in IDENTITY_HEADERS for name in request.headers.keys()):
            return error_response(400, "INVALID_REQUEST")
        query_keys = {_normal_key(key) for key in request.args.keys()}
        if query_keys & QUERY_CREDENTIAL_KEYS:
            return error_response(400, "QUERY_CREDENTIAL_FORBIDDEN")
        if query_keys:
            return error_response(400, "INVALID_REQUEST")
        maximum = 4096 if request.path.endswith("/heartbeat") else 16384
        if request.content_length is not None and request.content_length > maximum:
            return error_response(413, "PAYLOAD_TOO_LARGE")
        if request.method == "OPTIONS":
            origin = request.headers.get("Origin")
            requested_method = request.headers.get("Access-Control-Request-Method", "")
            requested_headers = {
                item.strip().casefold()
                for item in request.headers.get("Access-Control-Request-Headers", "").split(",")
                if item.strip()
            }
            allowed_headers = {"authorization", "content-type", "idempotency-key", "x-csrf-token"}
            if origin != control_settings.browser_origin or requested_method not in {"GET", "POST"} or not requested_headers <= allowed_headers:
                return error_response(403, "ORIGIN_FORBIDDEN")
            g.origin_allowed = True
            return make_response("", 204)
        return None

    @app.after_request
    def response_boundary(response: Response) -> Response:
        response.headers["X-Request-ID"] = getattr(g, "request_id", str(uuid.uuid4()))
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        response.headers.setdefault("Cache-Control", "no-store")
        if getattr(g, "origin_allowed", False):
            response.headers["Access-Control-Allow-Origin"] = control_settings.browser_origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Idempotency-Key, X-CSRF-Token"
            response.headers.add("Vary", "Origin")
        return response

    def route_template() -> str:
        return request.url_rule.rule if request.url_rule else request.path

    def enforce_origin(profile: str) -> None:
        origin = request.headers.get("Origin")
        if profile == "browser-refresh":
            if origin != control_settings.browser_origin:
                raise ApiProblem(403, "ORIGIN_FORBIDDEN")
            g.origin_allowed = True
        elif profile == "device":
            if origin is not None:
                raise ApiProblem(403, "ORIGIN_FORBIDDEN")
        elif origin is not None:
            if origin != control_settings.browser_origin:
                raise ApiProblem(403, "ORIGIN_FORBIDDEN")
            g.origin_allowed = True

    def no_body() -> None:
        raw = request.get_data(cache=True)
        if raw:
            raise ApiProblem(400, "INVALID_REQUEST")

    def strict_json(schema: Mapping[str, tuple[type, ...]], *, maximum: int = 16384) -> dict[str, Any]:
        if request.mimetype != "application/json":
            raise ApiProblem(415, "UNSUPPORTED_MEDIA_TYPE")
        raw = request.get_data(cache=True)
        if len(raw) > maximum:
            raise ApiProblem(413, "PAYLOAD_TOO_LARGE")
        try:
            payload = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey, ValueError):
            raise ApiProblem(400, "INVALID_REQUEST") from None
        if not isinstance(payload, dict):
            raise ApiProblem(400, "INVALID_REQUEST")
        if _walk_forbidden(payload):
            if runtime.repository is not None and not runtime.readiness_reasons(now()):
                runtime.repository.append_audit(
                    now=now(), event_type="payload.forbidden", actor_type="anonymous",
                    outcome="denied", reason_code="PAYLOAD_FORBIDDEN", request_id=g.request_id,
                    target_type="system", target_ref=None,
                    metadata={"route_template": route_template(), "http_method": request.method, "status_code": 400},
                )
            raise ApiProblem(400, "PAYLOAD_FORBIDDEN")
        if set(payload) != set(schema):
            raise ApiProblem(400, "INVALID_REQUEST")
        for field, allowed_types in schema.items():
            value = payload[field]
            if isinstance(value, bool) and bool not in allowed_types:
                raise ApiProblem(400, "INVALID_REQUEST")
            if not isinstance(value, allowed_types):
                raise ApiProblem(400, "INVALID_REQUEST")
            if isinstance(value, float) and not math.isfinite(value):
                raise ApiProblem(400, "INVALID_REQUEST")
        return payload

    def bearer() -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or header.count(" ") != 1:
            raise ApiProblem(401, "INVALID_CREDENTIAL")
        token = header[7:]
        if not token:
            raise ApiProblem(401, "INVALID_CREDENTIAL")
        return token

    def user_context() -> UserContext:
        return runtime.authenticate_user(bearer(), now=now())

    def device_context() -> DeviceContext:
        return runtime.authenticate_device(bearer(), request_id=g.request_id, now=now())

    def set_refresh(response: Response, value: str) -> None:
        response.set_cookie(
            REFRESH_COOKIE, value, max_age=30 * 24 * 60 * 60,
            secure=control_settings.cookie_secure, httponly=True, samesite="Strict", path="/v1/auth",
        )

    def clear_refresh(response: Response) -> None:
        response.delete_cookie(
            REFRESH_COOKIE, secure=control_settings.cookie_secure,
            httponly=True, samesite="Strict", path="/v1/auth",
        )

    @app.get("/v1/health/live")
    def v1_live():
        no_body()
        enforce_origin("public")
        return jsonify({"status": "live"})

    @app.get("/v1/health/ready")
    def v1_ready():
        no_body()
        enforce_origin("public")
        reasons = runtime.readiness_reasons(now())
        if reasons:
            return error_response(503, "NOT_READY", {"reasons": list(reasons)})
        return jsonify({"status": "ready"})

    @app.post("/v1/auth/register")
    def register():
        payload = strict_json(
            {
                "invite_code": (str,), "email": (str,), "password": (str,),
                "accepted_terms_version": (str, type(None)),
                "accepted_privacy_version": (str, type(None)),
            }
        )
        enforce_origin("public")
        bundle = runtime.register(
            payload, network_key=_coarse_network(request.remote_addr), request_id=g.request_id, now=now(),
        )
        response = jsonify(bundle.body)
        response.status_code = 201
        set_refresh(response, bundle.refresh_credential)
        return response

    @app.post("/v1/auth/login")
    def login():
        payload = strict_json({"email": (str,), "password": (str,)})
        enforce_origin("public")
        bundle = runtime.login(
            payload, network_key=_coarse_network(request.remote_addr), request_id=g.request_id, now=now(),
        )
        response = jsonify(bundle.body)
        set_refresh(response, bundle.refresh_credential)
        return response

    @app.post("/v1/auth/refresh")
    def refresh():
        no_body()
        enforce_origin("browser-refresh")
        bundle = runtime.refresh(
            request.cookies.get(REFRESH_COOKIE), request.headers.get("X-CSRF-Token"),
            request_id=g.request_id, now=now(),
        )
        body = {key: value for key, value in bundle.body.items() if key != "user"}
        response = jsonify(body)
        set_refresh(response, bundle.refresh_credential)
        return response

    @app.post("/v1/auth/logout")
    def logout():
        no_body()
        enforce_origin("browser-refresh")
        runtime.logout(
            request.cookies.get(REFRESH_COOKIE), request.headers.get("X-CSRF-Token"),
            request_id=g.request_id, now=now(),
        )
        response = make_response("", 204)
        clear_refresh(response)
        return response

    @app.get("/v1/me")
    def me():
        no_body()
        enforce_origin("user")
        return jsonify(runtime.me(user_context()))

    @app.post("/v1/device-activation-codes")
    def create_activation_code():
        no_body()
        enforce_origin("user")
        result = runtime.create_activation_code(
            user_context(), request.headers.get("Idempotency-Key"), request_id=g.request_id, now=now(),
        )
        return jsonify(result), 201

    @app.post("/v1/devices/activate")
    def activate_device():
        payload = strict_json(
            {"activation_code": (str,), "device_instance_id": (str,), "app_version": (str,), "os_family": (str,)}
        )
        enforce_origin("public")
        result, status = runtime.activate_device(
            payload, request.headers.get("Idempotency-Key"),
            network_key=_coarse_network(request.remote_addr), request_id=g.request_id, now=now(),
        )
        return jsonify(result), status

    @app.get("/v1/devices")
    def list_devices():
        no_body()
        enforce_origin("user")
        return jsonify(runtime.list_devices(user_context()))

    @app.post("/v1/devices/<device_id>/revoke")
    def revoke_device(device_id: str):
        payload = strict_json({"reason": (str,)})
        enforce_origin("user")
        runtime.revoke_device(
            user_context(), device_id, payload["reason"], request.headers.get("Idempotency-Key"),
            request_id=g.request_id, now=now(),
        )
        return make_response("", 204)

    @app.post("/v1/devices/<device_id>/heartbeat")
    def heartbeat(device_id: str):
        payload = strict_json({"app_version": (str,), "os_family": (str,)}, maximum=4096)
        enforce_origin("device")
        if request.headers.get("Idempotency-Key") is not None:
            validate_idempotency_key(request.headers.get("Idempotency-Key"))
        return jsonify(runtime.heartbeat(device_context(), payload, device_id, now=now()))

    # Compatibility health endpoints remain control-plane-only.  They do not
    # expose connector/static/Socket.IO surfaces.
    @app.get("/health/live")
    def legacy_live():
        return jsonify({"status": "live", "service": "live-chat-aggregator", "role": "control-plane"})

    @app.get("/health/ready")
    def legacy_ready():
        reasons = runtime.readiness_reasons(now())
        if reasons:
            return jsonify({"status": "blocked", "role": "control-plane", "reason": "production_authentication_not_implemented"}), 503
        return jsonify({"status": "ready", "role": "control-plane"})

    @app.get("/api/control-plane/info")
    def legacy_info():
        reasons = runtime.readiness_reasons(now())
        if reasons:
            return jsonify({"role": "control-plane", "productionReady": False, "capabilities": []})
        return jsonify(
            {
                "role": "control-plane", "productionReady": True,
                "externalLaunchAuthorized": False,
                "capabilities": ["closed_invite", "revocable_session", "device_activation"],
            }
        )

    @app.errorhandler(ApiProblem)
    def handle_api_problem(exc: ApiProblem):
        return error_response(exc.status, exc.code, exc.details)

    @app.errorhandler(ServiceError)
    def handle_service_error(exc: ServiceError):
        if runtime.repository is not None and not isinstance(exc, (NotReady,)) and exc.code != "RATE_LIMITED":
            event_type = None
            if request.path.endswith("/heartbeat"):
                event_type = "device.heartbeat_denied"
            elif request.path in {"/v1/auth/refresh", "/v1/auth/logout"}:
                event_type = "auth.refresh_denied"
            if event_type:
                try:
                    runtime.repository.append_audit(
                        now=now(), event_type=event_type, actor_type="anonymous",
                        outcome="denied", reason_code=exc.code, request_id=g.request_id,
                        target_type="system", target_ref=None,
                        metadata={"route_template": route_template(), "http_method": request.method, "status_code": exc.status},
                    )
                except RepositoryError:
                    pass
        details = {"retry_after_seconds": exc.retry_after} if hasattr(exc, "retry_after") else {}
        return error_response(exc.status, exc.code, details)

    @app.errorhandler(RepositoryError)
    def handle_repository_error(_: RepositoryError):
        return error_response(503, "NOT_READY")

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        if isinstance(exc, HTTPException):
            if request.path.startswith("/v1"):
                return error_response(exc.code or 500, "NOT_FOUND" if exc.code == 404 else "INVALID_REQUEST")
            return exc
        return error_response(500, "INTERNAL_ERROR")

    return app
