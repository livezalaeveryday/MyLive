from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import json
import secrets
import sys
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from lca.control_repository import AUDIT_EVENT_TYPES, AUDIT_METADATA_KEYS, RepositoryUnavailable, verify_restored_database
from tests.control_support import ControlPlaneTestCase, ORIGIN, PASSWORD, refresh_cookie


class DeviceLifecycleTests(ControlPlaneTestCase):
    def _registered_access(self) -> str:
        response = self.register()
        self.assertEqual(201, response.status_code)
        return response.get_json()["access_token"]

    def test_activation_exact_retry_rotates_credential_without_duplicate_device(self) -> None:
        access = self._registered_access()
        code_response = self.activation_code(access)
        code = code_response.get_json()["activation_code"]
        instance = str(uuid.uuid4())
        first = self.activate(code, instance_id=instance)
        self.assertEqual(201, first.status_code)
        first_body = first.get_json()
        retry = self.activate(code, instance_id=instance)
        self.assertEqual(200, retry.status_code)
        retry_body = retry.get_json()
        self.assertEqual(first_body["device_id"], retry_body["device_id"])
        self.assertEqual(2, retry_body["credential_generation"])
        self.assertNotEqual(first_body["device_credential"], retry_body["device_credential"])
        self.assertEqual(1, len(self.repository.table_rows("devices")))
        old_denied = self.client.post(
            f"/v1/devices/{first_body['device_id']}/heartbeat",
            headers={"Authorization": f"Bearer {first_body['device_credential']}"},
            json={"app_version": "1.0.1", "os_family": "windows"},
        )
        self.assertEqual(401, old_denied.status_code)
        current = self.client.post(
            f"/v1/devices/{first_body['device_id']}/heartbeat",
            headers={"Authorization": f"Bearer {retry_body['device_credential']}"},
            json={"app_version": "1.0.1", "os_family": "windows"},
        )
        self.assertEqual(200, current.status_code)

    def test_concurrent_activation_attempts_converge_to_one_device(self) -> None:
        access = self._registered_access()
        code = self.activation_code(access).get_json()["activation_code"]

        def attempt(index: int):
            client = self.app.test_client()
            return client.post(
                "/v1/devices/activate",
                headers={"Origin": ORIGIN, "Idempotency-Key": f"concurrent-activate-key-{index:04d}"},
                json={
                    "activation_code": code, "device_instance_id": str(uuid.uuid4()),
                    "app_version": "1.0.0", "os_family": "windows",
                },
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(attempt, (1, 2)))
        self.assertEqual(1, statuses.count(201))
        self.assertEqual(1, len(self.repository.table_rows("devices")))

    def test_activation_code_idempotent_retry_rotates_pending_plaintext_once(self) -> None:
        access = self._registered_access()
        first = self.activation_code(access, "activation-code-retry-0001")
        second = self.activation_code(access, "activation-code-retry-0001")
        self.assertEqual((201, 201), (first.status_code, second.status_code))
        self.assertNotEqual(first.get_json()["activation_code"], second.get_json()["activation_code"])
        self.assertEqual(1, len(self.repository.table_rows("activation_codes")))
        old_denied = self.activate(first.get_json()["activation_code"], key="activate-old-code-key-01")
        self.assertEqual(400, old_denied.status_code)
        current = self.activate(second.get_json()["activation_code"], key="activate-new-code-key-01")
        self.assertEqual(201, current.status_code)

    def test_one_active_device_cap_revoke_and_replacement(self) -> None:
        access = self._registered_access()
        first_code = self.activation_code(access, "activation-code-key-0001").get_json()["activation_code"]
        first = self.activate(first_code)
        device_id = first.get_json()["device_id"]
        second_code = self.activation_code(access, "activation-code-key-0002").get_json()["activation_code"]
        capped = self.activate(second_code, key="device-activate-key-0002")
        self.assertEqual((409, "DEVICE_LIMIT_REACHED"), (capped.status_code, capped.get_json()["error"]["code"]))
        revoked = self.client.post(
            f"/v1/devices/{device_id}/revoke",
            headers={**self.access_headers(access), "Idempotency-Key": "device-revoke-key-0001"},
            json={"reason": "device_replaced"},
        )
        self.assertEqual(204, revoked.status_code)
        repeat = self.client.post(
            f"/v1/devices/{device_id}/revoke",
            headers={**self.access_headers(access), "Idempotency-Key": "device-revoke-key-0001"},
            json={"reason": "device_replaced"},
        )
        self.assertEqual(204, repeat.status_code)
        replacement = self.activate(second_code, key="device-activate-key-0002")
        self.assertEqual(201, replacement.status_code)
        self.assertEqual(1, len([row for row in self.repository.table_rows("devices") if row["status"] == "active"]))

    def test_activation_expiry_and_attempt_cap_are_generic(self) -> None:
        access = self._registered_access()
        code = self.activation_code(access).get_json()["activation_code"]
        with self.repository._transaction() as connection:
            connection.execute("UPDATE activation_codes SET expires_at='2020-01-01T00:00:00Z'")
        responses = [
            self.activate(code, instance_id=str(uuid.uuid4()), key=f"device-activate-key-{index:04d}")
            for index in range(1, 6)
        ]
        self.assertTrue(all(item.status_code == 400 and item.get_json()["error"]["code"] == "ACTIVATION_FAILED" for item in responses))
        state = self.repository.table_rows("activation_codes")[0]
        self.assertEqual(5, state["failed_attempt_count"])
        self.assertIsNotNone(state["revoked_at"])

    def test_revoke_immediately_denies_heartbeat_and_list_exposes_only_allowlist(self) -> None:
        access = self._registered_access()
        code = self.activation_code(access).get_json()["activation_code"]
        activated = self.activate(code).get_json()
        listed = self.client.get("/v1/devices", headers=self.access_headers(access))
        self.assertEqual(200, listed.status_code)
        self.assertEqual(
            {"id", "status", "app_version", "os_family", "activated_at", "last_heartbeat_at", "revoked_at"},
            set(listed.get_json()["devices"][0]),
        )
        self.client.post(
            f"/v1/devices/{activated['device_id']}/revoke",
            headers={**self.access_headers(access), "Idempotency-Key": "device-revoke-key-0001"},
            json={"reason": "security"},
        )
        denied = self.client.post(
            f"/v1/devices/{activated['device_id']}/heartbeat",
            headers={"Authorization": f"Bearer {activated['device_credential']}"},
            json={"app_version": "1.0.1", "os_family": "windows"},
        )
        self.assertEqual(401, denied.status_code)

    def test_heartbeat_updates_latest_metadata_only_and_has_no_success_audit(self) -> None:
        access = self._registered_access()
        code = self.activation_code(access).get_json()["activation_code"]
        activated = self.activate(code).get_json()
        audit_before = len(self.repository.table_rows("audit_events"))
        response = self.client.post(
            f"/v1/devices/{activated['device_id']}/heartbeat",
            headers={"Authorization": f"Bearer {activated['device_credential']}"},
            json={"app_version": "1.2.3", "os_family": "windows"},
        )
        self.assertEqual(200, response.status_code)
        device = self.repository.table_rows("devices")[0]
        self.assertEqual(("1.2.3", "windows"), (device["app_version"], device["os_family"]))
        self.assertIsNotNone(device["last_heartbeat_at"])
        self.assertEqual(audit_before, len(self.repository.table_rows("audit_events")))

    def test_heartbeat_rate_and_optional_idempotency_header_are_strict(self) -> None:
        access = self._registered_access()
        code = self.activation_code(access).get_json()["activation_code"]
        activated = self.activate(code).get_json()
        path = f"/v1/devices/{activated['device_id']}/heartbeat"
        headers = {
            "Authorization": f"Bearer {activated['device_credential']}",
            "Idempotency-Key": "heartbeat-idempotency-0001",
        }
        statuses = [
            self.client.post(path, headers=headers, json={"app_version": "1.0.0", "os_family": "windows"}).status_code
            for _ in range(13)
        ]
        self.assertEqual([200] * 12 + [429], statuses)
        invalid_header = self.client.post(
            path,
            headers={"Authorization": f"Bearer {activated['device_credential']}", "Idempotency-Key": "short"},
            json={"app_version": "1.0.0", "os_family": "windows"},
        )
        # Header schema validation occurs before credential/rate processing.
        self.assertEqual(400, invalid_header.status_code)

    def test_cross_tenant_device_is_indistinguishable_from_nonexistent(self) -> None:
        access_a = self._registered_access()
        code_a = self.activation_code(access_a, "activation-code-key-a001").get_json()["activation_code"]
        device_a = self.activate(code_a, key="device-activate-key-a001").get_json()

        code_b_invite = self.bootstrap("second@example.test")
        second = self.app.test_client()
        registered_b = second.post(
            "/v1/auth/register", headers={"Origin": ORIGIN},
            json={
                "invite_code": code_b_invite, "email": "second@example.test", "password": PASSWORD,
                "accepted_terms_version": None, "accepted_privacy_version": None,
            },
        )
        access_b = registered_b.get_json()["access_token"]
        activation_b = second.post(
            "/v1/device-activation-codes",
            headers={**self.access_headers(access_b), "Idempotency-Key": "activation-code-key-b001"},
        ).get_json()["activation_code"]
        device_b = second.post(
            "/v1/devices/activate", headers={"Origin": ORIGIN, "Idempotency-Key": "device-activate-key-b001"},
            json={"activation_code": activation_b, "device_instance_id": str(uuid.uuid4()), "app_version": "1.0.0", "os_family": "windows"},
        ).get_json()

        foreign = self.client.post(
            f"/v1/devices/{device_b['device_id']}/revoke",
            headers={**self.access_headers(access_a), "Idempotency-Key": "device-revoke-key-a001"},
            json={"reason": "security"},
        )
        missing = self.client.post(
            f"/v1/devices/{uuid.uuid4()}/revoke",
            headers={**self.access_headers(access_a), "Idempotency-Key": "device-revoke-key-a002"},
            json={"reason": "security"},
        )
        self.assertEqual((404, foreign.get_json()["error"]["code"]), (missing.status_code, missing.get_json()["error"]["code"]))
        self.assertEqual("active", self.repository.get_device_principal(device_b["device_id"])["status"])
        self.assertEqual("active", self.repository.get_device_principal(device_a["device_id"])["status"])


class StrictApiTests(ControlPlaneTestCase):
    def test_recursive_forbidden_key_variants_are_rejected_before_schema(self) -> None:
        marker = "synthetic-live-value-never-store"
        for key in ("Comment", " viewer-name ", "YouTube-Channel"):
            response = self.client.post(
                "/v1/auth/login", headers={"Origin": ORIGIN, "Content-Type": "application/json"},
                data=json.dumps({"email": "owner@example.test", "password": PASSWORD, "nested": [{key: marker}]}),
            )
            self.assertEqual((400, "PAYLOAD_FORBIDDEN"), (response.status_code, response.get_json()["error"]["code"]))
        database_bytes = b"".join(path.read_bytes() for path in self.data_dir.glob("control.sqlite3*"))
        self.assertNotIn(marker.encode(), database_bytes)

    def test_duplicate_unknown_wrong_type_nonfinite_and_body_limit(self) -> None:
        duplicate = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            data='{"email":"a@example.test","email":"b@example.test","password":"long-enough-password"}',
        )
        unknown = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN},
            json={"email": "a@example.test", "password": "long-enough-password", "extra": 1},
        )
        wrong = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN}, json={"email": 1, "password": "long-enough-password"},
        )
        nonfinite = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            data='{"email":"a@example.test","password":NaN}',
        )
        huge = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            data=b"{" + b"x" * 17000,
        )
        self.assertEqual([400, 400, 400, 400, 413], [item.status_code for item in (duplicate, unknown, wrong, nonfinite, huge)])
        self.assertEqual("PAYLOAD_TOO_LARGE", huge.get_json()["error"]["code"])

    def test_query_credentials_and_client_identity_headers_are_rejected_before_auth(self) -> None:
        paths = (
            ("get", "/v1/health/live"),
            ("get", "/v1/me"),
            ("post", "/v1/auth/login"),
            ("post", "/v1/devices/activate"),
        )
        for method, path in paths:
            response = getattr(self.client, method)(path + "?access_token=synthetic-secret")
            self.assertEqual((400, "QUERY_CREDENTIAL_FORBIDDEN"), (response.status_code, response.get_json()["error"]["code"]))
        identity = self.client.get("/v1/me", headers={"X-Tenant-ID": str(uuid.uuid4())})
        self.assertEqual((400, "INVALID_REQUEST"), (identity.status_code, identity.get_json()["error"]["code"]))

    def test_media_type_no_body_and_request_id_contract(self) -> None:
        unsupported = self.client.post("/v1/auth/login", data="{}", content_type="text/plain")
        self.assertEqual((415, "UNSUPPORTED_MEDIA_TYPE"), (unsupported.status_code, unsupported.get_json()["error"]["code"]))
        body_on_get = self.client.get("/v1/me", data="x")
        self.assertEqual(400, body_on_get.status_code)
        self.assertEqual(body_on_get.headers["X-Request-ID"], body_on_get.get_json()["error"]["request_id"])
        self.assertEqual("no-store", body_on_get.headers["Cache-Control"])
        self.assertEqual("nosniff", body_on_get.headers["X-Content-Type-Options"])

    def test_device_origin_is_rejected_and_preflight_is_exact(self) -> None:
        device = self.client.post(
            f"/v1/devices/{uuid.uuid4()}/heartbeat",
            headers={"Origin": ORIGIN, "Authorization": "Bearer invalid"},
            json={"app_version": "1.0.0", "os_family": "windows"},
        )
        self.assertEqual((403, "ORIGIN_FORBIDDEN"), (device.status_code, device.get_json()["error"]["code"]))
        allowed = self.client.options(
            "/v1/auth/login",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        denied = self.client.options(
            "/v1/auth/login",
            headers={"Origin": "https://attacker.example", "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(204, allowed.status_code)
        self.assertEqual(ORIGIN, allowed.headers["Access-Control-Allow-Origin"])
        self.assertEqual(403, denied.status_code)
        self.assertNotIn("Access-Control-Allow-Origin", denied.headers)

    def test_persistent_rate_limit_and_database_failure_are_fail_closed(self) -> None:
        self.register()
        attempts = [
            self.client.post(
                "/v1/auth/login", headers={"Origin": ORIGIN},
                json={"email": "owner@example.test", "password": "Wrong password long value"},
            )
            for _ in range(6)
        ]
        self.assertEqual([401, 401, 401, 401, 401, 429], [item.status_code for item in attempts])
        self.build_app()
        persisted = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN},
            json={"email": "owner@example.test", "password": "Wrong password long value"},
        )
        self.assertEqual(429, persisted.status_code)
        with patch.object(self.repository, "consume_rate_limit", side_effect=RepositoryUnavailable("down")):
            failed = self.client.post(
                "/v1/auth/login", headers={"Origin": ORIGIN},
                json={"email": "another@example.test", "password": "Wrong password long value"},
            )
        self.assertEqual((503, "NOT_READY"), (failed.status_code, failed.get_json()["error"]["code"]))

    def test_full_flow_stores_no_plaintext_credentials_and_audit_is_allowlisted(self) -> None:
        password_marker = "Synthetic-password-marker-937!"
        invite = self.bootstrap("scan@example.test")
        registered = self.client.post(
            "/v1/auth/register", headers={"Origin": ORIGIN},
            json={
                "invite_code": invite, "email": "scan@example.test", "password": password_marker,
                "accepted_terms_version": None, "accepted_privacy_version": None,
            },
        )
        access = registered.get_json()["access_token"]
        refresh = refresh_cookie(registered)
        activation = self.activation_code(access).get_json()["activation_code"]
        device = self.activate(activation).get_json()["device_credential"]
        database_bytes = b"".join(path.read_bytes() for path in self.data_dir.glob("control.sqlite3*"))
        for marker in (password_marker, invite, refresh, activation, device, access):
            self.assertNotIn(marker.encode(), database_bytes)
        for event in self.repository.table_rows("audit_events"):
            self.assertIn(event["event_type"], AUDIT_EVENT_TYPES)
            metadata = json.loads(event["metadata_json"])
            self.assertLessEqual(set(metadata), AUDIT_METADATA_KEYS)
            self.assertNotIn("scan@example.test", event["metadata_json"])

    def test_restore_verifier_is_nonmutating_and_rejects_invariant_break(self) -> None:
        self.register()
        before = self.db_path.stat().st_mtime_ns
        self.assertTrue(verify_restored_database(self.db_path))
        self.assertEqual(before, self.db_path.stat().st_mtime_ns)
        with self.repository._transaction() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            # The partial unique index prevents a cap violation, so corrupt an
            # audit allowlist value to prove the verifier fails closed.
            connection.execute("UPDATE audit_events SET event_type='unknown.event' WHERE id=(SELECT id FROM audit_events LIMIT 1)")
        with self.assertRaises(RepositoryUnavailable):
            verify_restored_database(self.db_path)

    def test_no_http_admin_or_connector_surface_exists(self) -> None:
        for path in ("/v1/admin", "/admin", "/", "/overlay.html", "/connect", "/socket.io/"):
            self.assertEqual(404, self.client.get(path).status_code, path)


if __name__ == "__main__":
    import unittest
    unittest.main()
