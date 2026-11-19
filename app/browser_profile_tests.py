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

    def test_bound_profile_never_borrows_another_connection(self):
        server.db_set_setting("profile_bridge_client:Profile 1", "client-1")
        with patch.object(browser_profiles, "select", side_effect=RuntimeError("未连接")), patch.object(
            browser_profiles, "reuse_single_connection"
        ) as reuse:
            with self.assertRaisesRegex(server.PublishError, "没有连接"):
                server.select_profile_connection("Profile 1")
        reuse.assert_not_called()

    def test_unbound_connection_cannot_replace_known_identity(self):
        server.db_set_setting("account_identity:Profile 1", {
            "logged_in": True, "status": "verified", "nickname": "摄影师唐唐—",
            "user_id": "xhs-user-2", "avatar": "",
        })
        other = {
            "logged_in": True, "status": "verified", "nickname": "摄影师唐唐",
            "user_id": "xhs-user-1", "avatar": "",
        }
        with patch.object(
            browser_profiles,
            "ensure_server",
            return_value={"extension_connected": True, "protocol": 2, "clients": ["client-1"]},
        ), patch.object(browser_profiles, "select"), patch.object(
            server, "read_connected_identity", return_value=other
        ):
            result = server.refresh_account_identity("Profile 1")
        self.assertEqual(result["identity"]["status"], "unavailable")
        self.assertIn("上次识别", result["identity"]["error"])
        self.assertEqual(server.account_identity("Profile 1")["user_id"], "xhs-user-2")

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

    def test_bound_account_mismatch_waits_for_confirmation_then_adopts(self):
        server.db_set_setting("profile_bridge_client:Profile 1", "client-1")
        server.db_set_setting("account_identity:Profile 1", {
            "logged_in": True, "status": "verified", "nickname": "旧账号",
            "user_id": "xhs-user-old", "avatar": "",
        })
        observed = {
            "logged_in": True, "status": "verified", "nickname": "新账号",
            "user_id": "xhs-user-new", "avatar": "",
        }
        with patch.object(browser_profiles, "ensure_server"), patch.object(
            browser_profiles, "select"
        ), patch.object(server, "read_connected_identity", return_value=observed):
            with self.assertRaises(server.AccountMismatch) as caught:
                server.refresh_account_identity("Profile 1")
            self.assertEqual(caught.exception.payload()["conflict"]["observed"]["user_id"], "xhs-user-new")
            self.assertEqual(server.account_identity("Profile 1")["user_id"], "xhs-user-old")
            result = server.refresh_account_identity("Profile 1", adopt=True)
        self.assertEqual(result["identity"]["user_id"], "xhs-user-new")
        self.assertEqual(server.account_identity("Profile 1")["user_id"], "xhs-user-new")

    def test_mismatch_snooze_adopts_without_prompt_for_24h(self):
        server.db_set_setting("profile_bridge_client:Profile 1", "client-1")
        server.db_set_setting("account_identity:Profile 1", {
            "logged_in": True, "status": "verified", "nickname": "旧账号",
            "user_id": "xhs-user-old", "avatar": "",
        })
        observed = {
            "logged_in": True, "status": "verified", "nickname": "新账号",
            "user_id": "xhs-user-new", "avatar": "",
        }
        server.snooze_account_mismatch("Profile 1")
        with patch.object(browser_profiles, "ensure_server"), patch.object(
            browser_profiles, "select"
        ), patch.object(server, "read_connected_identity", return_value=observed):
            result = server.refresh_account_identity("Profile 1")
        self.assertEqual(result["identity"]["user_id"], "xhs-user-new")

    def test_focus_workspace_opens_profile_specific_url(self):
        with patch.object(browser_profiles, "open_app_tab") as open_tab:
            result = server.focus_account_workspace("Profile 1")
        self.assertTrue(result["ok"])
        self.assertTrue(result["url"].endswith("/?profile=Profile%201"))
        open_tab.assert_called_once()

    def test_minimize_previous_window_skips_unbound_old_client(self):
        with patch.object(browser_profiles, "minimize_window") as minimize:
            self.assertFalse(server.minimize_previous_window("")["ok"])
            self.assertTrue(server.minimize_previous_window("client-old")["ok"])
        minimize.assert_called_once_with("client-old")

    def test_old_extension_without_capabilities_is_refused(self):
        with patch.object(
            browser_profiles, "control", return_value={"capabilities": {"client-1": []}}
        ):
            with self.assertRaisesRegex(RuntimeError, "重新加载扩展"):
                browser_profiles.open_app_tab("http://127.0.0.1:5818/?profile=Profile%201", "client-1")


if __name__ == "__main__":
    unittest.main()
