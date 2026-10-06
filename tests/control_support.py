from __future__ import annotations

from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import unittest
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from lca import create_app


ORIGIN = "http://localhost:3000"
PASSWORD = "Correct horse battery staple!"


def refresh_cookie(response) -> str:
    cookie = SimpleCookie()
    cookie.load(response.headers["Set-Cookie"])
    return cookie["lca_refresh"].value


class FrozenClock:
    def __init__(self, value: datetime | None = None):
        self.value = value or datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class ControlPlaneTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.mkdtemp(prefix="lca-w2-")
        root = Path(self._temp)
        self.data_dir = root / "data"
        self.secret_dir = root / "secrets"
        self.data_dir.mkdir(mode=0o700)
        self.secret_dir.mkdir(mode=0o700)
        self.db_path = self.data_dir / "control.sqlite3"
        self.signing_path = self.secret_dir / "access-ed25519.pem"
        self.pepper_path = self.secret_dir / "hash-root.bin"
        private_key = Ed25519PrivateKey.generate()
        self.signing_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.pepper_path.write_bytes(secrets.token_bytes(32))
        self.signing_path.chmod(0o600)
        self.pepper_path.chmod(0o600)
        self.clock = FrozenClock()
        self.overrides = {
            "TESTING": True,
            "RUNTIME_ROLE": "control-plane",
            "BIND_HOST": "127.0.0.1",
            "LCA_DATA_DIR": str(self.data_dir),
            "LCA_DB_PATH": str(self.db_path),
            "LCA_CP_SECRET_DIR": str(self.secret_dir),
            "LCA_CP_SIGNING_KEY_FILE": str(self.signing_path),
            "LCA_CP_HASH_PEPPER_FILE": str(self.pepper_path),
            "LCA_CP_BROWSER_ORIGIN": ORIGIN,
            "LCA_CP_TOKEN_ISSUER": "lca-home-server-test",
            "LCA_CP_TOKEN_AUDIENCE": "lca-browser-test",
            "LCA_CP_SIGNING_KEY_ID": "test-ed25519-1",
            "LCA_CP_HASH_KEY_ID": "test-hash-1",
            "LCA_CP_COOKIE_SECURE": False,
            "LCA_CP_ALLOW_INSECURE_TEST_MODE": True,
            "CLOCK": self.clock,
        }
        self.build_app()

    def tearDown(self) -> None:
        shutil.rmtree(self._temp, ignore_errors=True)

    def build_app(self) -> None:
        self.app = create_app(self.overrides)
        self.client = self.app.test_client()
        self.runtime = self.app.extensions["lca.control_runtime"]
        self.repository = self.runtime.repository

    def bootstrap(self, email: str = "owner@example.test") -> str:
        _, code = self.runtime.bootstrap_invite(
            email,
            actor_ref=self.runtime.hasher.digest("audit-ref", "test-operator"),
            request_id=str(uuid.uuid4()),
            now=self.clock(),
        )
        return code

    def register(self, email: str = "owner@example.test", password: str = PASSWORD):
        code = self.bootstrap(email)
        return self.client.post(
            "/v1/auth/register",
            headers={"Origin": ORIGIN},
            json={
                "invite_code": code,
                "email": email,
                "password": password,
                "accepted_terms_version": None,
                "accepted_privacy_version": None,
            },
        )

    @staticmethod
    def access_headers(access_token: str, *, origin: str | None = ORIGIN) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {access_token}"}
        if origin is not None:
            headers["Origin"] = origin
        return headers

    def activation_code(self, access_token: str, key: str = "activation-code-key-0001"):
        return self.client.post(
            "/v1/device-activation-codes",
            headers={**self.access_headers(access_token), "Idempotency-Key": key},
        )

    def activate(
        self,
        code: str,
        *,
        instance_id: str | None = None,
        key: str = "device-activate-key-0001",
    ):
        return self.client.post(
            "/v1/devices/activate",
            headers={"Origin": ORIGIN, "Idempotency-Key": key},
            json={
                "activation_code": code,
                "device_instance_id": instance_id or str(uuid.uuid4()),
                "app_version": "1.0.0",
                "os_family": "windows",
            },
        )
