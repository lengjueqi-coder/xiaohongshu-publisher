from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import publish_checks
import server
import mac_notifications
from automation_engine import AutomationEngine


class PublishingCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_patch = patch.object(server, "DB_PATH", self.root / "test.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        server.init_db()

    def make_history(self, status: str = "accepted") -> tuple[int, Path, Path]:
        materials = self.root / "materials"
        folder = materials / "组一"
        folder.mkdir(parents=True)
        image = folder / "01.jpg"
        Image.new("RGB", (60, 80), "white").save(image)
        payload = {
            "material_mode": "folder",
            "material_root": str(materials),
            "folder_path": str(folder),
            "folder_name": folder.name,
            "images": [str(image)],
            "title": "管理页核验测试",
            "content": "正文",
            "tags": [],
            "profile_dir": "Default",
            "visibility": "仅自己可见",
        }
        payload["archive_info"] = server.make_archive_info(payload)
        return server.record_history(payload, status), folder, materials

    def test_scanner_never_scores_or_warns_about_duplicates(self) -> None:
        folder = self.root / "materials" / "A"
        folder.mkdir(parents=True)
        (folder / "01.jpg").write_bytes(b"image")
        server.record_history(
            {
                "folder_path": str(folder),
                "folder_name": "A",
                "images": [str(folder / "01.jpg")],
                "title": "重复标题",
                "content": "重复正文",
                "profile_dir": "Default",
            },
            "published",
        )
        item = server.scan_folders(str(folder.parent))[0]
        self.assertEqual(item.duplicate_status, "not_scored")
        self.assertEqual(item.published_count, 0)
        warnings = server.content_warnings(
            {"title": "重复标题", "content": "重复正文", "tags": [], "images": [], "profile_dir": "Default"}
        )
        self.assertFalse(any("重复" in warning for warning in warnings))

    def test_immediate_receipt_matches_title_and_observed_time(self) -> None:
        cards = [{"text": "管理页核验测试\n2026-11-06 02:05\n浏览\n0\n点赞\n0", "url": "https://example/note"}]
        receipt = publish_checks.match_receipt(
            cards,
            {
                "title": "管理页核验测试",
                "schedule_time": "",
                "receipt_reference_time": "2026-11-06T02:04:30",
            },
        )
        self.assertEqual(receipt["status"], "published")

    def test_receipt_rejects_ambiguous_immediate_title(self) -> None:
        cards = [
            {"text": "相同标题\n2026-11-06 02:04\n浏览\n0\n点赞\n0", "url": "one"},
            {"text": "相同标题\n2026-11-06 02:05\n浏览\n0\n点赞\n0", "url": "two"},
        ]
        receipt = publish_checks.match_receipt(
            cards,
            {"title": "相同标题", "schedule_time": "", "receipt_reference_time": "2026-11-06T02:05:00"},
        )
        self.assertEqual(receipt["status"], "unconfirmed")

    def test_immediate_receipt_rejects_old_same_title_outside_five_minutes(self) -> None:
        cards = [{"text": "相同标题\n2026-11-06 01:45\n浏览\n0\n点赞\n0", "url": "old"}]
        receipt = publish_checks.match_receipt(
            cards,
            {"title": "相同标题", "schedule_time": "", "receipt_reference_time": "2026-11-06T02:05:00"},
        )
        self.assertEqual(receipt["status"], "unconfirmed")

    def test_structured_private_receipt_returns_platform_note_id(self) -> None:
        cards = [{
            "title": "私密测试笔记",
            "time": "2026-11-06 02:05",
            "text": "私密测试笔记\n浏览\n0\n点赞\n0",
            "private": True,
            "note_id": "abcdef0123456789",
            "url": "https://example/note",
        }]
        receipt = publish_checks.match_receipt(
            cards,
            {
                "title": "私密测试笔记",
                "schedule_time": "",
                "receipt_reference_time": "2026-11-06T02:05:00",
                "visibility": "仅自己可见",
            },
        )
        self.assertEqual(receipt["status"], "published")
        self.assertEqual(receipt["note_id"], "abcdef0123456789")

    def test_manual_receipt_uses_verification_time_across_midnight(self) -> None:
        history_id, _folder, _materials = self.make_history()
        with server.db_connection() as db:
            db.execute(
                "UPDATE publish_history SET created_at=? WHERE id=?",
                ("2026-11-05T23:59:30", history_id),
            )
        identity = {"status": "verified", "user_id": "user-1", "logged_in": True}
        server.db_set_setting("account_identity:Default", identity)
        cards = [{
            "text": "管理页核验测试\n2026-11-06 00:01\n仅自己可见\n浏览\n0\n点赞\n0",
            "url": "https://example/note",
        }]
        with patch.object(server, "select_profile_connection"), patch.object(
            server, "read_connected_identity", return_value=identity
        ), patch.object(publish_checks, "read_management", return_value=cards), patch.object(
            server, "now_iso", return_value="2026-11-06T00:02:00"
        ):
            receipt = server.verify_history_published(history_id)
        self.assertEqual(receipt["status"], "published")

    def test_history_archives_only_after_management_reports_published(self) -> None:
        history_id, folder, materials = self.make_history()
        identity = {"status": "verified", "user_id": "user-1", "logged_in": True}
        server.db_set_setting("account_identity:Default", identity)
        with patch.object(server, "select_profile_connection"), patch.object(
            server, "read_connected_identity", return_value=identity
        ), patch.object(publish_checks, "read_management", return_value=[]), patch.object(
            publish_checks, "match_receipt", return_value={"status": "reviewing", "reason": "审核中"}
        ):
            result = server.verify_history_published(history_id)
        self.assertEqual(result["status"], "reviewing")
        self.assertTrue(folder.exists())

        with patch.object(server, "select_profile_connection"), patch.object(
            server, "read_connected_identity", return_value=identity
        ), patch.object(publish_checks, "read_management", return_value=[]), patch.object(
            publish_checks, "match_receipt", return_value={"status": "published", "reason": "已发布"}
        ):
            result = server.verify_history_published(history_id)
        self.assertEqual(result["status"], "published")
        self.assertFalse(folder.exists())
        self.assertTrue((materials / "已发布" / "组一" / "01.jpg").exists())

    def test_queue_submission_does_not_archive_before_receipt(self) -> None:
        job = server.enqueue_job(
            {
                "profile_dir": "Default",
                "publish_mode": "immediate",
                "title": "立即发布待核验",
                "content": "正文",
                "images": ["frozen.jpg"],
            },
            "",
        )
        prepared = {"history_id": 7}
        with patch.object(server, "switch_chrome_profile"), patch.object(
            server, "prepare_publish", return_value=prepared
        ), patch.object(server, "click_publish", return_value={"ok": True}), patch.object(
            server, "archive_history"
        ) as archive:
            self.assertTrue(server.process_queue_job(job))
        archive.assert_not_called()

    def test_reconcile_selects_immediate_submissions(self) -> None:
        history_id, _folder, _materials = self.make_history()
        job = server.enqueue_job(
            {
                "profile_dir": "Default",
                "publish_mode": "immediate",
                "title": "管理页核验测试",
                "content": "正文",
                "images": ["frozen.jpg"],
                "expected_user_id": "user-1",
            },
            "",
        )
        server.update_job(job["id"], status="submitted", history_id=history_id,
                          started_at="2026-11-06T02:00:00", completed_at="2026-11-06T02:00:10")
        engine = AutomationEngine(server)
        row = engine.next_receipt_job(now=datetime(2026, 11, 6, 2, 1).timestamp())
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], job["id"])

    def test_reconcile_published_receipt_archives_group_and_updates_queue_view(self) -> None:
        history_id, folder, materials = self.make_history()
        job = server.enqueue_job(
            {
                "profile_dir": "Default",
                "publish_mode": "immediate",
                "title": "管理页核验测试",
                "content": "正文",
                "images": ["frozen.jpg"],
                "expected_user_id": "user-1",
            },
            "",
        )
        completed = "2026-11-06T02:00:10"
        server.update_job(job["id"], status="submitted", history_id=history_id, completed_at=completed)
        engine = AutomationEngine(server)
        now = datetime(2026, 11, 6, 2, 1).timestamp()
        with patch("automation_engine.time.time", return_value=now), patch.object(
            server, "switch_chrome_profile"
        ), patch.object(server, "check_task_identity"), patch.object(
            publish_checks, "read_management", return_value=[]
        ), patch.object(
            publish_checks, "match_receipt", return_value={"status": "published", "reason": "已发布"}
        ):
            engine.reconcile_due()
        self.assertFalse(folder.exists())
        self.assertTrue((materials / "已发布" / "组一" / "01.jpg").exists())
        queued = server.queue_rows()[0]
        self.assertEqual(queued["status"], "submitted")
        self.assertEqual(queued["history_status"], "published")
        self.assertTrue(str(queued["archive_result"]).startswith("已归档："))

    def test_reconcile_published_receipt_keeps_changed_materials_and_alerts(self) -> None:
        history_id, folder, _materials = self.make_history()
        with (folder / "01.jpg").open("ab") as image:
            image.write(b"changed-after-queue")
        job = server.enqueue_job(
            {
                "profile_dir": "Default",
                "publish_mode": "immediate",
                "title": "管理页核验测试",
                "content": "正文",
                "images": ["frozen.jpg"],
                "expected_user_id": "user-1",
            },
            "",
        )
        server.update_job(
            job["id"],
            status="submitted",
            history_id=history_id,
            completed_at="2026-11-06T02:00:10",
        )
        engine = AutomationEngine(server)
        now = datetime(2026, 11, 6, 2, 1).timestamp()
        with patch("automation_engine.time.time", return_value=now), patch.object(
            server, "switch_chrome_profile"
        ), patch.object(server, "check_task_identity"), patch.object(
            publish_checks, "read_management", return_value=[]
        ), patch.object(
            publish_checks, "match_receipt", return_value={"status": "published", "reason": "已发布"}
        ):
            engine.reconcile_due()
        self.assertTrue(folder.exists())
        queued = server.queue_rows()[0]
        self.assertEqual(queued["history_status"], "published")
        self.assertTrue(str(queued["archive_result"]).startswith("未自动归档："))
        alerts = mac_notifications.alerts(server)
        self.assertEqual(alerts[0]["kind"], "archive_error")


if __name__ == "__main__":
    unittest.main()
