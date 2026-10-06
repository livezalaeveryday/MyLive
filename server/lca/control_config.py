"""Fail-closed configuration for the Home Server control plane.

Secret values are loaded only from explicitly configured files.  This module
never falls back to generated production credentials or environment values.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit

from .config import _as_bool


SAFE_READINESS_REASONS = frozenset(
    {
        "BIND_UNSAFE",
        "DATA_PATH_INVALID",
        "DATABASE_UNAVAILABLE",
        "MIGRATION_INVALID",
        "SECURITY_CONFIG_INVALID",
        "ORIGIN_INVALID",
        "RETENTION_OVERDUE",
        "MAINTENANCE",
    }
)


def _value(overrides: Mapping[str, Any] | None, name: str, default: Any = "") -> Any:
    if overrides:
        if name in overrides:
            return overrides[name]
        short = name.removeprefix("LCA_CP_").removeprefix("LCA_").lower()
        if short in overrides:
            return overrides[short]
    return os.getenv(name, default)


def _configured_path(value: Any) -> Path | None:
    text = str(value or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


def _below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _looks_like_sync_or_download(path: Path) -> bool:
    prohibited = {"downloads", "download", "onedrive", "dropbox", "google drive"}
    return any(part.casefold() in prohibited for part in path.parts)


def _secure_permissions(path: Path, *, directory: bool, writable: bool = False) -> bool:
    if not path.exists() or path.is_dir() != directory:
        return False
    access_mode = os.R_OK
    if directory:
        access_mode |= os.X_OK
    if writable:
        access_mode |= os.W_OK
    if os.name == "nt":
        # ACL ownership is validated by the Operations deployment drill on
        # Windows; POSIX mode bits are not authoritative on NTFS.
        return os.access(path, access_mode)
    return (path.stat().st_mode & 0o077) == 0 and os.access(path, access_mode)


def _validate_origin(origin: str, allow_insecure_test_mode: bool) -> bool:
    if not origin or origin == "*" or origin.endswith("/"):
        return False
    parsed = urlsplit(origin)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        return False
    if not parsed.hostname or parsed.scheme not in {"https", "http"}:
        return False
    if parsed.scheme == "http":
        if not allow_insecure_test_mode or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False
    return True


@dataclass(frozen=True, slots=True)
class ControlPlaneSettings:
    bind_host: str
    data_dir: Path | None
    db_path: Path | None
    secret_dir: Path | None
    signing_key_file: Path | None
    hash_pepper_file: Path | None
    browser_origin: str
    token_issuer: str
    token_audience: str
    signing_key_id: str
    hash_key_id: str
    cookie_secure: bool
    maintenance_mode: bool
    testing: bool
    allow_insecure_test_mode: bool
    issues: tuple[str, ...]

    @property
    def externally_ready(self) -> bool:
        return not self.testing and self.cookie_secure and self.browser_origin.startswith("https://")

    @classmethod
    def from_env(
        cls,
        overrides: Mapping[str, Any] | None = None,
        *,
        bind_host: str,
        maintenance_mode: bool,
        testing: bool,
    ) -> "ControlPlaneSettings":
        allow_test = testing and _as_bool(_value(overrides, "LCA_CP_ALLOW_INSECURE_TEST_MODE", False))
        data_dir = _configured_path(_value(overrides, "LCA_DATA_DIR"))
        db_path = _configured_path(_value(overrides, "LCA_DB_PATH"))
        secret_dir = _configured_path(_value(overrides, "LCA_CP_SECRET_DIR"))
        signing_key_file = _configured_path(_value(overrides, "LCA_CP_SIGNING_KEY_FILE"))
        hash_pepper_file = _configured_path(_value(overrides, "LCA_CP_HASH_PEPPER_FILE"))
        origin = str(_value(overrides, "LCA_CP_BROWSER_ORIGIN", "")).strip()
        issuer = str(_value(overrides, "LCA_CP_TOKEN_ISSUER", "")).strip()
        audience = str(_value(overrides, "LCA_CP_TOKEN_AUDIENCE", "")).strip()
        signing_key_id = str(_value(overrides, "LCA_CP_SIGNING_KEY_ID", "")).strip()
        hash_key_id = str(_value(overrides, "LCA_CP_HASH_KEY_ID", "")).strip()
        cookie_secure = _as_bool(_value(overrides, "LCA_CP_COOKIE_SECURE", True), True)

        issues: set[str] = set()
        if bind_host not in {"127.0.0.1", "::1", "localhost"}:
            issues.add("BIND_UNSAFE")
        if maintenance_mode:
            issues.add("MAINTENANCE")

        source_root = Path(__file__).resolve().parents[2]
        system_temp = Path(tempfile.gettempdir()).resolve()
        path_valid = bool(data_dir and db_path)
        if path_valid:
            assert data_dir is not None and db_path is not None
            path_valid = data_dir.is_absolute() and db_path.is_absolute() and _below(db_path, data_dir)
            path_valid = path_valid and db_path.suffix == ".sqlite3" and db_path != data_dir
            path_valid = path_valid and not _below(data_dir, source_root)
            path_valid = path_valid and not _looks_like_sync_or_download(data_dir)
            path_valid = path_valid and _secure_permissions(data_dir, directory=True, writable=True)
            if _below(data_dir, system_temp) and not allow_test:
                path_valid = False
        if not path_valid:
            issues.add("DATA_PATH_INVALID")

        security_paths_valid = bool(secret_dir and signing_key_file and hash_pepper_file)
        if security_paths_valid:
            assert secret_dir is not None and signing_key_file is not None and hash_pepper_file is not None
            security_paths_valid = (
                secret_dir.is_absolute()
                and _below(signing_key_file, secret_dir)
                and _below(hash_pepper_file, secret_dir)
                and (not data_dir or not _below(secret_dir, data_dir))
                and not _below(secret_dir, source_root)
                and not _looks_like_sync_or_download(secret_dir)
                # The deployment mounts the secret directory read-only.  It
                # must be readable/traversable by the service identity, never
                # writable as a readiness prerequisite.
                and _secure_permissions(secret_dir, directory=True)
                and _secure_permissions(signing_key_file, directory=False)
                and _secure_permissions(hash_pepper_file, directory=False)
            )
            if _below(secret_dir, system_temp) and not allow_test:
                security_paths_valid = False
        identifiers_valid = all(
            value and len(value) <= 128 and not any(char.isspace() for char in value)
            for value in (issuer, audience, signing_key_id, hash_key_id)
        )
        if not security_paths_valid or not identifiers_valid:
            issues.add("SECURITY_CONFIG_INVALID")
        if not _validate_origin(origin, allow_test):
            issues.add("ORIGIN_INVALID")
        if not cookie_secure and not allow_test:
            issues.add("SECURITY_CONFIG_INVALID")

        return cls(
            bind_host=bind_host,
            data_dir=data_dir,
            db_path=db_path,
            secret_dir=secret_dir,
            signing_key_file=signing_key_file,
            hash_pepper_file=hash_pepper_file,
            browser_origin=origin,
            token_issuer=issuer,
            token_audience=audience,
            signing_key_id=signing_key_id,
            hash_key_id=hash_key_id,
            cookie_secure=cookie_secure,
            maintenance_mode=maintenance_mode,
            testing=testing,
            allow_insecure_test_mode=allow_test,
            issues=tuple(sorted(issues)),
        )
