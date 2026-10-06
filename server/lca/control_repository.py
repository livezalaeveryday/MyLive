"""SQLite repository for the single-node closed-beta control plane."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator, Mapping, Protocol
import uuid

from .control_security import parse_rfc3339, rfc3339


class RepositoryError(RuntimeError):
    pass


class RepositoryUnavailable(RepositoryError):
    pass


class MigrationInvalid(RepositoryError):
    pass


class Conflict(RepositoryError):
    pass


class NotFound(RepositoryError):
    pass


class InvalidSecret(RepositoryError):
    pass


class DeviceLimitReached(RepositoryError):
    pass


class ControlPlaneRepository(Protocol):
    """Persistence boundary retained for a later managed-database adapter."""

    def initialize(self) -> None: ...
    def readiness(self, now: datetime) -> tuple[bool, str | None]: ...
    def run_retention(self, now: datetime) -> int: ...
    def security_key_dependencies_valid(self, *, hash_key_id: str, signing_key_id: str, now: datetime) -> bool: ...
    def append_audit(self, **values: Any) -> None: ...
    def create_invite(self, **values: Any) -> tuple[str, str]: ...
    def revoke_invite(self, **values: Any) -> bool: ...
    def register(self, **values: Any) -> dict[str, Any]: ...
    def find_login(self, email: str) -> dict[str, Any] | None: ...
    def login_success(self, **values: Any) -> None: ...
    def authenticate_access(self, **values: Any) -> dict[str, Any] | None: ...
    def get_session(self, session_id: str) -> dict[str, Any] | None: ...
    def spent_refresh_credentials(self, session_id: str) -> list[dict[str, Any]]: ...
    def rotate_refresh(self, **values: Any) -> dict[str, Any]: ...
    def revoke_proven_refresh_replay(self, **values: Any) -> bool: ...
    def revoke_session_by_refresh(self, **values: Any) -> bool: ...
    def operator_revoke_session(self, **values: Any) -> bool: ...
    def suspend_tenant(self, **values: Any) -> bool: ...
    def consume_rate_limit(self, **values: Any) -> tuple[bool, int]: ...
    def create_activation_code(self, **values: Any) -> str: ...
    def activate_device(self, **values: Any) -> tuple[dict[str, Any], bool]: ...
    def get_device_principal(self, device_id: str) -> dict[str, Any] | None: ...
    def list_devices(self, tenant_id: str) -> list[dict[str, Any]]: ...
    def revoke_device(self, **values: Any) -> None: ...
    def heartbeat(self, **values: Any) -> bool: ...


AUDIT_EVENT_TYPES = frozenset(
    {
        "invite.created", "invite.revoked", "invite.consumed", "invite.denied",
        "auth.registered", "auth.login_succeeded", "auth.login_denied",
        "auth.refresh_succeeded", "auth.refresh_denied", "auth.logout",
        "session.revoked", "tenant.suspended", "activation.created",
        "activation.consumed", "activation.denied", "device.revoked",
        "device.heartbeat_denied", "rate_limit.triggered", "payload.forbidden",
        "migration.applied", "backup.created", "restore.verified",
        "readiness.failed", "retention.completed", "retention.failed",
    }
)
AUDIT_METADATA_KEYS = frozenset(
    {
        "route_template", "http_method", "status_code", "reason_code",
        "app_version", "os_family", "credential_generation",
        "migration_version", "record_count", "duration_ms",
    }
)
AUDIT_ACTORS = frozenset({"anonymous", "user", "device", "operator", "system"})
AUDIT_OUTCOMES = frozenset({"success", "denied", "failure"})
TARGET_TYPES = frozenset({"tenant", "user", "session", "invite", "activation_code", "device", "system"})
RATE_SCOPES = frozenset(
    {
        "login_email", "login_network", "register_code", "register_network",
        "activate_code", "activate_network", "refresh_session", "heartbeat_device",
    }
)


SCHEMA_SQL = r"""
CREATE TABLE tenants (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active','suspended','deletion_pending','deleted')),
    plan_code TEXT NOT NULL CHECK (plan_code = 'free_beta'),
    active_device_limit INTEGER NOT NULL CHECK (active_device_limit = 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deletion_due_at TEXT,
    deleted_at TEXT
);
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email_normalized TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role = 'owner'),
    status TEXT NOT NULL CHECK (status IN ('active','locked','suspended','deletion_pending','deleted')),
    accepted_terms_version TEXT,
    accepted_privacy_version TEXT,
    accepted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT,
    deletion_due_at TEXT,
    deleted_at TEXT
);
CREATE TABLE invites (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email_normalized TEXT NOT NULL,
    code_hash TEXT NOT NULL UNIQUE,
    code_hash_key_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role = 'owner'),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    used_by_user_id TEXT REFERENCES users(id),
    failed_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempt_count BETWEEN 0 AND 5)
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    refresh_token_hash TEXT NOT NULL,
    refresh_hash_key_id TEXT NOT NULL,
    refresh_generation INTEGER NOT NULL CHECK (refresh_generation >= 1),
    client_kind TEXT NOT NULL CHECK (client_kind = 'browser'),
    signing_key_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT CHECK (revoke_reason IN ('logout','refresh_replay','password_change','user_suspended','operator_security','expired'))
);
CREATE TABLE spent_refresh_credentials (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    token_hash TEXT NOT NULL,
    key_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (session_id, generation)
);
CREATE TABLE activation_codes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE,
    code_hash_key_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    activated_device_id TEXT REFERENCES devices(id),
    failed_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempt_count BETWEEN 0 AND 5),
    idempotency_key_hash TEXT,
    idempotency_until TEXT,
    device_instance_id_hash TEXT
);
CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_instance_id_hash TEXT NOT NULL,
    credential_hash TEXT NOT NULL,
    credential_hash_key_id TEXT NOT NULL,
    credential_generation INTEGER NOT NULL CHECK (credential_generation >= 1),
    status TEXT NOT NULL CHECK (status IN ('active','revoked')),
    app_version TEXT NOT NULL,
    os_family TEXT NOT NULL CHECK (os_family = 'windows'),
    activated_at TEXT NOT NULL,
    last_heartbeat_at TEXT,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT CHECK (revoke_reason IN ('user_request','device_replaced','security','account_suspended'))
);
CREATE UNIQUE INDEX one_active_device_per_tenant ON devices(tenant_id) WHERE status = 'active';
CREATE TABLE rate_limit_buckets (
    scope TEXT NOT NULL CHECK (scope IN ('login_email','login_network','register_code','register_network','activate_code','activate_network','refresh_session','heartbeat_device')),
    key_hash TEXT NOT NULL,
    window_started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    blocked_until TEXT,
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (scope, key_hash, window_started_at)
);
CREATE TABLE idempotency_records (
    key_hash TEXT NOT NULL,
    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN ('activation_code_create','device_activate','device_revoke')),
    request_fingerprint TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (operation, key_hash)
);
CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    tenant_id TEXT REFERENCES tenants(id) ON DELETE SET NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('anonymous','user','device','operator','system')),
    actor_ref TEXT,
    event_type TEXT NOT NULL,
    target_type TEXT CHECK (target_type IN ('tenant','user','session','invite','activation_code','device','system')),
    target_ref TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('success','denied','failure')),
    reason_code TEXT NOT NULL,
    request_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


MIGRATIONS = ((1, "minimum_identity_schema", SCHEMA_SQL),)


EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "tenants": ("id","status","plan_code","active_device_limit","created_at","updated_at","deletion_due_at","deleted_at"),
    "users": ("id","tenant_id","email_normalized","password_hash","role","status","accepted_terms_version","accepted_privacy_version","accepted_at","created_at","updated_at","last_login_at","deletion_due_at","deleted_at"),
    "invites": ("id","tenant_id","email_normalized","code_hash","code_hash_key_id","role","created_at","expires_at","used_at","revoked_at","used_by_user_id","failed_attempt_count"),
    "sessions": ("id","tenant_id","user_id","refresh_token_hash","refresh_hash_key_id","refresh_generation","client_kind","signing_key_id","issued_at","accessed_at","expires_at","revoked_at","revoke_reason"),
    "spent_refresh_credentials": ("session_id","generation","token_hash","key_id","expires_at"),
    "activation_codes": ("id","tenant_id","user_id","code_hash","code_hash_key_id","created_at","expires_at","used_at","revoked_at","activated_device_id","failed_attempt_count","idempotency_key_hash","idempotency_until","device_instance_id_hash"),
    "devices": ("id","tenant_id","user_id","device_instance_id_hash","credential_hash","credential_hash_key_id","credential_generation","status","app_version","os_family","activated_at","last_heartbeat_at","updated_at","revoked_at","revoke_reason"),
    "rate_limit_buckets": ("scope","key_hash","window_started_at","updated_at","blocked_until","count"),
    "idempotency_records": ("key_hash","tenant_id","operation","request_fingerprint","resource_ref","created_at","expires_at"),
    "audit_events": ("id","occurred_at","tenant_id","actor_type","actor_ref","event_type","target_type","target_ref","outcome","reason_code","request_id","metadata_json"),
    "schema_migrations": ("version","name","checksum","applied_at"),
}


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _uuid() -> str:
    return str(uuid.uuid4())


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class SQLiteRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = _path_lock(db_path)

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except sqlite3.Error as exc:
            raise RepositoryUnavailable("Database unavailable") from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except InvalidSecret:
                # Generic credential denials may intentionally persist bounded
                # failure counters or a proven replay revocation.  Those
                # security changes commit atomically before the generic error
                # crosses the repository boundary.
                if connection.in_transaction:
                    connection.execute("COMMIT")
                raise
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise RepositoryUnavailable("Database transaction failed") from exc
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        if not self.db_path.parent.is_dir():
            raise RepositoryUnavailable("Approved data directory does not exist")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                applied = {int(row["version"]): dict(row) for row in connection.execute("SELECT * FROM schema_migrations")}
                known = {version for version, _, _ in MIGRATIONS}
                if set(applied) - known:
                    raise MigrationInvalid("Unknown migration")
                for version, name, sql in MIGRATIONS:
                    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    current = applied.get(version)
                    if current:
                        if current["name"] != name or current["checksum"] != checksum:
                            raise MigrationInvalid("Migration checksum mismatch")
                        continue
                    if any(existing > version for existing in applied):
                        raise MigrationInvalid("Missing migration")
                    now = rfc3339(datetime.now(timezone.utc))
                    script = "BEGIN IMMEDIATE;\n" + sql + (
                        "\nINSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES"
                        f"({version},{json.dumps(name)},{json.dumps(checksum)},{json.dumps(now)});\nCOMMIT;"
                    )
                    try:
                        connection.executescript(script)
                    except sqlite3.Error as exc:
                        if connection.in_transaction:
                            connection.execute("ROLLBACK")
                        raise MigrationInvalid("Migration failed") from exc
                self._validate_schema(connection)
            finally:
                connection.close()

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }
        if tables != set(EXPECTED_COLUMNS):
            raise MigrationInvalid("Schema table inventory mismatch")
        for table, expected in EXPECTED_COLUMNS.items():
            actual = tuple(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
            if actual != expected:
                raise MigrationInvalid("Schema column inventory mismatch")
        unexpected_objects = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1"
        ).fetchone()
        if unexpected_objects:
            raise MigrationInvalid("Unknown schema object")
        indexes = {
            row["name"]: row["sql"]
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            )
        }
        expected_index_sql = "CREATE UNIQUE INDEX one_active_device_per_tenant ON devices(tenant_id) WHERE status = 'active'"
        if indexes != {"one_active_device_per_tenant": expected_index_sql}:
            raise MigrationInvalid("Schema index inventory mismatch")

    def pragma_inventory(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            return {
                "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
                "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
            }
        finally:
            connection.close()

    def readiness(self, now: datetime) -> tuple[bool, str | None]:
        try:
            connection = self._connect()
            try:
                pragmas = {
                    "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
                    "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                    "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
                    "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
                }
                if pragmas != {"foreign_keys": 1, "journal_mode": "wal", "busy_timeout": 5000, "synchronous": 2}:
                    return False, "DATABASE_UNAVAILABLE"
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    return False, "DATABASE_UNAVAILABLE"
                self._validate_schema(connection)
                applied = {int(row["version"]): dict(row) for row in connection.execute("SELECT * FROM schema_migrations")}
                for version, name, sql in MIGRATIONS:
                    expected = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    if version not in applied or applied[version]["name"] != name or applied[version]["checksum"] != expected:
                        return False, "MIGRATION_INVALID"
                if set(applied) != {version for version, _, _ in MIGRATIONS}:
                    return False, "MIGRATION_INVALID"
                last = connection.execute(
                    "SELECT occurred_at FROM audit_events WHERE event_type='retention.completed' AND outcome='success' ORDER BY occurred_at DESC LIMIT 1"
                ).fetchone()
                if not last or parse_rfc3339(last["occurred_at"]) < now - timedelta(hours=24):
                    return False, "RETENTION_OVERDUE"
                return True, None
            finally:
                connection.close()
        except MigrationInvalid:
            return False, "MIGRATION_INVALID"
        except (sqlite3.Error, RepositoryError, ValueError):
            return False, "DATABASE_UNAVAILABLE"

    def security_key_dependencies_valid(self, *, hash_key_id: str, signing_key_id: str, now: datetime) -> bool:
        stamp = rfc3339(now)
        connection = self._connect()
        try:
            checks = (
                (
                    "SELECT 1 FROM invites WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at>? AND code_hash_key_id!=? LIMIT 1",
                    (stamp, hash_key_id),
                ),
                (
                    "SELECT 1 FROM activation_codes WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at>? AND code_hash_key_id!=? LIMIT 1",
                    (stamp, hash_key_id),
                ),
                (
                    "SELECT 1 FROM sessions WHERE revoked_at IS NULL AND expires_at>? AND (refresh_hash_key_id!=? OR signing_key_id!=?) LIMIT 1",
                    (stamp, hash_key_id, signing_key_id),
                ),
                (
                    "SELECT 1 FROM spent_refresh_credentials s JOIN sessions x ON x.id=s.session_id WHERE x.revoked_at IS NULL AND s.expires_at>? AND s.key_id!=? LIMIT 1",
                    (stamp, hash_key_id),
                ),
                (
                    "SELECT 1 FROM devices WHERE status='active' AND credential_hash_key_id!=? LIMIT 1",
                    (hash_key_id,),
                ),
            )
            return not any(connection.execute(sql, params).fetchone() for sql, params in checks)
        except sqlite3.Error as exc:
            raise RepositoryUnavailable("Security dependency check failed") from exc
        finally:
            connection.close()

    def _insert_audit(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        event_type: str,
        actor_type: str,
        outcome: str,
        reason_code: str,
        request_id: str,
        tenant_id: str | None = None,
        actor_ref: str | None = None,
        target_type: str | None = None,
        target_ref: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if event_type not in AUDIT_EVENT_TYPES or actor_type not in AUDIT_ACTORS or outcome not in AUDIT_OUTCOMES:
            raise ValueError("Audit enum is not allowlisted")
        if target_type is not None and target_type not in TARGET_TYPES:
            raise ValueError("Audit target is not allowlisted")
        if not re.fullmatch(r"[A-Z0-9_]{1,64}", reason_code):
            raise ValueError("Audit reason is invalid")
        clean_metadata: dict[str, Any] = {}
        for key, value in (metadata or {}).items():
            if key not in AUDIT_METADATA_KEYS or not isinstance(value, (str, int, float, bool)) or len(str(value)) > 128:
                raise ValueError("Audit metadata is not allowlisted")
            clean_metadata[key] = value
        connection.execute(
            "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _uuid(), rfc3339(now), tenant_id, actor_type, actor_ref, event_type,
                target_type, target_ref, outcome, reason_code, request_id,
                json.dumps(clean_metadata, sort_keys=True, separators=(",", ":")),
            ),
        )

    def append_audit(self, **values: Any) -> None:
        with self._transaction() as connection:
            self._insert_audit(connection, **values)

    def run_retention(self, now: datetime) -> int:
        t30 = rfc3339(now - timedelta(days=30))
        t180 = rfc3339(now - timedelta(days=180))
        t24h = rfc3339(now - timedelta(hours=24))
        current = rfc3339(now)
        count = 0
        with self._transaction() as connection:
            statements = (
                ("DELETE FROM idempotency_records WHERE expires_at <= ?", (current,)),
                ("DELETE FROM spent_refresh_credentials WHERE expires_at <= ?", (current,)),
                ("DELETE FROM rate_limit_buckets WHERE updated_at <= ? AND (blocked_until IS NULL OR blocked_until <= ?)", (t24h, t24h)),
                ("DELETE FROM activation_codes WHERE (used_at IS NOT NULL AND used_at <= ?) OR (revoked_at IS NOT NULL AND revoked_at <= ?) OR expires_at <= ?", (t30, t30, t30)),
                ("DELETE FROM invites WHERE (used_at IS NOT NULL AND used_at <= ?) OR (revoked_at IS NOT NULL AND revoked_at <= ?) OR expires_at <= ?", (t30, t30, t30)),
                ("DELETE FROM sessions WHERE (revoked_at IS NOT NULL AND revoked_at <= ?) OR expires_at <= ?", (t30, t30)),
                ("DELETE FROM devices WHERE status='revoked' AND revoked_at <= ?", (t30,)),
                ("DELETE FROM audit_events WHERE occurred_at <= ?", (t180,)),
                ("DELETE FROM tenants WHERE status IN ('deletion_pending','deleted') AND deletion_due_at <= ?", (current,)),
            )
            for sql, params in statements:
                count += connection.execute(sql, params).rowcount
            self._insert_audit(
                connection, now=now, event_type="retention.completed", actor_type="system",
                outcome="success", reason_code="SCHEDULED", request_id=_uuid(),
                target_type="system", target_ref=None, metadata={"record_count": count},
            )
        return count

    def create_invite(
        self, *, email: str, code_hash: str, hash_key_id: str, now: datetime,
        expires_at: datetime, request_id: str, actor_ref: str, tenant_id: str | None = None,
    ) -> tuple[str, str]:
        invite_id = _uuid()
        tenant = tenant_id or _uuid()
        stamp = rfc3339(now)
        with self._transaction() as connection:
            if tenant_id is None:
                connection.execute(
                    "INSERT INTO tenants VALUES (?,?,?,?,?,?,?,?)",
                    (tenant, "active", "free_beta", 1, stamp, stamp, None, None),
                )
            else:
                active = connection.execute("SELECT id FROM tenants WHERE id=? AND status='active'", (tenant,)).fetchone()
                if not active:
                    raise NotFound("Tenant unavailable")
            connection.execute(
                "INSERT INTO invites VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (invite_id, tenant, email, code_hash, hash_key_id, "owner", stamp, rfc3339(expires_at), None, None, None, 0),
            )
            self._insert_audit(
                connection, now=now, event_type="invite.created", actor_type="operator", actor_ref=actor_ref,
                outcome="success", reason_code="CREATED", request_id=request_id, tenant_id=tenant,
                target_type="invite", target_ref=invite_id,
            )
        return invite_id, tenant

    def revoke_invite(self, *, invite_id: str, now: datetime, request_id: str, actor_ref: str) -> bool:
        with self._transaction() as connection:
            row = connection.execute("SELECT tenant_id,revoked_at FROM invites WHERE id=?", (invite_id,)).fetchone()
            if not row:
                return False
            if row["revoked_at"] is None:
                connection.execute("UPDATE invites SET revoked_at=? WHERE id=?", (rfc3339(now), invite_id))
                self._insert_audit(
                    connection, now=now, event_type="invite.revoked", actor_type="operator", actor_ref=actor_ref,
                    outcome="success", reason_code="REVOKED", request_id=request_id, tenant_id=row["tenant_id"],
                    target_type="invite", target_ref=invite_id,
                )
            return True

    def register(
        self, *, code_hash: str, hash_key_id: str, email: str, password_hash: str,
        accepted_terms_version: str | None, accepted_privacy_version: str | None,
        session_id: str, refresh_hash: str, signing_key_id: str, now: datetime,
        session_expires_at: datetime, request_id: str,
    ) -> dict[str, Any]:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            invite = connection.execute("SELECT * FROM invites WHERE code_hash=? AND code_hash_key_id=?", (code_hash, hash_key_id)).fetchone()
            valid = bool(
                invite and invite["email_normalized"] == email and invite["used_at"] is None
                and invite["revoked_at"] is None and invite["failed_attempt_count"] < 5
                and parse_rfc3339(invite["expires_at"]) > now
            )
            if not valid:
                if invite and invite["used_at"] is None and invite["revoked_at"] is None:
                    failures = min(5, int(invite["failed_attempt_count"]) + 1)
                    connection.execute(
                        "UPDATE invites SET failed_attempt_count=?, revoked_at=CASE WHEN ? >= 5 THEN ? ELSE revoked_at END WHERE id=?",
                        (failures, failures, stamp, invite["id"]),
                    )
                self._insert_audit(
                    connection, now=now, event_type="invite.denied", actor_type="anonymous",
                    outcome="denied", reason_code="INVALID", request_id=request_id,
                    tenant_id=invite["tenant_id"] if invite else None,
                    target_type="invite" if invite else "system",
                    target_ref=invite["id"] if invite else None,
                )
                raise InvalidSecret("Invite invalid")
            user_id = _uuid()
            try:
                connection.execute(
                    "INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, invite["tenant_id"], email, password_hash, "owner", "active",
                        accepted_terms_version, accepted_privacy_version,
                        stamp if accepted_terms_version is not None or accepted_privacy_version is not None else None,
                        stamp, stamp, None, None, None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidSecret("Invite invalid") from exc
            connection.execute("UPDATE invites SET used_at=?, used_by_user_id=? WHERE id=?", (stamp, user_id, invite["id"]))
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id, invite["tenant_id"], user_id, refresh_hash, hash_key_id, 1,
                    "browser", signing_key_id, stamp, stamp, rfc3339(session_expires_at), None, None,
                ),
            )
            self._insert_audit(
                connection, now=now, event_type="invite.consumed", actor_type="anonymous", outcome="success",
                reason_code="CONSUMED", request_id=request_id, tenant_id=invite["tenant_id"],
                target_type="invite", target_ref=invite["id"],
            )
            self._insert_audit(
                connection, now=now, event_type="auth.registered", actor_type="user", actor_ref=user_id,
                outcome="success", reason_code="REGISTERED", request_id=request_id,
                tenant_id=invite["tenant_id"], target_type="user", target_ref=user_id,
            )
            return {"user_id": user_id, "tenant_id": invite["tenant_id"], "role": "owner", "status": "active"}

    def find_login(self, email: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return _row(
                connection.execute(
                    "SELECT u.*,t.status AS tenant_status,t.plan_code,t.active_device_limit FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE u.email_normalized=?",
                    (email,),
                ).fetchone()
            )
        finally:
            connection.close()

    def login_success(
        self, *, user_id: str, tenant_id: str, session_id: str, refresh_hash: str,
        hash_key_id: str, signing_key_id: str, now: datetime, expires_at: datetime,
        request_id: str, replacement_password_hash: str | None = None,
    ) -> None:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT u.status,t.status AS tenant_status FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE u.id=? AND u.tenant_id=?",
                (user_id, tenant_id),
            ).fetchone()
            if not current or current["status"] != "active" or current["tenant_status"] != "active":
                raise InvalidSecret("Authentication failed")
            if replacement_password_hash:
                connection.execute("UPDATE users SET password_hash=?,updated_at=?,last_login_at=? WHERE id=?", (replacement_password_hash, stamp, stamp, user_id))
            else:
                connection.execute("UPDATE users SET updated_at=?,last_login_at=? WHERE id=?", (stamp, stamp, user_id))
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, tenant_id, user_id, refresh_hash, hash_key_id, 1, "browser", signing_key_id, stamp, stamp, rfc3339(expires_at), None, None),
            )
            self._insert_audit(
                connection, now=now, event_type="auth.login_succeeded", actor_type="user", actor_ref=user_id,
                outcome="success", reason_code="AUTHENTICATED", request_id=request_id,
                tenant_id=tenant_id, target_type="session", target_ref=session_id,
            )

    def authenticate_access(self, *, user_id: str, tenant_id: str, session_id: str, now: datetime) -> dict[str, Any] | None:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            record = connection.execute(
                "SELECT s.*,u.email_normalized,u.role,u.status AS user_status,t.status AS tenant_status,t.plan_code,t.active_device_limit "
                "FROM sessions s JOIN users u ON u.id=s.user_id AND u.tenant_id=s.tenant_id "
                "JOIN tenants t ON t.id=s.tenant_id WHERE s.id=? AND s.user_id=? AND s.tenant_id=?",
                (session_id, user_id, tenant_id),
            ).fetchone()
            valid = bool(
                record and record["revoked_at"] is None and record["user_status"] == "active"
                and record["tenant_status"] == "active" and parse_rfc3339(record["expires_at"]) > now
                and record["role"] == "owner"
            )
            if not valid:
                return None
            connection.execute("UPDATE sessions SET accessed_at=? WHERE id=?", (stamp, session_id))
            result = dict(record)
            result["active_device_count"] = connection.execute(
                "SELECT COUNT(*) FROM devices WHERE tenant_id=? AND status='active'", (tenant_id,)
            ).fetchone()[0]
            return result

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return _row(
                connection.execute(
                    "SELECT s.*,u.role,u.status AS user_status,t.status AS tenant_status FROM sessions s "
                    "JOIN users u ON u.id=s.user_id AND u.tenant_id=s.tenant_id JOIN tenants t ON t.id=s.tenant_id WHERE s.id=?",
                    (session_id,),
                ).fetchone()
            )
        finally:
            connection.close()

    def spent_refresh_credentials(self, session_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT session_id,generation,token_hash,key_id,expires_at FROM spent_refresh_credentials WHERE session_id=? ORDER BY generation",
                    (session_id,),
                )
            ]
        finally:
            connection.close()

    def rotate_refresh(
        self, *, session_id: str, expected_generation: int, current_hash: str,
        new_hash: str, hash_key_id: str, now: datetime, request_id: str,
    ) -> dict[str, Any]:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT s.*,u.role,u.status AS user_status,t.status AS tenant_status FROM sessions s "
                "JOIN users u ON u.id=s.user_id AND u.tenant_id=s.tenant_id JOIN tenants t ON t.id=s.tenant_id WHERE s.id=?",
                (session_id,),
            ).fetchone()
            if not session:
                raise InvalidSecret("Invalid credential")
            if session["revoked_at"] is not None:
                raise InvalidSecret("Invalid credential")
            if session["refresh_generation"] != expected_generation or session["refresh_token_hash"] != current_hash:
                connection.execute("UPDATE sessions SET revoked_at=?,revoke_reason='refresh_replay' WHERE id=?", (stamp, session_id))
                self._insert_audit(
                    connection, now=now, event_type="auth.refresh_denied", actor_type="anonymous",
                    outcome="denied", reason_code="REFRESH_REPLAY", request_id=request_id,
                    tenant_id=session["tenant_id"], target_type="session", target_ref=session_id,
                )
                raise InvalidSecret("Invalid credential")
            if (
                session["user_status"] != "active" or session["tenant_status"] != "active"
                or parse_rfc3339(session["expires_at"]) <= now
            ):
                raise InvalidSecret("Invalid credential")
            generation = expected_generation + 1
            connection.execute(
                "INSERT INTO spent_refresh_credentials(session_id,generation,token_hash,key_id,expires_at) VALUES(?,?,?,?,?)",
                (
                    session_id, int(session["refresh_generation"]), session["refresh_token_hash"],
                    session["refresh_hash_key_id"], session["expires_at"],
                ),
            )
            connection.execute(
                "UPDATE sessions SET refresh_token_hash=?,refresh_hash_key_id=?,refresh_generation=?,accessed_at=? WHERE id=?",
                (new_hash, hash_key_id, generation, stamp, session_id),
            )
            self._insert_audit(
                connection, now=now, event_type="auth.refresh_succeeded", actor_type="user", actor_ref=session["user_id"],
                outcome="success", reason_code="ROTATED", request_id=request_id, tenant_id=session["tenant_id"],
                target_type="session", target_ref=session_id,
            )
            return {**dict(session), "refresh_generation": generation}

    def revoke_proven_refresh_replay(
        self, *, session_id: str, generation: int, token_hash: str, key_id: str,
        now: datetime, request_id: str,
    ) -> bool:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            spent = connection.execute(
                "SELECT * FROM spent_refresh_credentials WHERE session_id=? AND generation=? AND token_hash=? AND key_id=?",
                (session_id, generation, token_hash, key_id),
            ).fetchone()
            session = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not spent or not session:
                return False
            if session["revoked_at"] is None:
                connection.execute(
                    "UPDATE sessions SET revoked_at=?,revoke_reason='refresh_replay' WHERE id=?",
                    (stamp, session_id),
                )
                self._insert_audit(
                    connection, now=now, event_type="auth.refresh_denied", actor_type="anonymous",
                    outcome="denied", reason_code="REFRESH_REPLAY", request_id=request_id,
                    tenant_id=session["tenant_id"], target_type="session", target_ref=session_id,
                )
            return True

    def revoke_session_by_refresh(
        self, *, session_id: str, current_hash: str, now: datetime, request_id: str,
    ) -> bool:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            session = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session or session["refresh_token_hash"] != current_hash:
                return False
            if session["revoked_at"] is None:
                connection.execute("UPDATE sessions SET revoked_at=?,revoke_reason='logout' WHERE id=?", (stamp, session_id))
                self._insert_audit(
                    connection, now=now, event_type="auth.logout", actor_type="user", actor_ref=session["user_id"],
                    outcome="success", reason_code="LOGOUT", request_id=request_id, tenant_id=session["tenant_id"],
                    target_type="session", target_ref=session_id,
                )
            return True

    def operator_revoke_session(self, *, session_id: str, now: datetime, request_id: str, actor_ref: str) -> bool:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            session = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                return False
            if session["revoked_at"] is None:
                connection.execute("UPDATE sessions SET revoked_at=?,revoke_reason='operator_security' WHERE id=?", (stamp, session_id))
                self._insert_audit(
                    connection, now=now, event_type="session.revoked", actor_type="operator", actor_ref=actor_ref,
                    outcome="success", reason_code="OPERATOR_SECURITY", request_id=request_id,
                    tenant_id=session["tenant_id"], target_type="session", target_ref=session_id,
                )
            return True

    def suspend_tenant(self, *, tenant_id: str, now: datetime, request_id: str, actor_ref: str) -> bool:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            tenant = connection.execute("SELECT status FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            if not tenant:
                return False
            connection.execute("UPDATE tenants SET status='suspended',updated_at=? WHERE id=?", (stamp, tenant_id))
            connection.execute(
                "UPDATE sessions SET revoked_at=?,revoke_reason='user_suspended' WHERE tenant_id=? AND revoked_at IS NULL",
                (stamp, tenant_id),
            )
            connection.execute(
                "UPDATE devices SET status='revoked',revoked_at=?,revoke_reason='account_suspended',updated_at=? WHERE tenant_id=? AND status='active'",
                (stamp, stamp, tenant_id),
            )
            self._insert_audit(
                connection, now=now, event_type="tenant.suspended", actor_type="operator", actor_ref=actor_ref,
                outcome="success", reason_code="SUSPENDED", request_id=request_id, tenant_id=tenant_id,
                target_type="tenant", target_ref=tenant_id,
            )
            return True

    def consume_rate_limit(
        self, *, scope: str, key_hash: str, limit: int, window_seconds: int,
        block_seconds: int, now: datetime,
    ) -> tuple[bool, int]:
        if scope not in RATE_SCOPES:
            raise ValueError("Rate scope not allowlisted")
        epoch = int(now.timestamp())
        window_start = datetime.fromtimestamp(epoch - (epoch % window_seconds), timezone.utc)
        started = rfc3339(window_start)
        stamp = rfc3339(now)
        with self._transaction() as connection:
            bucket = connection.execute(
                "SELECT * FROM rate_limit_buckets WHERE scope=? AND key_hash=? AND window_started_at=?",
                (scope, key_hash, started),
            ).fetchone()
            if bucket and bucket["blocked_until"] and parse_rfc3339(bucket["blocked_until"]) > now:
                retry = max(1, int((parse_rfc3339(bucket["blocked_until"]) - now).total_seconds()))
                return False, retry
            count = (int(bucket["count"]) if bucket else 0) + 1
            blocked_until = rfc3339(now + timedelta(seconds=block_seconds)) if count > limit else None
            connection.execute(
                "INSERT INTO rate_limit_buckets(scope,key_hash,window_started_at,updated_at,blocked_until,count) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(scope,key_hash,window_started_at) DO UPDATE SET updated_at=excluded.updated_at,blocked_until=excluded.blocked_until,count=excluded.count",
                (scope, key_hash, started, stamp, blocked_until, count),
            )
            return (count <= limit), (block_seconds if count > limit else 0)

    def create_activation_code(
        self, *, tenant_id: str, user_id: str, code_hash: str, hash_key_id: str,
        idempotency_key_hash: str, fingerprint: str, now: datetime, expires_at: datetime,
        request_id: str,
    ) -> str:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM idempotency_records WHERE operation='activation_code_create' AND key_hash=?",
                (idempotency_key_hash,),
            ).fetchone()
            if existing and parse_rfc3339(existing["expires_at"]) > now:
                if existing["tenant_id"] != tenant_id or existing["request_fingerprint"] != fingerprint:
                    raise Conflict("Idempotency conflict")
                # Rotate the still-pending code.  This preserves the resource and
                # keeps each plaintext issuance single-display without caching it.
                activation_id = existing["resource_ref"]
                row = connection.execute(
                    "SELECT used_at,revoked_at FROM activation_codes WHERE id=? AND tenant_id=?",
                    (activation_id, tenant_id),
                ).fetchone()
                if not row or row["used_at"] is not None or row["revoked_at"] is not None:
                    raise Conflict("Idempotency outcome no longer retryable")
                connection.execute(
                    "UPDATE activation_codes SET code_hash=?,code_hash_key_id=?,created_at=?,expires_at=?,failed_attempt_count=0 WHERE id=?",
                    (code_hash, hash_key_id, stamp, rfc3339(expires_at), activation_id),
                )
                return activation_id
            connection.execute(
                "UPDATE activation_codes SET revoked_at=? WHERE user_id=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?",
                (stamp, user_id, stamp),
            )
            activation_id = _uuid()
            connection.execute(
                "INSERT INTO activation_codes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (activation_id, tenant_id, user_id, code_hash, hash_key_id, stamp, rfc3339(expires_at), None, None, None, 0, None, None, None),
            )
            connection.execute(
                "INSERT INTO idempotency_records VALUES (?,?,?,?,?,?,?)",
                (idempotency_key_hash, tenant_id, "activation_code_create", fingerprint, activation_id, stamp, rfc3339(now + timedelta(hours=24))),
            )
            self._insert_audit(
                connection, now=now, event_type="activation.created", actor_type="user", actor_ref=user_id,
                outcome="success", reason_code="CREATED", request_id=request_id, tenant_id=tenant_id,
                target_type="activation_code", target_ref=activation_id,
            )
            return activation_id

    def activate_device(
        self, *, code_hash: str, hash_key_id: str, instance_hash: str,
        credential_hash: str, idempotency_key_hash: str, fingerprint: str,
        app_version: str, os_family: str, now: datetime, request_id: str,
    ) -> tuple[dict[str, Any], bool]:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            code = connection.execute(
                "SELECT * FROM activation_codes WHERE code_hash=? AND code_hash_key_id=?", (code_hash, hash_key_id)
            ).fetchone()
            if not code:
                self._insert_audit(
                    connection, now=now, event_type="activation.denied", actor_type="anonymous",
                    outcome="denied", reason_code="INVALID", request_id=request_id,
                    target_type="system", target_ref=None,
                )
                raise InvalidSecret("Activation failed")
            if code["used_at"] is not None:
                exact_retry = bool(
                    code["activated_device_id"] and code["idempotency_key_hash"] == idempotency_key_hash
                    and code["device_instance_id_hash"] == instance_hash and code["idempotency_until"]
                    and parse_rfc3339(code["idempotency_until"]) > now
                )
                record = connection.execute(
                    "SELECT * FROM idempotency_records WHERE operation='device_activate' AND key_hash=?",
                    (idempotency_key_hash,),
                ).fetchone()
                exact_retry = exact_retry and bool(record and record["request_fingerprint"] == fingerprint)
                if not exact_retry:
                    self._insert_audit(
                        connection, now=now, event_type="activation.denied", actor_type="anonymous",
                        outcome="denied", reason_code="REPLAY", request_id=request_id,
                        tenant_id=code["tenant_id"], target_type="activation_code", target_ref=code["id"],
                    )
                    raise InvalidSecret("Activation failed")
                device = connection.execute(
                    "SELECT * FROM devices WHERE id=? AND tenant_id=? AND status='active'",
                    (code["activated_device_id"], code["tenant_id"]),
                ).fetchone()
                if not device:
                    raise InvalidSecret("Activation failed")
                generation = int(device["credential_generation"]) + 1
                connection.execute(
                    "UPDATE devices SET credential_hash=?,credential_hash_key_id=?,credential_generation=?,app_version=?,os_family=?,updated_at=? WHERE id=?",
                    (credential_hash, hash_key_id, generation, app_version, os_family, stamp, device["id"]),
                )
                result = dict(device)
                result.update(credential_hash=credential_hash, credential_hash_key_id=hash_key_id, credential_generation=generation, app_version=app_version, os_family=os_family, updated_at=stamp)
                return result, True
            valid = bool(
                code["revoked_at"] is None and code["failed_attempt_count"] < 5
                and parse_rfc3339(code["expires_at"]) > now
            )
            account = connection.execute(
                "SELECT u.status AS user_status,t.status AS tenant_status,t.active_device_limit FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE u.id=? AND u.tenant_id=?",
                (code["user_id"], code["tenant_id"]),
            ).fetchone()
            if not valid or not account or account["user_status"] != "active" or account["tenant_status"] != "active":
                failures = min(5, int(code["failed_attempt_count"]) + 1)
                connection.execute(
                    "UPDATE activation_codes SET failed_attempt_count=?,revoked_at=CASE WHEN ?>=5 THEN ? ELSE revoked_at END WHERE id=?",
                    (failures, failures, stamp, code["id"]),
                )
                self._insert_audit(
                    connection, now=now, event_type="activation.denied", actor_type="anonymous",
                    outcome="denied", reason_code="INVALID", request_id=request_id,
                    tenant_id=code["tenant_id"], target_type="activation_code", target_ref=code["id"],
                )
                raise InvalidSecret("Activation failed")
            active_count = connection.execute(
                "SELECT COUNT(*) FROM devices WHERE tenant_id=? AND status='active'", (code["tenant_id"],)
            ).fetchone()[0]
            if active_count >= int(account["active_device_limit"]):
                raise DeviceLimitReached("Device limit reached")
            existing_idem = connection.execute(
                "SELECT * FROM idempotency_records WHERE operation='device_activate' AND key_hash=?", (idempotency_key_hash,)
            ).fetchone()
            if existing_idem:
                if existing_idem["request_fingerprint"] != fingerprint:
                    raise Conflict("Idempotency conflict")
                raise InvalidSecret("Activation failed")
            device_id = _uuid()
            retry_until = now + timedelta(minutes=5)
            try:
                connection.execute(
                    "INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (device_id, code["tenant_id"], code["user_id"], instance_hash, credential_hash, hash_key_id, 1, "active", app_version, os_family, stamp, None, stamp, None, None),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed: devices.tenant_id" in str(exc):
                    raise DeviceLimitReached("Device limit reached") from exc
                raise RepositoryUnavailable("Device transaction failed") from exc
            connection.execute(
                "UPDATE activation_codes SET used_at=?,activated_device_id=?,idempotency_key_hash=?,idempotency_until=?,device_instance_id_hash=? WHERE id=?",
                (stamp, device_id, idempotency_key_hash, rfc3339(retry_until), instance_hash, code["id"]),
            )
            connection.execute(
                "INSERT INTO idempotency_records VALUES (?,?,?,?,?,?,?)",
                (idempotency_key_hash, code["tenant_id"], "device_activate", fingerprint, device_id, stamp, rfc3339(retry_until)),
            )
            self._insert_audit(
                connection, now=now, event_type="activation.consumed", actor_type="anonymous",
                outcome="success", reason_code="CONSUMED", request_id=request_id, tenant_id=code["tenant_id"],
                target_type="device", target_ref=device_id, metadata={"app_version": app_version, "os_family": os_family, "credential_generation": 1},
            )
            return {
                "id": device_id, "tenant_id": code["tenant_id"], "user_id": code["user_id"],
                "credential_generation": 1, "status": "active", "app_version": app_version,
                "os_family": os_family, "activated_at": stamp, "last_heartbeat_at": None,
                "updated_at": stamp,
            }, False

    def get_device_principal(self, device_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            return _row(
                connection.execute(
                    "SELECT d.*,u.status AS user_status,t.status AS tenant_status FROM devices d "
                    "JOIN users u ON u.id=d.user_id AND u.tenant_id=d.tenant_id JOIN tenants t ON t.id=d.tenant_id WHERE d.id=?",
                    (device_id,),
                ).fetchone()
            )
        finally:
            connection.close()

    def list_devices(self, tenant_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            return [
                dict(row) for row in connection.execute(
                    "SELECT id,status,app_version,os_family,activated_at,last_heartbeat_at,revoked_at FROM devices WHERE tenant_id=? ORDER BY activated_at",
                    (tenant_id,),
                )
            ]
        finally:
            connection.close()

    def revoke_device(
        self, *, tenant_id: str, user_id: str, device_id: str, reason: str,
        idempotency_key_hash: str, fingerprint: str, now: datetime, request_id: str,
    ) -> None:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            device = connection.execute("SELECT * FROM devices WHERE id=? AND tenant_id=?", (device_id, tenant_id)).fetchone()
            if not device:
                raise NotFound("Device unavailable")
            idem = connection.execute(
                "SELECT * FROM idempotency_records WHERE operation='device_revoke' AND key_hash=?", (idempotency_key_hash,)
            ).fetchone()
            if idem:
                if idem["tenant_id"] != tenant_id or idem["request_fingerprint"] != fingerprint or idem["resource_ref"] != device_id:
                    raise Conflict("Idempotency conflict")
                return
            if device["status"] == "active":
                connection.execute(
                    "UPDATE devices SET status='revoked',revoked_at=?,revoke_reason=?,updated_at=? WHERE id=? AND tenant_id=?",
                    (stamp, reason, stamp, device_id, tenant_id),
                )
                self._insert_audit(
                    connection, now=now, event_type="device.revoked", actor_type="user", actor_ref=user_id,
                    outcome="success", reason_code="REVOKED", request_id=request_id, tenant_id=tenant_id,
                    target_type="device", target_ref=device_id,
                )
            connection.execute(
                "INSERT INTO idempotency_records VALUES (?,?,?,?,?,?,?)",
                (idempotency_key_hash, tenant_id, "device_revoke", fingerprint, device_id, stamp, rfc3339(now + timedelta(hours=24))),
            )

    def heartbeat(
        self, *, device_id: str, tenant_id: str, app_version: str, os_family: str, now: datetime,
    ) -> bool:
        stamp = rfc3339(now)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE devices SET app_version=?,os_family=?,last_heartbeat_at=?,updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='active' "
                "AND EXISTS(SELECT 1 FROM users u WHERE u.id=devices.user_id AND u.tenant_id=? AND u.status='active') "
                "AND EXISTS(SELECT 1 FROM tenants t WHERE t.id=? AND t.status='active')",
                (app_version, os_family, stamp, stamp, device_id, tenant_id, tenant_id, tenant_id),
            ).rowcount
            return changed == 1

    def table_rows(self, table: str) -> list[dict[str, Any]]:
        if table not in EXPECTED_COLUMNS:
            raise ValueError("Unknown table")
        connection = self._connect()
        try:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
        finally:
            connection.close()


def verify_restored_database(restored_sqlite_path: str | Path) -> bool:
    """Non-mutating Operations hook for restored SQLite snapshots.

    The callable raises ``MigrationInvalid``/``RepositoryUnavailable`` on any
    failed invariant and returns ``True`` only after exact schema, checksum,
    integrity, tenant/device/session, and audit-allowlist checks pass.  It does
    not print rows, paths, credentials, or identifiers.
    """

    path = Path(restored_sqlite_path).expanduser().resolve(strict=False)
    if not path.is_absolute() or path.suffix != ".sqlite3" or not path.is_file():
        raise RepositoryUnavailable("Restore candidate is unavailable")
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RepositoryUnavailable("Restore integrity failed")
        SQLiteRepository(path)._validate_schema(connection)
        applied = {int(row["version"]): dict(row) for row in connection.execute("SELECT * FROM schema_migrations")}
        if set(applied) != {version for version, _, _ in MIGRATIONS}:
            raise MigrationInvalid("Restore migration inventory mismatch")
        for version, name, sql in MIGRATIONS:
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if applied[version]["name"] != name or applied[version]["checksum"] != checksum:
                raise MigrationInvalid("Restore migration checksum mismatch")

        invariant_queries = (
            "SELECT 1 FROM sessions s LEFT JOIN users u ON u.id=s.user_id AND u.tenant_id=s.tenant_id LEFT JOIN tenants t ON t.id=s.tenant_id WHERE s.revoked_at IS NULL AND (u.status!='active' OR t.status!='active' OR u.id IS NULL OR t.id IS NULL) LIMIT 1",
            "SELECT 1 FROM devices d LEFT JOIN users u ON u.id=d.user_id AND u.tenant_id=d.tenant_id LEFT JOIN tenants t ON t.id=d.tenant_id WHERE d.status='active' AND (u.status!='active' OR t.status!='active' OR u.id IS NULL OR t.id IS NULL) LIMIT 1",
            "SELECT 1 FROM devices WHERE status='revoked' AND (revoked_at IS NULL OR revoke_reason IS NULL) LIMIT 1",
            "SELECT 1 FROM sessions WHERE revoked_at IS NOT NULL AND revoke_reason IS NULL LIMIT 1",
            "SELECT 1 FROM invites WHERE used_at IS NOT NULL AND used_by_user_id IS NULL LIMIT 1",
            "SELECT 1 FROM activation_codes WHERE used_at IS NOT NULL AND activated_device_id IS NULL LIMIT 1",
            "SELECT 1 FROM devices WHERE status='active' GROUP BY tenant_id HAVING COUNT(*) > 1 LIMIT 1",
        )
        if any(connection.execute(query).fetchone() is not None for query in invariant_queries):
            raise RepositoryUnavailable("Restore recovery invariant failed")

        for row in connection.execute("SELECT event_type,actor_type,target_type,outcome,reason_code,metadata_json FROM audit_events"):
            if row["event_type"] not in AUDIT_EVENT_TYPES or row["actor_type"] not in AUDIT_ACTORS or row["outcome"] not in AUDIT_OUTCOMES:
                raise RepositoryUnavailable("Restore audit allowlist failed")
            if row["target_type"] is not None and row["target_type"] not in TARGET_TYPES:
                raise RepositoryUnavailable("Restore audit allowlist failed")
            if not re.fullmatch(r"[A-Z0-9_]{1,64}", row["reason_code"]):
                raise RepositoryUnavailable("Restore audit allowlist failed")
            metadata = json.loads(row["metadata_json"])
            if not isinstance(metadata, dict) or not set(metadata) <= AUDIT_METADATA_KEYS:
                raise RepositoryUnavailable("Restore audit allowlist failed")
        return True
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RepositoryUnavailable("Restore verification failed") from exc
    finally:
        if "connection" in locals():
            connection.close()
