from __future__ import annotations

from pathlib import Path
import unittest


STATIC = Path(__file__).resolve().parents[1] / "server" / "static"
SERVER_PACKAGE = Path(__file__).resolve().parents[1] / "server" / "lca"


class StaticSecurityTests(unittest.TestCase):
    def test_served_ui_does_not_use_inner_html(self) -> None:
        for path in STATIC.glob("*"):
            if path.suffix in {".html", ".js"}:
                self.assertNotIn("innerHTML", path.read_text(encoding="utf-8"), path.name)

    def test_served_ui_uses_text_content_for_untrusted_values(self) -> None:
        app_js = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("textContent", app_js)
        self.assertIn("safeAvatar", app_js)

    def test_external_scripts_are_version_pinned_and_integrity_checked(self) -> None:
        for path in STATIC.glob("*.html"):
            source = path.read_text(encoding="utf-8")
            if "<script src=\"https://" in source:
                self.assertIn("integrity=\"sha384-", source, path.name)
                self.assertNotIn("src=\"https://cdn.tailwindcss.com\"", source, path.name)

    def test_runtime_has_no_firebase_service_account_path(self) -> None:
        source = "\n".join(path.read_text(encoding="utf-8") for path in SERVER_PACKAGE.rglob("*.py"))
        self.assertNotIn("firebase-key.json", source)
        self.assertNotIn("credentials.Certificate", source)


if __name__ == "__main__":
    unittest.main()
