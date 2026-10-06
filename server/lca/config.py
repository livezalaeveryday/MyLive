"""Runtime configuration for the local connector service."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
import secrets
from typing import Any, Mapping


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _origins(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (tuple, list)):
        raw = value
    else:
        raw = str(value).split(",")
    origins = tuple(str(item).strip().rstrip("/") for item in raw if str(item).strip())
    if "*" in origins:
        raise ValueError("Wildcard CORS origins are not permitted")
    for origin in origins:
        if not origin.startswith(("http://", "https://")):
            raise ValueError(f"Invalid CORS origin: {origin!r}")
    return origins


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings with safe local-only defaults."""

    # LAN-friendly default. Authentication still protects the API and socket.
    bind_host: str = "0.0.0.0"
    port: int = 5000
    runtime_role: str = "connector"
    local_auth_token: str = ""
    device_id: str = "local-device"
    cors_origins: tuple[str, ...] = ()
    maintenance_mode: bool = False
    testing: bool = False

    @classmethod
    def from_env(cls, overrides: Mapping[str, Any] | None = None) -> "Settings":
        values: dict[str, Any] = {
            "bind_host": os.getenv("LCA_BIND_HOST", "0.0.0.0"),
            "port": os.getenv("LCA_PORT", "5000"),
            "runtime_role": os.getenv("LCA_RUNTIME_ROLE", "connector"),
            "local_auth_token": os.getenv("LCA_LOCAL_AUTH_TOKEN", ""),
            "device_id": os.getenv("LCA_DEVICE_ID", "local-device"),
            "cors_origins": os.getenv("LCA_CORS_ORIGINS", ""),
            "maintenance_mode": os.getenv("LCA_MAINTENANCE_MODE", "false"),
            "testing": False,
        }
        aliases = {
            "BIND_HOST": "bind_host",
            "PORT": "port",
            "RUNTIME_ROLE": "runtime_role",
            "LOCAL_AUTH_TOKEN": "local_auth_token",
            "DEVICE_ID": "device_id",
            "CORS_ORIGINS": "cors_origins",
            "MAINTENANCE_MODE": "maintenance_mode",
            "TESTING": "testing",
        }
        for key, value in (overrides or {}).items():
            normalized_key = aliases.get(key, key.lower())
            if normalized_key in values:
                values[normalized_key] = value

        device_id = str(values["device_id"]).strip()
        bind_host = str(values["bind_host"]).strip()
        runtime_role = str(values["runtime_role"]).strip().lower()
        try:
            port = int(values["port"])
        except (TypeError, ValueError) as exc:
            raise ValueError("LCA_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("LCA_PORT must be between 1 and 65535")
        if not bind_host:
            raise ValueError("LCA_BIND_HOST cannot be empty")
        if runtime_role not in {"connector", "control-plane"}:
            raise ValueError("LCA_RUNTIME_ROLE must be connector or control-plane")
        if runtime_role == "connector":
            token = str(values["local_auth_token"]).strip() or secrets.token_urlsafe(32)
            if len(token) < 24:
                raise ValueError("LCA_LOCAL_AUTH_TOKEN must contain at least 24 characters")
        else:
            # Local Connector authentication is a separate boundary and must
            # not create or accept a fallback credential in control-plane mode.
            token = ""
        if not device_id or len(device_id) > 64:
            raise ValueError("LCA_DEVICE_ID must contain 1-64 characters")

        return cls(
            bind_host=bind_host,
            port=port,
            runtime_role=runtime_role,
            local_auth_token=token,
            device_id=device_id,
            cors_origins=_origins(values["cors_origins"]),
            maintenance_mode=_as_bool(values["maintenance_mode"]),
            testing=_as_bool(values["testing"]),
        )

    def with_port(self, port: int) -> "Settings":
        return replace(self, port=port)
