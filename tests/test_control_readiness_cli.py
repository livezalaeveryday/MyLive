from __future__ import annotations

from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import os
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import admin_cli
from lca import create_app
from tests.control_support import ControlPlaneTestCase


class ReadinessToggleTests(ControlPlaneTestCase):
    def test_control_plane_does_not_generate_connector_fallback_credential(self) -> None:
        self.assertEqual("", self.app.extensions["lca.settings"].local_auth_token)

    def _reasons(self, **changes):
        app = create_app({**self.overrides, **changes})
        response = app.test_client().get("/v1/health/ready")
        self.assertEqual(503, response.status_code)
        reasons = response.get_json()["error"]["details"]["reasons"]
        self.assertTrue(set(reasons) <= {
            "BIND_UNSAFE", "DATA_PATH_INVALID", "DATABASE_UNAVAILABLE", "MIGRATION_INVALID",
            "SECURITY_CONFIG_INVALID", "ORIGIN_INVALID", "RETENTION_OVERDUE", "MAINTENANCE",
        })
        return reasons

    def test_bind_maintenance_origin_data_and_security_toggle_independently(self) -> None:
        self.assertIn("BIND_UNSAFE", self._reasons(BIND_HOST="0.0.0.0"))
        self.assertIn("MAINTENANCE", self._reasons(MAINTENANCE_MODE=True))
        self.assertIn("ORIGIN_INVALID", self._reasons(LCA_CP_BROWSER_ORIGIN="*"))
        self.assertIn("DATA_PATH_INVALID", self._reasons(LCA_DB_PATH="relative.sqlite3"))
        self.assertIn("SECURITY_CONFIG_INVALID", self._reasons(LCA_CP_SIGNING_KEY_FILE=str(self.secret_dir / "missing.pem")))

    def test_retention_overdue_and_unknown_active_key_id_fail_closed(self) -> None:
        with self.repository._transaction() as connection:
            connection.execute("DELETE FROM audit_events WHERE event_type='retention.completed'")
        response = self.client.get("/v1/health/ready")
        self.assertIn("RETENTION_OVERDUE", response.get_json()["error"]["details"]["reasons"])
        self.repository.run_retention(self.clock())
        registered = self.register()
        session_id = self.repository.table_rows("sessions")[0]["id"]
        with self.repository._transaction() as connection:
            connection.execute("UPDATE sessions SET refresh_hash_key_id='retired-key' WHERE id=?", (session_id,))
        response = self.client.get("/v1/health/ready")
        self.assertIn("SECURITY_CONFIG_INVALID", response.get_json()["error"]["details"]["reasons"])

    def test_readiness_never_exposes_paths_or_key_material(self) -> None:
        app = create_app({**self.overrides, "LCA_CP_SIGNING_KEY_FILE": str(self.secret_dir / "missing.pem")})
        response = app.test_client().get("/v1/health/ready")
        serialized = response.get_data(as_text=True)
        self.assertNotIn(str(self.secret_dir), serialized)
        self.assertNotIn("test-ed25519-1", serialized)
        self.assertNotIn("missing.pem", serialized)


class AdminCliBoundaryTests(ControlPlaneTestCase):
    def test_cli_refuses_noninteractive_session(self) -> None:
        with patch.object(admin_cli, "_interactive", return_value=False):
            self.assertEqual(2, admin_cli.main(["readiness-check"]))

    def test_bootstrap_secret_is_handed_to_single_display_function(self) -> None:
        with (
            patch.object(admin_cli, "_interactive", return_value=True),
            patch.object(admin_cli, "create_app", return_value=self.app),
            patch.object(admin_cli, "_operator_identity", return_value="operator-ref"),
            patch.object(admin_cli, "_prompt_hidden", return_value="cli-owner@example.test"),
            patch.object(admin_cli, "_print_invite") as display,
        ):
            self.assertEqual(0, admin_cli.main(["bootstrap-owner-invite"]))
        display.assert_called_once()
        displayed_code = display.call_args.args[0]
        self.assertNotIn(displayed_code, self.db_path.read_bytes().decode("latin1"))

    def test_cli_rejects_secret_argv_without_echoing_value(self) -> None:
        secret_marker = "secret-email-marker@example.test"
        stderr = StringIO()
        with patch.object(sys, "stderr", stderr):
            with self.assertRaises(SystemExit):
                admin_cli.main(["bootstrap-owner-invite", "--email", secret_marker])
        self.assertNotIn(secret_marker, stderr.getvalue())


if __name__ == "__main__":
    import unittest
    unittest.main()
