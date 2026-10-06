from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
from pathlib import Path
import sqlite3
import sys
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from lca.control_config import ControlPlaneSettings
from lca.control_repository import EXPECTED_COLUMNS, InvalidSecret, RepositoryUnavailable
from lca.control_security import hash_password, issue_opaque_credential, new_code, parse_opaque_credential
from tests.control_support import ControlPlaneTestCase, refresh_cookie


class ControlRepositoryTests(ControlPlaneTestCase):
    def test_complete_isolated_runtime_is_ready_and_pragmas_are_enforced(self) -> None:
        self.assertEqual(200, self.client.get("/v1/health/ready").status_code)
        self.assertEqual(
            {"foreign_keys": 1, "journal_mode": "wal", "busy_timeout": 5000, "synchronous": 2},
            self.repository.pragma_inventory(),
        )
        info = self.client.get("/api/control-plane/info").get_json()
        self.assertTrue(info["productionReady"])
        self.assertFalse(info["externalLaunchAuthorized"])

    def test_schema_inventory_is_exact_including_replay_amendment(self) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            actual_tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            }
            self.assertEqual(set(EXPECTED_COLUMNS), actual_tables)
            for table, expected in EXPECTED_COLUMNS.items():
                actual = tuple(row["name"] for row in connection.execute(f"PRAGMA table_info({table})"))
                self.assertEqual(expected, actual, table)
        finally:
            connection.close()
        self.assertEqual(
            ("session_id", "generation", "token_hash", "key_id", "expires_at"),
            EXPECTED_COLUMNS["spent_refresh_credentials"],
        )

    def test_migration_repeat_is_noop_and_checksum_mutation_fails_readiness(self) -> None:
        before = self.repository.table_rows("schema_migrations")
        self.repository.initialize()
        self.assertEqual(before, self.repository.table_rows("schema_migrations"))
        with self.repository._transaction() as connection:
            connection.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=1")
        response = self.client.get("/v1/health/ready")
        self.assertEqual(503, response.status_code)
        self.assertIn("MIGRATION_INVALID", response.get_json()["error"]["details"]["reasons"])

    def test_retention_is_idempotent_and_bounded(self) -> None:
        old = self.clock() - timedelta(days=181)
        self.repository.append_audit(
            now=old, event_type="readiness.failed", actor_type="system", outcome="failure",
            reason_code="NOT_READY", request_id=str(uuid.uuid4()), target_type="system", target_ref=None,
        )
        first = self.repository.run_retention(self.clock())
        second = self.repository.run_retention(self.clock())
        self.assertEqual(1, first)
        self.assertEqual(0, second)
        self.assertTrue(all(row["occurred_at"] > old.isoformat() for row in self.repository.table_rows("audit_events")))

    def test_persistence_survives_app_restart(self) -> None:
        response = self.register()
        self.assertEqual(201, response.status_code)
        access = response.get_json()["access_token"]
        self.build_app()
        me = self.client.get("/v1/me", headers=self.access_headers(access))
        self.assertEqual(200, me.status_code)
        self.assertEqual("free_beta", me.get_json()["tenant"]["plan_code"])

    def test_register_transaction_rolls_back_on_injected_session_failure(self) -> None:
        code = self.bootstrap()
        with self.repository._transaction() as connection:
            connection.execute(
                "CREATE TRIGGER reject_session BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        session_id = str(uuid.uuid4())
        _, refresh_secret = issue_opaque_credential(session_id)
        with self.assertRaises(RepositoryUnavailable):
            self.repository.register(
                code_hash=self.runtime.hasher.digest("invite", code), hash_key_id=self.runtime.hasher.key_id,
                email="owner@example.test", password_hash=hash_password("Correct horse battery staple!"),
                accepted_terms_version=None, accepted_privacy_version=None,
                session_id=session_id, refresh_hash=self.runtime.hasher.digest("refresh", refresh_secret),
                signing_key_id="test-ed25519-1", now=self.clock(),
                session_expires_at=self.clock() + timedelta(days=30), request_id=str(uuid.uuid4()),
            )
        self.assertEqual([], self.repository.table_rows("users"))
        invite = self.repository.table_rows("invites")[0]
        self.assertIsNone(invite["used_at"])
        self.assertIsNone(invite["used_by_user_id"])

    def test_refresh_transaction_rolls_back_on_injected_spent_insert_failure(self) -> None:
        registered = self.register()
        session_id, current_secret = parse_opaque_credential(refresh_cookie(registered))
        before = self.repository.get_session(session_id)
        with self.repository._transaction() as connection:
            connection.execute(
                "CREATE TRIGGER reject_spent BEFORE INSERT ON spent_refresh_credentials BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        replacement_secret = new_code(32)
        with self.assertRaises(RepositoryUnavailable):
            self.repository.rotate_refresh(
                session_id=session_id, expected_generation=1,
                current_hash=self.runtime.hasher.digest("refresh", current_secret),
                new_hash=self.runtime.hasher.digest("refresh", replacement_secret),
                hash_key_id=self.runtime.hasher.key_id, now=self.clock(), request_id=str(uuid.uuid4()),
            )
        after = self.repository.get_session(session_id)
        self.assertEqual(before["refresh_token_hash"], after["refresh_token_hash"])
        self.assertEqual(1, after["refresh_generation"])
        self.assertEqual([], self.repository.spent_refresh_credentials(session_id))

    def test_activation_transaction_rolls_back_on_injected_device_insert_failure(self) -> None:
        registered = self.register()
        access = registered.get_json()["access_token"]
        code = self.activation_code(access).get_json()["activation_code"]
        instance = str(uuid.uuid4())
        fields = {
            "activation_code_hash": self.runtime.hasher.digest("activation", code),
            "device_instance_id_hash": self.runtime.hasher.digest("device-instance", instance),
            "app_version": "1.0.0",
            "os_family": "windows",
        }
        with self.repository._transaction() as connection:
            connection.execute(
                "CREATE TRIGGER reject_device BEFORE INSERT ON devices BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        with self.assertRaises(RepositoryUnavailable):
            self.repository.activate_device(
                code_hash=fields["activation_code_hash"], hash_key_id=self.runtime.hasher.key_id,
                instance_hash=fields["device_instance_id_hash"],
                credential_hash=self.runtime.hasher.digest("device", new_code(32)),
                idempotency_key_hash=self.runtime.hasher.digest("idempotency", "injected-activate-key-01"),
                fingerprint=self.runtime.hasher.fingerprint("device_activate", fields),
                app_version="1.0.0", os_family="windows", now=self.clock(), request_id=str(uuid.uuid4()),
            )
        self.assertEqual([], self.repository.table_rows("devices"))
        activation = self.repository.table_rows("activation_codes")[0]
        self.assertIsNone(activation["used_at"])
        self.assertIsNone(activation["activated_device_id"])
        self.assertFalse(any(row["operation"] == "device_activate" for row in self.repository.table_rows("idempotency_records")))

    def test_revoke_transaction_rolls_back_on_injected_update_failure(self) -> None:
        registered = self.register()
        access = registered.get_json()["access_token"]
        code = self.activation_code(access).get_json()["activation_code"]
        activated = self.activate(code).get_json()
        device = self.repository.get_device_principal(activated["device_id"])
        fields = {"device_id": device["id"], "reason": "security"}
        with self.repository._transaction() as connection:
            connection.execute(
                "CREATE TRIGGER reject_revoke BEFORE UPDATE OF status ON devices WHEN NEW.status='revoked' BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        with self.assertRaises(RepositoryUnavailable):
            self.repository.revoke_device(
                tenant_id=device["tenant_id"], user_id=device["user_id"], device_id=device["id"],
                reason="security", idempotency_key_hash=self.runtime.hasher.digest("idempotency", "injected-revoke-key-001"),
                fingerprint=self.runtime.hasher.fingerprint("device_revoke", fields),
                now=self.clock(), request_id=str(uuid.uuid4()),
            )
        self.assertEqual("active", self.repository.get_device_principal(device["id"])["status"])
        self.assertFalse(any(row["operation"] == "device_revoke" for row in self.repository.table_rows("idempotency_records")))

    def test_concurrent_invite_consumption_has_one_success(self) -> None:
        code = self.bootstrap()
        hasher = self.runtime.hasher
        code_hash = hasher.digest("invite", code)

        def consume(index: int) -> bool:
            session_id = str(uuid.uuid4())
            _, refresh_secret = issue_opaque_credential(session_id)
            try:
                self.repository.register(
                    code_hash=code_hash, hash_key_id=hasher.key_id,
                    email="owner@example.test", password_hash=hash_password("Correct horse battery staple!"),
                    accepted_terms_version=None, accepted_privacy_version=None,
                    session_id=session_id, refresh_hash=hasher.digest("refresh", refresh_secret),
                    signing_key_id="test-ed25519-1", now=self.clock(),
                    session_expires_at=self.clock() + timedelta(days=30), request_id=str(uuid.uuid4()),
                )
                return True
            except InvalidSecret:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(consume, (1, 2)))
        self.assertEqual(1, results.count(True))
        self.assertEqual(1, len(self.repository.table_rows("users")))

    def test_concurrent_rate_limit_updates_are_atomic(self) -> None:
        key_hash = self.runtime.hasher.digest("rate-limit", "atomic-rate-test")

        def consume(_: int) -> bool:
            allowed, _ = self.repository.consume_rate_limit(
                scope="login_network", key_hash=key_hash, limit=5,
                window_seconds=900, block_seconds=1800, now=self.clock(),
            )
            return allowed

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(consume, range(10)))
        self.assertEqual(5, results.count(True))
        bucket = self.repository.table_rows("rate_limit_buckets")[0]
        self.assertEqual(6, bucket["count"])
        self.assertIsNotNone(bucket["blocked_until"])


class DataPathValidationTests(ControlPlaneTestCase):
    def test_relative_and_temp_paths_fail_without_creating_database(self) -> None:
        relative = ControlPlaneSettings.from_env(
            {
                "LCA_DATA_DIR": "relative-data",
                "LCA_DB_PATH": "relative-data/control.sqlite3",
            },
            bind_host="127.0.0.1", maintenance_mode=False, testing=False,
        )
        self.assertIn("DATA_PATH_INVALID", relative.issues)
        temp_without_override = ControlPlaneSettings.from_env(
            {
                **self.overrides,
                "LCA_CP_ALLOW_INSECURE_TEST_MODE": False,
            },
            bind_host="127.0.0.1", maintenance_mode=False, testing=True,
        )
        self.assertIn("DATA_PATH_INVALID", temp_without_override.issues)


if __name__ == "__main__":
    import unittest
    unittest.main()
