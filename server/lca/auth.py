"""Authentication and session isolation for local HTTP/Socket.IO boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import hmac
from typing import Any, Callable, TypeVar

from flask import g, jsonify, request

from .models import ValidationError, validate_session_id


F = TypeVar("F", bound=Callable[..., Any])


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SecurityContext:
    device_id: str
    session_id: str

    @property
    def room(self) -> str:
        return f"session:{self.session_id}"


class LocalSessionAuth:
    """Binds operations to one device token and one validated session."""

    def __init__(self, token: str, device_id: str):
        self._token = token
        self._device_id = device_id

    @property
    def token(self) -> str:
        """Used only by the launcher to build a URL fragment."""
        return self._token

    def authenticate(self, token: Any, session_id: Any) -> SecurityContext:
        supplied = str(token or "")
        if not supplied or not hmac.compare_digest(supplied, self._token):
            raise AuthenticationError("Invalid local device token")
        try:
            normalized_session = validate_session_id(session_id)
        except ValidationError as exc:
            raise AuthenticationError(str(exc)) from exc
        return SecurityContext(device_id=self._device_id, session_id=normalized_session)

    def from_request(self) -> SecurityContext:
        return self.authenticate(request.headers.get("X-LCA-Token"), request.headers.get("X-LCA-Session"))

    def required(self, function: F) -> F:
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any):
            try:
                g.security_context = self.from_request()
            except AuthenticationError as exc:
                return jsonify({"status": "error", "message": str(exc)}), 401
            return function(*args, **kwargs)

        return wrapped  # type: ignore[return-value]
