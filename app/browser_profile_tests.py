from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import browser_profiles
import server


class BrowserProfileCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(server, "DB_PATH", Path(self.temp.name) / "test.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        server.init_db()
        server.db_set_setting("active_chrome_profile", "Profile 1")

    def test_legacy_bridge_reuses_active_profile_without_client_binding(self):
        with patch.object(
            browser_profiles,
            "ensure_server",
            return_value={"extension_connected": True},
        ), patch.object(browser_profiles, "select") as select:
            client = server.select_profile_connection("Profile 1")
        self.assertEqual(client, "")
        select.assert_not_called()

    def test_protocol_two_reuses_sole_client_for_active_profile(self):
        with patch.object(
            browser_profiles,
            "ensure_server",
            return_value={"extension_connected": True, "protocol": 2, "clients": ["client-1"]},
        ), patch.object(browser_profiles, "select") as select:
            client = server.select_profile_connection("Profile 1")
        self.assertEqual(client, "client-1")
        select.assert_called_once_with("client-1")

    def test_unbound_inactive_profile_never_reuses_current_connection(self):
        with patch.object(browser_profiles, "reuse_single_connection") as reuse:
            with self.assertRaisesRegex(server.PublishError, "尚未绑定"):
                server.select_profile_connection("Default")
        reuse.assert_not_called()

    def test_multiple_clients_never_guess_active_profile_connection(self):
        status = {
            "extension_connected": True,
            "protocol": 2,
            "clients": ["client-1", "client-2"],
        }
        with patch.object(browser_profiles, "ensure_server", return_value=status):
            with self.assertRaisesRegex(server.PublishError, "尚未绑定"):
                server.select_profile_connection("Profile 1")

    def test_switch_active_profile_verifies_legacy_connection_without_pairing(self):
        identity = {
            "logged_in": True,
            "status": "verified",
            "nickname": "摄影师唐唐",
            "user_id": "xhs-user-1",
            "avatar": "",
        }
        with patch.object(server, "chrome_profiles", return_value=[{"dir": "Profile 1"}]), patch.object(
            browser_profiles, "ensure_server", return_value={"extension_connected": True}
        ), patch.object(server, "read_connected_identity", return_value=identity), patch.object(
            browser_profiles, "pair"
        ) as pair:
            result = server.switch_chrome_profile("Profile 1")
        self.assertTrue(result["ready"])
        pair.assert_not_called()
        self.assertEqual(server.account_identity("Profile 1")["user_id"], "xhs-user-1")


if __name__ == "__main__":
    unittest.main()
