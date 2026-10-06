from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import secrets
import sys
import uuid

import jwt


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from lca.control_security import CURRENT_PASSWORD_METHOD, hash_password
from tests.control_support import ControlPlaneTestCase, ORIGIN, PASSWORD, refresh_cookie


class AuthLifecycleTests(ControlPlaneTestCase):
    def test_registration_is_invite_only_and_password_uses_current_scrypt(self) -> None:
        invalid = self.client.post(
            "/v1/auth/register",
            headers={"Origin": ORIGIN},
            json={
                "invite_code": "x" * 32, "email": "owner@example.test", "password": PASSWORD,
                "accepted_terms_version": None, "accepted_privacy_version": None,
            },
        )
        self.assertEqual((400, "INVITE_INVALID"), (invalid.status_code, invalid.get_json()["error"]["code"]))
        response = self.register()
        self.assertEqual(201, response.status_code)
        user = self.repository.table_rows("users")[0]
        self.assertTrue(user["password_hash"].startswith(CURRENT_PASSWORD_METHOD + "$"))
        self.assertNotEqual(PASSWORD, user["password_hash"])

    def test_invite_mismatch_expiry_revoke_used_and_attempt_cap_share_generic_shape(self) -> None:
        def registration(code: str, email: str):
            response = self.client.post(
                "/v1/auth/register", headers={"Origin": ORIGIN},
                json={
                    "invite_code": code, "email": email, "password": PASSWORD,
                    "accepted_terms_version": None, "accepted_privacy_version": None,
                },
            )
            body = response.get_json()
            shape = None if response.status_code < 400 else (response.status_code, body["error"]["code"], body["error"]["details"])
            return response, shape

        mismatch_code = self.bootstrap("bound@example.test")
        mismatch, mismatch_shape = registration(mismatch_code, "other@example.test")
        self.assertEqual((400, "INVITE_INVALID", {}), mismatch_shape)

        expired_code = self.bootstrap("expired@example.test")
        with self.repository._transaction() as connection:
            connection.execute("UPDATE invites SET expires_at='2020-01-01T00:00:00Z' WHERE email_normalized='expired@example.test'")
        _, expired_shape = registration(expired_code, "expired@example.test")

        revoked_code = self.bootstrap("revoked@example.test")
        revoked_id = [row for row in self.repository.table_rows("invites") if row["email_normalized"] == "revoked@example.test"][0]["id"]
        self.repository.revoke_invite(
            invite_id=revoked_id, now=self.clock(), request_id=str(uuid.uuid4()), actor_ref="operator-ref",
        )
        _, revoked_shape = registration(revoked_code, "revoked@example.test")

        used_code = self.bootstrap("used@example.test")
        first, _ = registration(used_code, "used@example.test")
        self.assertEqual(201, first.status_code)
        _, used_shape = registration(used_code, "used@example.test")
        self.assertEqual([mismatch_shape] * 3, [expired_shape, revoked_shape, used_shape])

        cap_code = self.bootstrap("cap@example.test")
        capped = [registration(cap_code, "wrong@example.test")[0] for _ in range(5)]
        self.assertTrue(all(item.status_code == 400 for item in capped))
        cap_invite = [row for row in self.repository.table_rows("invites") if row["email_normalized"] == "cap@example.test"][0]
        self.assertEqual(5, cap_invite["failed_attempt_count"])
        self.assertIsNotNone(cap_invite["revoked_at"])

    def test_successful_old_scrypt_login_rehashes_atomically(self) -> None:
        response = self.register()
        user = self.repository.table_rows("users")[0]
        older = hash_password(PASSWORD, method="scrypt:16384:8:1")
        with self.repository._transaction() as connection:
            connection.execute("UPDATE users SET password_hash=? WHERE id=?", (older, user["id"]))
        login = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN},
            json={"email": "owner@example.test", "password": PASSWORD},
        )
        self.assertEqual(200, login.status_code)
        updated = self.repository.table_rows("users")[0]["password_hash"]
        self.assertTrue(updated.startswith(CURRENT_PASSWORD_METHOD + "$"))
        self.assertNotEqual(older, updated)

    def test_login_failure_is_generic_for_absent_wrong_and_suspended(self) -> None:
        self.register()
        wrong = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN},
            json={"email": "owner@example.test", "password": "Incorrect password value"},
        )
        absent = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN},
            json={"email": "absent@example.test", "password": "Incorrect password value"},
        )
        tenant = self.repository.table_rows("tenants")[0]["id"]
        self.repository.suspend_tenant(
            tenant_id=tenant, now=self.clock(), request_id=str(uuid.uuid4()), actor_ref="operator-ref",
        )
        suspended = self.client.post(
            "/v1/auth/login", headers={"Origin": ORIGIN},
            json={"email": "owner@example.test", "password": PASSWORD},
        )
        public_shapes = [(item.status_code, item.get_json()["error"]["code"], item.get_json()["error"]["details"]) for item in (wrong, absent, suspended)]
        self.assertEqual([(401, "AUTH_FAILED", {})] * 3, public_shapes)

    def test_access_token_claims_are_pinned_and_session_revocation_is_immediate(self) -> None:
        response = self.register()
        access = response.get_json()["access_token"]
        header = jwt.get_unverified_header(access)
        claims = jwt.decode(access, options={"verify_signature": False})
        self.assertEqual({"alg": "EdDSA", "kid": "test-ed25519-1", "typ": "JWT"}, header)
        self.assertEqual("test-ed25519-1", claims["kid"])
        self.assertEqual(200, self.client.get("/v1/me", headers=self.access_headers(access)).status_code)
        self.repository.operator_revoke_session(
            session_id=claims["sid"], now=self.clock(), request_id=str(uuid.uuid4()), actor_ref="operator-ref",
        )
        denied = self.client.get("/v1/me", headers=self.access_headers(access))
        self.assertEqual((401, "INVALID_CREDENTIAL"), (denied.status_code, denied.get_json()["error"]["code"]))

    def test_wrong_alg_signature_kid_issuer_audience_and_sid_are_rejected(self) -> None:
        response = self.register()
        access = response.get_json()["access_token"]
        claims = jwt.decode(access, options={"verify_signature": False})
        private = self.runtime.tokens._private_key

        variants: list[str] = []
        for changed, value in (
            ("iss", "wrong-issuer"),
            ("aud", "wrong-audience"),
            ("sid", str(uuid.uuid4())),
            ("tenant_id", str(uuid.uuid4())),
            ("role", "admin"),
        ):
            modified = dict(claims)
            modified[changed] = value
            variants.append(jwt.encode(modified, private, algorithm="EdDSA", headers={"kid": "test-ed25519-1"}))
        variants.append(jwt.encode(claims, private, algorithm="EdDSA", headers={"kid": "unknown"}))
        variants.append(jwt.encode(claims, secrets.token_bytes(32), algorithm="HS256", headers={"kid": "test-ed25519-1"}))
        variants.append(jwt.encode(claims, key="", algorithm="none", headers={"kid": "test-ed25519-1"}))
        for token in variants:
            denied = self.client.get("/v1/me", headers=self.access_headers(token))
            self.assertEqual(401, denied.status_code)
            self.assertEqual("INVALID_CREDENTIAL", denied.get_json()["error"]["code"])

    def test_access_expiry_uses_injected_clock(self) -> None:
        response = self.register()
        access = response.get_json()["access_token"]
        self.clock.value += timedelta(minutes=21)
        denied = self.client.get("/v1/me", headers=self.access_headers(access))
        self.assertEqual(401, denied.status_code)

    def test_refresh_rotates_and_proven_old_replay_revokes_family(self) -> None:
        registered = self.register()
        old_cookie = refresh_cookie(registered)
        old_csrf = registered.get_json()["csrf_token"]
        rotated = self.client.post(
            "/v1/auth/refresh", headers={"Origin": ORIGIN, "X-CSRF-Token": old_csrf},
        )
        self.assertEqual(200, rotated.status_code)
        current_cookie = refresh_cookie(rotated)
        current_csrf = rotated.get_json()["csrf_token"]
        session_id = old_cookie.split(".", 1)[0]
        self.assertEqual(1, len(self.repository.spent_refresh_credentials(session_id)))

        self.client.set_cookie("lca_refresh", old_cookie, path="/v1/auth")
        replay = self.client.post(
            "/v1/auth/refresh", headers={"Origin": ORIGIN, "X-CSRF-Token": old_csrf},
        )
        self.assertEqual((401, "INVALID_CREDENTIAL"), (replay.status_code, replay.get_json()["error"]["code"]))
        self.assertEqual("refresh_replay", self.repository.get_session(session_id)["revoke_reason"])

        self.client.set_cookie("lca_refresh", current_cookie, path="/v1/auth")
        denied = self.client.post(
            "/v1/auth/refresh", headers={"Origin": ORIGIN, "X-CSRF-Token": current_csrf},
        )
        self.assertEqual(401, denied.status_code)

    def test_random_secret_with_known_sid_does_not_revoke_current_session(self) -> None:
        registered = self.register()
        first_cookie = refresh_cookie(registered)
        first_csrf = registered.get_json()["csrf_token"]
        rotated = self.client.post(
            "/v1/auth/refresh", headers={"Origin": ORIGIN, "X-CSRF-Token": first_csrf},
        )
        current_cookie = refresh_cookie(rotated)
        current_csrf = rotated.get_json()["csrf_token"]
        session_id = current_cookie.split(".", 1)[0]
        random_cookie = f"{session_id}.{secrets.token_urlsafe(32)}"
        self.client.set_cookie("lca_refresh", random_cookie, path="/v1/auth")
        denied = self.client.post(
            "/v1/auth/refresh", headers={"Origin": ORIGIN, "X-CSRF-Token": secrets.token_urlsafe(32)},
        )
        self.assertEqual(401, denied.status_code)
        self.assertIsNone(self.repository.get_session(session_id)["revoked_at"])
        self.client.set_cookie("lca_refresh", current_cookie, path="/v1/auth")
        valid = self.client.post(
            "/v1/auth/refresh", headers={"Origin": ORIGIN, "X-CSRF-Token": current_csrf},
        )
        self.assertEqual(200, valid.status_code)

    def test_refresh_csrf_and_origin_fail_without_rotation(self) -> None:
        registered = self.register()
        session_id = refresh_cookie(registered).split(".", 1)[0]
        generation = self.repository.get_session(session_id)["refresh_generation"]
        for headers, expected_code, expect_cors in (
            ({"Origin": ORIGIN}, "CSRF_FAILED", True),
            ({"Origin": ORIGIN, "X-CSRF-Token": "wrong"}, "CSRF_FAILED", True),
            ({"X-CSRF-Token": registered.get_json()["csrf_token"]}, "ORIGIN_FORBIDDEN", False),
            ({"Origin": "https://attacker.example", "X-CSRF-Token": registered.get_json()["csrf_token"]}, "ORIGIN_FORBIDDEN", False),
        ):
            response = self.client.post("/v1/auth/refresh", headers=headers)
            self.assertEqual(expected_code, response.get_json()["error"]["code"])
            self.assertEqual(expect_cors, "Access-Control-Allow-Origin" in response.headers)
        self.assertEqual(generation, self.repository.get_session(session_id)["refresh_generation"])

    def test_logout_is_idempotent_when_same_refresh_is_safely_attributable(self) -> None:
        registered = self.register()
        cookie = refresh_cookie(registered)
        csrf = registered.get_json()["csrf_token"]
        first = self.client.post("/v1/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        self.assertEqual(204, first.status_code)
        self.client.set_cookie("lca_refresh", cookie, path="/v1/auth")
        second = self.client.post("/v1/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        self.assertEqual(204, second.status_code)

    def test_cookie_and_cors_attributes_are_exact(self) -> None:
        response = self.register()
        set_cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Path=/v1/auth", set_cookie)
        self.assertNotIn("Secure", set_cookie)  # explicit loopback-only test profile
        self.assertEqual(ORIGIN, response.headers["Access-Control-Allow-Origin"])
        self.assertEqual("true", response.headers["Access-Control-Allow-Credentials"])
        denied = self.client.post(
            "/v1/auth/login", headers={"Origin": "https://attacker.example"},
            json={"email": "owner@example.test", "password": PASSWORD},
        )
        self.assertEqual(403, denied.status_code)
        self.assertNotIn("Access-Control-Allow-Origin", denied.headers)


if __name__ == "__main__":
    import unittest
    unittest.main()
