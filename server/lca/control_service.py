"""Application services for closed-beta identity and device activation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import os
from pathlib import Path
import re
import secrets
from typing import Any, Mapping
import uuid

from .control_config import ControlPlaneSettings
from .control_repository import (
    Conflict,
    ControlPlaneRepository,
    DeviceLimitReached,
    InvalidSecret,
    MigrationInvalid,
    NotFound,
    RepositoryUnavailable,
    SQLiteRepository,
)
from .control_security import (
    AccessPrincipal,
    AccessTokenManager,
    CredentialError,
    KeyedHasher,
    hash_password,
    issue_opaque_credential,
    new_code,
    parse_opaque_credential,
    rfc3339,
    utc_now,
    verify_fake_password,
    verify_password,
)


EMAIL_PATTERN = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,189}$")
POLICY_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]{1,32})?$" )
IDEMPOTENCY_PATTERN = re.compile(r"^[\x20-\x7E]{16,128}$")
REVOKE_REASONS = frozenset({"user_request", "device_replaced", "security"})


class ServiceError(RuntimeError):
    code = "INVALID_REQUEST"
    status = 400


class AuthFailed(ServiceError):
    code = "AUTH_FAILED"
    status = 401


class InvalidCredential(ServiceError):
    code = "INVALID_CREDENTIAL"
    status = 401


class InviteInvalid(ServiceError):
    code = "INVITE_INVALID"
    status = 400


class ActivationFailed(ServiceError):
    code = "ACTIVATION_FAILED"
    status = 400


class CsrfFailed(ServiceError):
    code = "CSRF_FAILED"
    status = 403


class RateLimited(ServiceError):
    code = "RATE_LIMITED"
    status = 429

    def __init__(self, retry_after: int):
        super().__init__("Rate limited")
        self.retry_after = max(1, min(3600, int(retry_after)))


class IdempotencyConflict(ServiceError):
    code = "IDEMPOTENCY_CONFLICT"
    status = 409


class DeviceCap(ServiceError):
    code = "DEVICE_LIMIT_REACHED"
    status = 409


class ResourceNotFound(ServiceError):
    code = "NOT_FOUND"
    status = 404


class NotReady(ServiceError):
    code = "NOT_READY"
    status = 503


@dataclass(frozen=True, slots=True)
class SessionBundle:
    body: dict[str, Any]
    refresh_credential: str


@dataclass(frozen=True, slots=True)
class UserContext:
    principal: AccessPrincipal
    record: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DeviceContext:
    device_id: str
    tenant_id: str
    user_id: str
    credential_generation: int


def normalize_email(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError("Invalid email")
    email = value.strip().casefold()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise ServiceError("Invalid email")
    return email


def validate_password(value: Any) -> str:
    if not isinstance(value, str) or not 12 <= len(value) <= 128 or not value.strip():
        raise ServiceError("Invalid password")
    return value


def validate_policy_version(value: Any, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not POLICY_VERSION_PATTERN.fullmatch(value):
        raise ServiceError("Invalid policy version")
    return value


def validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not IDEMPOTENCY_PATTERN.fullmatch(value):
        raise ServiceError("Invalid idempotency key")
    return value


def validate_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError("Invalid identifier")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ServiceError("Invalid identifier") from exc
    if str(parsed) != value:
        raise ServiceError("Invalid identifier")
    return value


def validate_device_fields(device_instance_id: Any, app_version: Any, os_family: Any) -> tuple[str, str, str]:
    instance = validate_uuid(device_instance_id)
    if not isinstance(app_version, str) or len(app_version) > 64 or not SEMVER_PATTERN.fullmatch(app_version):
        raise ServiceError("Invalid app version")
    if os_family != "windows":
        raise ServiceError("Invalid OS family")
    return instance, app_version, os_family


class ControlPlaneRuntime:
    """Owns validated security material, repository, and service operations."""

    def __init__(
        self,
        settings: ControlPlaneSettings,
        repository: ControlPlaneRepository | None,
        hasher: KeyedHasher | None,
        tokens: AccessTokenManager | None,
        issues: set[str],
    ):
        self.settings = settings
        self.repository = repository
        self.hasher = hasher
        self.tokens = tokens
        self._issues = issues

    @classmethod
    def build(cls, settings: ControlPlaneSettings) -> "ControlPlaneRuntime":
        issues = set(settings.issues)
        repository: SQLiteRepository | None = None
        hasher: KeyedHasher | None = None
        tokens: AccessTokenManager | None = None

        try:
            secrets.token_bytes(1)
        except Exception:
            issues.add("SECURITY_CONFIG_INVALID")

        if "SECURITY_CONFIG_INVALID" not in issues:
            try:
                assert settings.signing_key_file is not None and settings.hash_pepper_file is not None
                signing_pem = settings.signing_key_file.read_bytes()
                root_pepper = settings.hash_pepper_file.read_bytes()
                hasher = KeyedHasher(root_pepper, settings.hash_key_id)
                tokens = AccessTokenManager.from_pem(
                    signing_pem,
                    key_id=settings.signing_key_id,
                    issuer=settings.token_issuer,
                    audience=settings.token_audience,
                )
            except (OSError, ValueError, TypeError):
                issues.add("SECURITY_CONFIG_INVALID")
                hasher = None
                tokens = None

        if "DATA_PATH_INVALID" not in issues:
            assert settings.db_path is not None
            repository = SQLiteRepository(settings.db_path)
            try:
                repository.initialize()
                if os.name != "nt":
                    settings.db_path.chmod(0o600)
                repository.run_retention(utc_now())
            except MigrationInvalid:
                issues.add("MIGRATION_INVALID")
            except (RepositoryUnavailable, OSError):
                issues.add("DATABASE_UNAVAILABLE")

        return cls(settings, repository, hasher, tokens, issues)

    def readiness_reasons(self, now: datetime | None = None) -> tuple[str, ...]:
        reasons = set(self._issues)
        current = now or utc_now()
        if self.repository is None:
            if "DATA_PATH_INVALID" not in reasons and "MIGRATION_INVALID" not in reasons:
                reasons.add("DATABASE_UNAVAILABLE")
        else:
            ready, reason = self.repository.readiness(current)
            if not ready and reason:
                reasons.add(reason)
            if self.hasher is not None and self.tokens is not None:
                try:
                    if not self.repository.security_key_dependencies_valid(
                        hash_key_id=self.hasher.key_id, signing_key_id=self.tokens.key_id, now=current,
                    ):
                        reasons.add("SECURITY_CONFIG_INVALID")
                except RepositoryUnavailable:
                    reasons.add("DATABASE_UNAVAILABLE")
        if self.hasher is None or self.tokens is None:
            reasons.add("SECURITY_CONFIG_INVALID")
        return tuple(sorted(reasons))

    def require_ready(self) -> tuple[SQLiteRepository, KeyedHasher, AccessTokenManager]:
        if self.readiness_reasons():
            raise NotReady("Runtime not ready")
        assert self.repository is not None and self.hasher is not None and self.tokens is not None
        return self.repository, self.hasher, self.tokens

    def _rate_key(self, hasher: KeyedHasher, scope: str, raw: str, now: datetime) -> str:
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        return hasher.digest("rate-limit", f"{day}\n{scope}\n{raw}")

    def enforce_rate(
        self, *, scope: str, raw_key: str, limit: int, window: int, block: int,
        now: datetime, request_id: str, tenant_id: str | None = None,
    ) -> None:
        repository, hasher, _ = self.require_ready()
        allowed, retry = repository.consume_rate_limit(
            scope=scope,
            key_hash=self._rate_key(hasher, scope, raw_key, now),
            limit=limit,
            window_seconds=window,
            block_seconds=block,
            now=now,
        )
        if not allowed:
            repository.append_audit(
                now=now, event_type="rate_limit.triggered", actor_type="anonymous",
                outcome="denied", reason_code="RATE_LIMITED", request_id=request_id,
                tenant_id=tenant_id, target_type="system", target_ref=None,
                metadata={"reason_code": scope.upper()},
            )
            raise RateLimited(retry)

    def register(self, payload: Mapping[str, Any], *, network_key: str, request_id: str, now: datetime) -> SessionBundle:
        repository, hasher, tokens = self.require_ready()
        code = payload["invite_code"]
        email = normalize_email(payload["email"])
        password = validate_password(payload["password"])
        terms = validate_policy_version(payload["accepted_terms_version"], nullable=self.settings.testing)
        privacy = validate_policy_version(payload["accepted_privacy_version"], nullable=self.settings.testing)
        if not isinstance(code, str) or not 20 <= len(code) <= 128:
            raise InviteInvalid("Invite invalid")
        self.enforce_rate(scope="register_code", raw_key=code, limit=5, window=900, block=1800, now=now, request_id=request_id)
        self.enforce_rate(scope="register_network", raw_key=network_key, limit=10, window=900, block=1800, now=now, request_id=request_id)
        encoded_password = hash_password(password)
        session_id = str(uuid.uuid4())
        refresh_credential, refresh_secret = issue_opaque_credential(session_id)
        session_expires = now + timedelta(days=30)
        try:
            account = repository.register(
                code_hash=hasher.digest("invite", code), hash_key_id=hasher.key_id,
                email=email, password_hash=encoded_password,
                accepted_terms_version=terms, accepted_privacy_version=privacy,
                session_id=session_id, refresh_hash=hasher.digest("refresh", refresh_secret),
                signing_key_id=tokens.key_id, now=now, session_expires_at=session_expires,
                request_id=request_id,
            )
        except InvalidSecret as exc:
            raise InviteInvalid("Invite invalid") from exc
        account["email_normalized"] = email
        return self._session_bundle(account, session_id, refresh_credential, refresh_secret, 1, now)

    def login(self, payload: Mapping[str, Any], *, network_key: str, request_id: str, now: datetime) -> SessionBundle:
        repository, hasher, tokens = self.require_ready()
        email = normalize_email(payload["email"])
        password = validate_password(payload["password"])
        self.enforce_rate(scope="login_email", raw_key=email, limit=5, window=900, block=1800, now=now, request_id=request_id)
        self.enforce_rate(scope="login_network", raw_key=network_key, limit=20, window=900, block=1800, now=now, request_id=request_id)
        account = repository.find_login(email)
        valid = False
        rehash = False
        if account:
            try:
                valid, rehash = verify_password(account["password_hash"], password)
            except CredentialError:
                valid = False
        else:
            verify_fake_password(password)
        if not account or not valid or account["status"] != "active" or account["tenant_status"] != "active":
            if account:
                repository.append_audit(
                    now=now, event_type="auth.login_denied", actor_type="anonymous", outcome="denied",
                    reason_code="AUTH_FAILED", request_id=request_id, tenant_id=account["tenant_id"],
                    target_type="user", target_ref=account["id"],
                )
            else:
                repository.append_audit(
                    now=now, event_type="auth.login_denied", actor_type="anonymous", outcome="denied",
                    reason_code="AUTH_FAILED", request_id=request_id, tenant_id=None,
                    target_type="system", target_ref=None,
                )
            raise AuthFailed("Authentication failed")
        session_id = str(uuid.uuid4())
        refresh_credential, refresh_secret = issue_opaque_credential(session_id)
        repository.login_success(
            user_id=account["id"], tenant_id=account["tenant_id"], session_id=session_id,
            refresh_hash=hasher.digest("refresh", refresh_secret), hash_key_id=hasher.key_id,
            signing_key_id=tokens.key_id, now=now, expires_at=now + timedelta(days=30),
            request_id=request_id, replacement_password_hash=hash_password(password) if rehash else None,
        )
        account_view = {
            "user_id": account["id"], "tenant_id": account["tenant_id"],
            "role": account["role"], "status": account["status"],
            "email_normalized": email,
        }
        return self._session_bundle(account_view, session_id, refresh_credential, refresh_secret, 1, now)

    def _session_bundle(
        self, account: Mapping[str, Any], session_id: str, refresh_credential: str,
        refresh_secret: str, generation: int, now: datetime,
    ) -> SessionBundle:
        assert self.tokens is not None and self.hasher is not None
        access, expires = self.tokens.issue(
            user_id=account["user_id"], tenant_id=account["tenant_id"], session_id=session_id,
            role=account["role"], now=now,
        )
        csrf = self.hasher.csrf_token(session_id, refresh_secret, generation)
        body = {
            "user": {"id": account["user_id"], "email": account.get("email_normalized"), "role": account["role"], "status": account["status"]},
            "access_token": access,
            "access_expires_at": rfc3339(expires),
            "csrf_token": csrf,
        }
        if body["user"]["email"] is None:
            # Registration/login callers already know the email, but repository
            # return types deliberately keep direct identifiers narrow.
            body["user"].pop("email")
        return SessionBundle(body=body, refresh_credential=refresh_credential)

    def authenticate_user(self, bearer: Any, *, now: datetime) -> UserContext:
        repository, _, tokens = self.require_ready()
        try:
            principal = tokens.verify(bearer, now=now)
        except CredentialError as exc:
            raise InvalidCredential("Invalid credential") from exc
        record = repository.authenticate_access(
            user_id=principal.user_id, tenant_id=principal.tenant_id,
            session_id=principal.session_id, now=now,
        )
        if not record or record["role"] != principal.role:
            raise InvalidCredential("Invalid credential")
        return UserContext(principal=principal, record=record)

    def refresh(
        self, refresh_credential: Any, csrf_header: Any, *, request_id: str, now: datetime,
    ) -> SessionBundle:
        repository, hasher, _ = self.require_ready()
        try:
            session_id, secret = parse_opaque_credential(refresh_credential)
        except CredentialError as exc:
            raise InvalidCredential("Invalid credential") from exc
        self.enforce_rate(scope="refresh_session", raw_key=session_id, limit=30, window=300, block=300, now=now, request_id=request_id)
        session = repository.get_session(session_id)
        if not session:
            raise InvalidCredential("Invalid credential")
        actual_hash = hasher.digest("refresh", secret)
        current_match = (
            session["refresh_hash_key_id"] == hasher.key_id
            and hasher.verify("refresh", secret, session["refresh_token_hash"])
        )
        if not current_match:
            for spent in repository.spent_refresh_credentials(session_id):
                if spent["key_id"] != hasher.key_id or not hasher.verify("refresh", secret, spent["token_hash"]):
                    continue
                expected_old_csrf = hasher.csrf_token(session_id, secret, int(spent["generation"]))
                if not isinstance(csrf_header, str) or not hmac.compare_digest(expected_old_csrf, csrf_header):
                    raise CsrfFailed("CSRF failed")
                repository.revoke_proven_refresh_replay(
                    session_id=session_id, generation=int(spent["generation"]),
                    token_hash=actual_hash, key_id=hasher.key_id, now=now,
                    request_id=request_id,
                )
                raise InvalidCredential("Invalid credential")
            # A random secret with a known selector is not proof of replay and
            # therefore must not revoke the session family.
            raise InvalidCredential("Invalid credential")
        expected_csrf = hasher.csrf_token(session_id, secret, int(session["refresh_generation"]))
        if not isinstance(csrf_header, str) or not hmac.compare_digest(expected_csrf, csrf_header):
            raise CsrfFailed("CSRF failed")
        new_credential, new_secret = issue_opaque_credential(session_id)
        try:
            rotated = repository.rotate_refresh(
                session_id=session_id, expected_generation=int(session["refresh_generation"]),
                current_hash=actual_hash, new_hash=hasher.digest("refresh", new_secret),
                hash_key_id=hasher.key_id, now=now, request_id=request_id,
            )
        except InvalidSecret as exc:
            raise InvalidCredential("Invalid credential") from exc
        account = {
            "user_id": rotated["user_id"], "tenant_id": rotated["tenant_id"],
            "role": rotated["role"], "status": "active",
        }
        return self._session_bundle(account, session_id, new_credential, new_secret, int(rotated["refresh_generation"]), now)

    def logout(self, refresh_credential: Any, csrf_header: Any, *, request_id: str, now: datetime) -> None:
        repository, hasher, _ = self.require_ready()
        try:
            session_id, secret = parse_opaque_credential(refresh_credential)
        except CredentialError as exc:
            raise InvalidCredential("Invalid credential") from exc
        session = repository.get_session(session_id)
        if not session:
            raise InvalidCredential("Invalid credential")
        expected_csrf = hasher.csrf_token(session_id, secret, int(session["refresh_generation"]))
        if not isinstance(csrf_header, str) or not hmac.compare_digest(expected_csrf, csrf_header):
            raise CsrfFailed("CSRF failed")
        actual_hash = hasher.digest("refresh", secret)
        if not hasher.verify("refresh", secret, session["refresh_token_hash"]):
            raise InvalidCredential("Invalid credential")
        if not repository.revoke_session_by_refresh(
            session_id=session_id, current_hash=actual_hash, now=now, request_id=request_id,
        ):
            raise InvalidCredential("Invalid credential")

    def me(self, context: UserContext) -> dict[str, Any]:
        record = context.record
        return {
            "user": {"id": context.principal.user_id, "email": record["email_normalized"], "role": record["role"], "status": record["user_status"]},
            "tenant": {"id": context.principal.tenant_id, "status": record["tenant_status"], "plan_code": record["plan_code"]},
            "entitlement": {"active_device_limit": record["active_device_limit"], "active_device_count": record["active_device_count"]},
        }

    def create_activation_code(
        self, context: UserContext, idempotency_key: Any, *, request_id: str, now: datetime,
    ) -> dict[str, Any]:
        repository, hasher, _ = self.require_ready()
        idem = validate_idempotency_key(idempotency_key)
        code = new_code(24)
        expires = now + timedelta(minutes=10)
        key_hash = hasher.digest("idempotency", idem)
        fingerprint = hasher.fingerprint("activation_code_create", {})
        try:
            repository.create_activation_code(
                tenant_id=context.principal.tenant_id, user_id=context.principal.user_id,
                code_hash=hasher.digest("activation", code), hash_key_id=hasher.key_id,
                idempotency_key_hash=key_hash, fingerprint=fingerprint, now=now,
                expires_at=expires, request_id=request_id,
            )
        except Conflict as exc:
            raise IdempotencyConflict("Idempotency conflict") from exc
        return {"activation_code": code, "expires_at": rfc3339(expires)}

    def activate_device(
        self, payload: Mapping[str, Any], idempotency_key: Any, *, network_key: str,
        request_id: str, now: datetime,
    ) -> tuple[dict[str, Any], int]:
        repository, hasher, _ = self.require_ready()
        idem = validate_idempotency_key(idempotency_key)
        code = payload["activation_code"]
        if not isinstance(code, str) or not 20 <= len(code) <= 128:
            raise ActivationFailed("Activation failed")
        instance, app_version, os_family = validate_device_fields(
            payload["device_instance_id"], payload["app_version"], payload["os_family"]
        )
        self.enforce_rate(scope="activate_code", raw_key=code, limit=5, window=900, block=1800, now=now, request_id=request_id)
        self.enforce_rate(scope="activate_network", raw_key=network_key, limit=10, window=900, block=1800, now=now, request_id=request_id)
        secret = new_code(32)
        fields = {"activation_code_hash": hasher.digest("activation", code), "device_instance_id_hash": hasher.digest("device-instance", instance), "app_version": app_version, "os_family": os_family}
        try:
            device, retried = repository.activate_device(
                code_hash=fields["activation_code_hash"], hash_key_id=hasher.key_id,
                instance_hash=fields["device_instance_id_hash"], credential_hash=hasher.digest("device", secret),
                idempotency_key_hash=hasher.digest("idempotency", idem),
                fingerprint=hasher.fingerprint("device_activate", fields), app_version=app_version,
                os_family=os_family, now=now, request_id=request_id,
            )
        except InvalidSecret as exc:
            raise ActivationFailed("Activation failed") from exc
        except DeviceLimitReached as exc:
            raise DeviceCap("Device limit reached") from exc
        except Conflict as exc:
            raise IdempotencyConflict("Idempotency conflict") from exc
        credential = f"{device['id']}.{secret}"
        return {
            "device_id": device["id"], "device_credential": credential,
            "credential_generation": device["credential_generation"],
            "heartbeat_interval_seconds": 60,
        }, (200 if retried else 201)

    def authenticate_device(self, bearer: Any, *, request_id: str, now: datetime) -> DeviceContext:
        repository, hasher, _ = self.require_ready()
        try:
            device_id, secret = parse_opaque_credential(bearer)
        except CredentialError as exc:
            raise InvalidCredential("Invalid credential") from exc
        self.enforce_rate(scope="heartbeat_device", raw_key=device_id, limit=12, window=60, block=60, now=now, request_id=request_id)
        device = repository.get_device_principal(device_id)
        if not device or device["credential_hash_key_id"] != hasher.key_id:
            raise InvalidCredential("Invalid credential")
        if not hasher.verify("device", secret, device["credential_hash"]):
            raise InvalidCredential("Invalid credential")
        if device["status"] != "active" or device["user_status"] != "active" or device["tenant_status"] != "active":
            raise InvalidCredential("Invalid credential")
        return DeviceContext(
            device_id=device_id, tenant_id=device["tenant_id"], user_id=device["user_id"],
            credential_generation=int(device["credential_generation"]),
        )

    def list_devices(self, context: UserContext) -> dict[str, Any]:
        repository, _, _ = self.require_ready()
        return {"devices": repository.list_devices(context.principal.tenant_id)}

    def revoke_device(
        self, context: UserContext, device_id: Any, reason: Any, idempotency_key: Any,
        *, request_id: str, now: datetime,
    ) -> None:
        repository, hasher, _ = self.require_ready()
        device = validate_uuid(device_id)
        idem = validate_idempotency_key(idempotency_key)
        if reason not in REVOKE_REASONS:
            raise ServiceError("Invalid revoke reason")
        fields = {"device_id": device, "reason": reason}
        try:
            repository.revoke_device(
                tenant_id=context.principal.tenant_id, user_id=context.principal.user_id,
                device_id=device, reason=reason,
                idempotency_key_hash=hasher.digest("idempotency", idem),
                fingerprint=hasher.fingerprint("device_revoke", fields), now=now, request_id=request_id,
            )
        except NotFound as exc:
            raise ResourceNotFound("Not found") from exc
        except Conflict as exc:
            raise IdempotencyConflict("Idempotency conflict") from exc

    def heartbeat(
        self, context: DeviceContext, payload: Mapping[str, Any], path_device_id: Any,
        *, now: datetime,
    ) -> dict[str, Any]:
        repository, _, _ = self.require_ready()
        path_id = validate_uuid(path_device_id)
        if path_id != context.device_id:
            raise ResourceNotFound("Not found")
        _, app_version, os_family = validate_device_fields(str(uuid.uuid4()), payload["app_version"], payload["os_family"])
        if not repository.heartbeat(
            device_id=context.device_id, tenant_id=context.tenant_id,
            app_version=app_version, os_family=os_family, now=now,
        ):
            raise InvalidCredential("Invalid credential")
        return {"server_time": rfc3339(now), "next_heartbeat_seconds": 60, "status": "active"}

    def bootstrap_invite(self, email: str, *, actor_ref: str, request_id: str, now: datetime) -> tuple[str, str]:
        repository, hasher, _ = self.require_ready()
        normalized = normalize_email(email)
        code = new_code(24)
        invite_id, _ = repository.create_invite(
            email=normalized, code_hash=hasher.digest("invite", code), hash_key_id=hasher.key_id,
            now=now, expires_at=now + timedelta(hours=24), request_id=request_id,
            actor_ref=actor_ref,
        )
        return invite_id, code

    def create_invite_for_tenant(
        self, email: str, tenant_id: str, *, actor_ref: str, request_id: str, now: datetime,
    ) -> tuple[str, str]:
        repository, hasher, _ = self.require_ready()
        normalized = normalize_email(email)
        validate_uuid(tenant_id)
        code = new_code(24)
        invite_id, _ = repository.create_invite(
            email=normalized, code_hash=hasher.digest("invite", code), hash_key_id=hasher.key_id,
            now=now, expires_at=now + timedelta(hours=24), request_id=request_id,
            actor_ref=actor_ref, tenant_id=tenant_id,
        )
        return invite_id, code
