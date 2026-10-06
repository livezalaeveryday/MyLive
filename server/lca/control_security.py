"""Reviewed cryptographic primitives used by the control plane.

This module composes maintained stdlib/cryptography/PyJWT/Werkzeug APIs.  It
does not implement password hashing, signatures, or key derivation itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Mapping
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import jwt
from werkzeug.security import check_password_hash, generate_password_hash


CURRENT_PASSWORD_METHOD = "scrypt:32768:8:1"
APPROVED_OLDER_PASSWORD_PREFIXES = ("scrypt:16384:8:1$",)
OPAQUE_CREDENTIAL_PATTERN = re.compile(
    r"^(?P<selector>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\."
    r"(?P<secret>[A-Za-z0-9_-]{40,64})$"
)


class CredentialError(ValueError):
    """A generic credential failure safe for internal control flow."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class KeyedHasher:
    """Purpose-separated HMAC keys derived with cryptography HKDF."""

    _PURPOSES = frozenset(
        {
            "invite",
            "activation",
            "refresh",
            "device",
            "idempotency",
            "fingerprint",
            "rate-limit",
            "audit-ref",
            "csrf",
            "device-instance",
        }
    )

    def __init__(self, root_secret: bytes, key_id: str):
        if len(root_secret) < 32:
            raise ValueError("Root hash key must contain at least 256 bits")
        self.key_id = key_id
        self._keys = {
            purpose: HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=("lca-control-plane/v1/" + purpose).encode("ascii"),
            ).derive(root_secret)
            for purpose in self._PURPOSES
        }

    def digest(self, purpose: str, value: str | bytes) -> str:
        if purpose not in self._keys:
            raise ValueError("Unsupported hash purpose")
        encoded = value.encode("utf-8") if isinstance(value, str) else value
        return hmac.new(self._keys[purpose], encoded, hashlib.sha256).hexdigest()

    def verify(self, purpose: str, value: str | bytes, expected_hex: str) -> bool:
        actual = self.digest(purpose, value)
        return hmac.compare_digest(actual, expected_hex)

    def fingerprint(self, operation: str, fields: Mapping[str, Any]) -> str:
        canonical = json.dumps(fields, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self.digest("fingerprint", f"{operation}\n{canonical}")

    def csrf_token(self, session_id: str, refresh_secret: str, generation: int) -> str:
        raw = hmac.new(
            self._keys["csrf"],
            f"{session_id}\n{generation}\n{refresh_secret}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_code(byte_count: int = 24) -> str:
    if byte_count < 16:
        raise ValueError("Codes require at least 128 bits of entropy")
    return secrets.token_urlsafe(byte_count)


def issue_opaque_credential(selector: str) -> tuple[str, str]:
    uuid.UUID(selector)
    secret = secrets.token_urlsafe(32)
    return f"{selector}.{secret}", secret


def parse_opaque_credential(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 128:
        raise CredentialError("Invalid credential")
    match = OPAQUE_CREDENTIAL_PATTERN.fullmatch(value)
    if not match:
        raise CredentialError("Invalid credential")
    return match.group("selector"), match.group("secret")


def hash_password(password: str, *, method: str = CURRENT_PASSWORD_METHOD) -> str:
    return generate_password_hash(password, method=method)


def verify_password(encoded_hash: str, password: str) -> tuple[bool, bool]:
    valid = check_password_hash(encoded_hash, password)
    if not valid:
        return False, False
    current = encoded_hash.startswith(CURRENT_PASSWORD_METHOD + "$")
    approved_old = encoded_hash.startswith(APPROVED_OLDER_PASSWORD_PREFIXES)
    if not current and not approved_old:
        raise CredentialError("Unapproved password profile")
    return True, not current


_FAKE_PASSWORD_HASH = hash_password("not-a-real-account-password")


def verify_fake_password(password: str) -> None:
    check_password_hash(_FAKE_PASSWORD_HASH, password)


@dataclass(frozen=True, slots=True)
class AccessPrincipal:
    user_id: str
    tenant_id: str
    session_id: str
    role: str
    expires_at: datetime


class AccessTokenManager:
    algorithm = "EdDSA"

    def __init__(self, private_key: Ed25519PrivateKey, *, key_id: str, issuer: str, audience: str):
        self._private_key = private_key
        self._public_key = private_key.public_key()
        self.key_id = key_id
        self.issuer = issuer
        self.audience = audience

    @classmethod
    def from_pem(cls, pem: bytes, *, key_id: str, issuer: str, audience: str) -> "AccessTokenManager":
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Signing key must be Ed25519")
        return cls(key, key_id=key_id, issuer=issuer, audience=audience)

    def issue(
        self,
        *,
        user_id: str,
        tenant_id: str,
        session_id: str,
        role: str,
        now: datetime,
        lifetime_seconds: int = 900,
    ) -> tuple[str, datetime]:
        if lifetime_seconds > 900:
            raise ValueError("Access credential lifetime exceeds 15 minutes")
        issued = now.astimezone(timezone.utc).replace(microsecond=0)
        expires = issued + timedelta(seconds=lifetime_seconds)
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "kid": self.key_id,
            "sub": user_id,
            "tenant_id": tenant_id,
            "sid": session_id,
            "role": role,
            "iat": int(issued.timestamp()),
            "nbf": int(issued.timestamp()),
            "exp": int(expires.timestamp()),
        }
        token = jwt.encode(claims, self._private_key, algorithm=self.algorithm, headers={"kid": self.key_id})
        return token, expires

    def verify(self, token: Any, *, now: datetime | None = None) -> AccessPrincipal:
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise CredentialError("Invalid credential")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != self.algorithm or header.get("kid") != self.key_id:
                raise CredentialError("Invalid credential")
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=[self.algorithm],
                audience=self.audience,
                issuer=self.issuer,
                leeway=300,
                options={
                    "require": ["iss", "aud", "kid", "sub", "tenant_id", "sid", "role", "iat", "nbf", "exp"],
                    "verify_signature": True,
                    # PyJWT verifies the signature/issuer/audience/required
                    # claims. Time checks below use the injected UTC clock so
                    # expiry tests are deterministic and do not depend on the
                    # host wall clock.
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
            if claims.get("kid") != self.key_id or claims.get("role") != "owner":
                raise CredentialError("Invalid credential")
            if claims.get("iss") != self.issuer or claims.get("aud") != self.audience:
                raise CredentialError("Invalid credential")
            if any(type(claims.get(field)) is not int for field in ("iat", "nbf", "exp")):
                raise CredentialError("Invalid credential")
            if any(not isinstance(claims.get(field), str) for field in ("sub", "tenant_id", "sid", "kid", "role")):
                raise CredentialError("Invalid credential")
            issued = int(claims["iat"])
            not_before = int(claims["nbf"])
            expires = int(claims["exp"])
            if expires <= issued or expires - issued > 900 or not_before < issued or not_before > expires:
                raise CredentialError("Invalid credential")
            current = now or utc_now()
            current_epoch = current.timestamp()
            if issued > current_epoch + 300 or not_before > current_epoch + 300 or current_epoch > expires + 300:
                raise CredentialError("Invalid credential")
            for identifier in (claims["sub"], claims["tenant_id"], claims["sid"]):
                uuid.UUID(str(identifier))
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CredentialError("Invalid credential") from exc
        return AccessPrincipal(
            user_id=str(claims["sub"]),
            tenant_id=str(claims["tenant_id"]),
            session_id=str(claims["sid"]),
            role="owner",
            expires_at=datetime.fromtimestamp(expires, timezone.utc),
        )
