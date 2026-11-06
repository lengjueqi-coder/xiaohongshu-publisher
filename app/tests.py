from __future__ import annotations

import tempfile
import unittest
import random
from datetime import date, datetime
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server
from account_identity import IdentityUnavailable, normalize_identity, safe_avatar_url
from server import (
    choose_image_group,
    content_warnings,
    generate_copy,
    infer_model_name,
    infer_shooting_date,
    infer_shooting_date_from_sources,
    init_db,
    resolve_planned_schedule_time,
    resolve_schedule_time,
    scan_folders,
)


class AssistantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.tmp.name) / "test.sqlite3"
        init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_infer_model_name_from_dated_folder(self) -> None:
        self.assertEqual(infer_model_name("2026-05-14_张三_演员资料照"), "张三")
        self.assertEqual(infer_model_name("模特-李四-棚拍"), "李四")

    def test_generate_copy_has_required_fields(self) -> None:
        copy = generate_copy(
            {
                "style": "praise",
                "model_name": "张三",
                "folder_name": "张三_棚拍演员资料",
                "notes": "白色背景",
                "copy_engine": "local",
            }
        )
        self.assertTrue(copy["title"])
        self.assertTrue(copy["content"])
        self.assertNotIn("张三", copy["tags"])

    def test_publish_preferences_are_account_scoped_and_validated(self) -> None:
        server.db_set_setting("account_defaults:Profile 1:schedule_windows", ["19:00-21:00"])
        server.save_publish_preferences("Default", {"visibility": "公开可见", "publish_mode": "immediate"})
        server.save_publish_preferences("Profile 1", {
            "visibility": "仅自己可见",
            "publish_mode": "scheduled",
            "default_schedule_window": "19:00-21:00",
            "original_enabled": False,
        })
        self.assertEqual(server.account_setting("visibility", profile_dir="Default"), "公开可见")
        self.assertEqual(server.account_setting("publish_mode", profile_dir="Profile 1"), "scheduled")
        self.assertEqual(server.account_setting("default_schedule_window", profile_dir="Profile 1"), "19:00-21:00")
        self.assertFalse(server.account_setting("original_enabled", True, profile_dir="Profile 1"))
        self.assertTrue(server.account_setting("original_enabled", True, profile_dir="Default"))
        with self.assertRaises(ValueError):
            server.save_publish_preferences("Default", {"visibility": "错误"})
        with self.assertRaises(ValueError):
            server.save_publish_preferences("Profile 1", {"default_schedule_window": "21:00-22:00"})
        with self.assertRaises(ValueError):
            server.save_publish_preferences("Profile 1", {"original_enabled": 0})
        server.save_publish_preferences("Profile 2", {
            "default_schedule_window": "早上 10:00",
            "original_enabled": True,
        })
        self.assertEqual(server.account_setting("default_schedule_window", profile_dir="Profile 2"), "早上 10:00")
        self.assertEqual(server.account_setting("visibility", profile_dir="Default"), "公开可见")

    def test_scan_folders_finds_image_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-05-14_张三_演员资料照"
            folder.mkdir()
            (folder / "01.jpg").write_bytes(b"fake")
            items = scan_folders(tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].model_name, "张三")
        self.assertEqual(items[0].image_count, 1)
        self.assertEqual(items[0].duplicate_status, "not_scored")
        self.assertEqual(items[0].shooting_date, "2026-05-14")

    def test_published_folder_is_not_scanned(self) -> None:
        root = Path(self.tmp.name) / "materials"
        (root / "已发布" / "old").mkdir(parents=True)
        (root / "已发布" / "old" / "old.jpg").write_bytes(b"old")
        (root / "new").mkdir()
        (root / "new" / "new.jpg").write_bytes(b"new")
        self.assertEqual([x.name for x in scan_folders(str(root))], ["new"])

    def test_archive_moves_only_unchanged_group_and_preserves_hierarchy(self) -> None:
        root = Path(self.tmp.name) / "materials"
        folder = root / "2026" / "09" / "组一"
        folder.mkdir(parents=True)
        image = folder / "one.jpg"
        image.write_bytes(b"image")
        payload = {"material_mode": "folder", "material_root": str(root), "folder_path": str(folder), "images": [str(image)]}
        info = server.make_archive_info(payload)
        history = server.record_history({**payload, "archive_info": info}, "accepted")
        result = server.archive_history(history)
        self.assertIn("已归档", result)
        self.assertFalse(folder.exists())
        self.assertEqual((root / "已发布" / "2026" / "09" / "组一" / "one.jpg").read_bytes(), b"image")
        self.assertEqual(server.archive_history(history), result)

    def test_archive_keeps_mixed_changed_and_conflicting_folders(self) -> None:
        root = Path(self.tmp.name) / "materials"
        folder = root / "组一"
        folder.mkdir(parents=True)
        image = folder / "one.jpg"
        image.write_bytes(b"image")
        payload = {"material_mode": "folder", "material_root": str(root), "folder_path": str(folder), "images": [str(image)]}
        self.assertIn("skip", server.make_archive_info({**payload, "material_mode": "mixed"}))
        self.assertIn("skip", server.make_archive_info({**payload, "folder_path": str(root)}))
        info = server.make_archive_info(payload)
        history = server.record_history({**payload, "archive_info": info}, "scheduled")
        (folder / "new.jpg").write_bytes(b"new")
        self.assertIn("已变化", server.archive_history(history))
        self.assertTrue(image.exists())
        (folder / "new.jpg").unlink()
        info = server.make_archive_info(payload)
        history = server.record_history({**payload, "archive_info": info}, "accepted")
        target = root / "已发布" / "组一"
        target.mkdir(parents=True)
        self.assertIn("已存在", server.archive_history(history))
        self.assertTrue(image.exists())

    def test_scan_folders_recurses_year_month_and_keeps_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2025" / "09" / "张三_演员资料照"
            folder.mkdir(parents=True)
            (folder / "01.jpg").write_bytes(b"fake")
            items = scan_folders(tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].year, "2025")
        self.assertEqual(items[0].month, "09")
        self.assertEqual(items[0].relative_path, "2025/09/张三_演员资料照")
        self.assertEqual(items[0].hierarchy, ["2025", "09", "张三_演员资料照"])

    def test_scan_folders_does_not_treat_numeric_publish_folder_as_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026" / "10" / "批量测试" / "03"
            folder.mkdir(parents=True)
            (folder / "01.jpg").write_bytes(b"fake")
            items = scan_folders(tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].year, "2026")
        self.assertEqual(items[0].month, "10")
        self.assertEqual(items[0].relative_path, "2026/10/批量测试/03")

    def test_scan_folders_accepts_selected_publish_folder_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026年10月"
            folder.mkdir()
            (folder / "01.png").write_bytes(b"fake")
            items = scan_folders(str(folder))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].relative_path, folder.name)
        self.assertEqual(items[0].year, "2026")
        self.assertEqual(items[0].month, "10")

    def test_scan_folders_infers_calendar_from_selected_year_month_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "2026" / "10"
            folder = root / "张三"
            folder.mkdir(parents=True)
            (folder / "01.jpg").write_bytes(b"fake")
            items = scan_folders(str(root))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].year, "2026")
        self.assertEqual(items[0].month, "10")
        self.assertEqual(items[0].relative_path, "张三")

    def test_scan_folders_does_not_merge_curated_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("A", "B"):
                folder = Path(tmp) / name
                folder.mkdir()
                (folder / "01.jpg").write_bytes(b"same curated source")
            items = scan_folders(tmp)
        self.assertEqual({item.name for item in items}, {"A", "B"})

    def test_queue_is_persisted_in_sqlite(self) -> None:
        job = server.enqueue_job(
            {
                "folder_path": "/tmp/A",
                "folder_name": "A",
                "images": ["/tmp/A/01.jpg"],
                "title": "测试标题",
                "content": "测试正文",
                "tags": ["测试"],
            },
            "2099-01-01T21:00",
        )
        rows = server.queue_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], job["id"])
        self.assertEqual(rows[0]["status"], "queued")
        self.assertEqual(rows[0]["payload"]["title"], "测试标题")

    def test_planned_schedule_skips_reserved_slot(self) -> None:
        server.db_set_setting("daily_limit", 3)
        server.enqueue_job(
            {
                "folder_path": "/tmp/A",
                "folder_name": "A",
                "images": ["/tmp/A/01.jpg"],
                "title": "第一条",
                "content": "正文",
                "profile_dir": "Default",
            },
            "2026-05-15T10:00",
        )
        planned = resolve_planned_schedule_time(
            "早上 10:00",
            profile_dir="Default",
            now_ref=datetime(2026, 5, 15, 8, 0),
        )
        self.assertEqual(planned, "2026-05-16T10:00")

    def test_planned_schedule_respects_daily_limit_for_queued_jobs(self) -> None:
        server.db_set_setting("daily_limit", 1)
        server.enqueue_job(
            {
                "folder_path": "/tmp/A",
                "folder_name": "A",
                "images": ["/tmp/A/01.jpg"],
                "title": "第一条",
                "content": "正文",
                "profile_dir": "Default",
            },
            "2026-05-15T10:00",
        )
        planned = resolve_planned_schedule_time(
            "早上 10:00",
            profile_dir="Default",
            now_ref=datetime(2026, 5, 15, 8, 0),
        )
        self.assertEqual(planned, "2026-05-16T10:00")

    def test_planned_schedule_isolated_by_profile(self) -> None:
        server.db_set_setting("daily_limit", 1)
        server.enqueue_job(
            {
                "folder_path": "/tmp/A",
                "folder_name": "A",
                "images": ["/tmp/A/01.jpg"],
                "title": "Default账号",
                "content": "正文",
                "profile_dir": "Default",
            },
            "2026-05-15T10:00",
        )
        planned = resolve_planned_schedule_time(
            "早上 10:00",
            profile_dir="Profile 1",
            now_ref=datetime(2026, 5, 15, 8, 0),
        )
        self.assertEqual(planned, "2026-05-15T10:00")

    def test_manual_profile_continues_when_platform_read_fails(self) -> None:
        captured: dict[str, str] = {}

        def fake_ai(_system: str, prompt: str, timeout: int = 120, profile_dir: str | None = None):
            captured["prompt"] = prompt
            return {
                "account_type": "摄影工作室",
                "positioning": "为演员提供资料照",
                "track": "演员资料照",
                "audience": "演员与艺考生",
                "content_style": "专业自然",
                "best_performing": "案例展示",
                "topics": ["演员资料照"],
                "avoid": "避免夸大承诺",
            }

        with patch.object(server, "run_xhs_cli", return_value={"ok": False, "json": {}, "stderr": "未登录"}), patch.object(
            server, "call_deepseek_json", side_effect=fake_ai
        ):
            result = server.generate_account_profile(
                manual_keywords="演员资料照 摄影工作室",
                manual_topics="演员资料照 北京写真",
                manual_notes="语气专业，不写夸张承诺",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"]["source_error"], "未登录")
        self.assertIn("演员资料照 摄影工作室", captured["prompt"])

    def test_account_identity_isolated_and_disconnect_preserves_cache(self) -> None:
        server.db_set_setting("active_chrome_profile", "Default")
        saved = {"nickname": "摄影师唐唐", "avatar": "https://sns-avatar-qc.xhscdn.com/a.jpg", "status": "verified"}
        server.db_set_setting("account_identity:Default", saved)
        server.db_set_setting("account_identity:Profile 1", {"nickname": "其他账号"})
        with patch.object(server, "read_connected_identity", side_effect=IdentityUnavailable("扩展未连接")):
            result = server.refresh_account_identity("Default")
        self.assertEqual(result["identity"]["nickname"], "摄影师唐唐")
        self.assertEqual(result["identity"]["status"], "unavailable")
        self.assertEqual(server.account_identity("Profile 1")["nickname"], "其他账号")
        with self.assertRaises(server.PublishError):
            server.refresh_account_identity("Profile 1")
        server.clear_account_identity("Default")
        self.assertEqual(server.account_identity("Default")["nickname"], "")
        self.assertEqual(server.account_identity("Default")["status"], "logged_out")
        self.assertEqual(server.account_identity("Profile 1")["nickname"], "其他账号")

    def test_account_list_returns_cached_identity_without_browser_reads(self) -> None:
        server.db_set_setting("account_identity:Default", {"nickname": "摄影师唐唐"})
        with patch.object(server, "chrome_profiles", return_value=[{"dir": "Default", "label": "崛起"}]), patch.object(server, "read_connected_identity") as reader, patch.object(server, "BROWSER_LOCK") as browser_lock:
            result = server.account_list()
        reader.assert_not_called()
        browser_lock.__enter__.assert_not_called()
        self.assertEqual(result["profiles"][0]["identity"]["nickname"], "摄影师唐唐")

    def test_identity_accepts_only_signed_in_account_and_safe_avatar(self) -> None:
        self.assertEqual(safe_avatar_url("//sns-avatar-qc.xhscdn.com/a.jpg"), "https://sns-avatar-qc.xhscdn.com/a.jpg")
        for url in ("javascript:alert(1)", "https://xhscdn.com.evil.test/a", "http://xhscdn.com/a", "https://user:pass@xhscdn.com/a", "https://[bad"):
            self.assertEqual(safe_avatar_url(url), "")
        with self.assertRaises(IdentityUnavailable):
            normalize_identity({"nickname": "被访问的作者", "logged_in": None})
        identity = normalize_identity({"logged_in": True, "nickname": "摄影师唐唐", "avatar": "https://evil.test/a"})
        self.assertEqual(identity["avatar"], "")
        self.assertEqual(normalize_identity({"logged_in": False, "nickname": "旧账号"})["nickname"], "")

    def test_profile_generation_saved_to_origin_when_account_changes_during_ai(self) -> None:
        server.db_set_setting("active_chrome_profile", "Default")
        def ai_changes_account(*_args, **_kwargs):
            server.db_set_setting("active_chrome_profile", "Profile 1")
            return {"positioning": "摄影工作室", "topics": []}
        with patch.object(server, "run_xhs_cli", return_value={"ok": False, "stderr": "未连接"}), patch.object(server, "call_deepseek_json", side_effect=ai_changes_account):
            result = server.generate_account_profile(manual_keywords="摄影", expected_profile="Default")
        self.assertEqual(result["profile_dir"], "Default")
        self.assertEqual(server.get_account_profile("Default")["positioning"], "摄影工作室")
        self.assertIsNone(server.get_account_profile("Profile 1"))

    def test_profile_generation_rejects_stale_account_before_browser_or_ai(self) -> None:
        server.db_set_setting("active_chrome_profile", "Profile 1")
        with patch.object(server, "run_xhs_cli") as browser, patch.object(server, "call_deepseek_json") as ai:
            with self.assertRaises(server.PublishError):
                server.generate_account_profile(manual_keywords="摄影", expected_profile="Default")
        browser.assert_not_called()
        ai.assert_not_called()

    def test_init_db_marks_inflight_jobs_unknown_and_pauses_queue(self) -> None:
        job = server.enqueue_job(
            {
                "folder_path": "/tmp/A",
                "folder_name": "A",
                "images": ["/tmp/A/01.jpg"],
                "title": "提交中",
                "content": "正文",
            },
            "2099-01-01T21:00",
        )
        server.update_job(job["id"], status="submitting")
        server.db_set_setting("queue_enabled", True)
        init_db()
        rows = server.queue_rows()
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertFalse(server.db_get_setting("queue_enabled", True))

    def test_prefilled_history_does_not_consume_daily_limit(self) -> None:
        payload = {
            "folder_path": "/tmp/A",
            "folder_name": "A",
            "images": ["/tmp/A/01.jpg"],
            "title": "测试标题",
            "content": "测试正文",
            "tags": [],
            "schedule_time": f"{date.today().isoformat()}T21:00",
        }
        history_id = server.record_history(payload, "prefilled")
        self.assertEqual(server.today_usage_count(), 0)
        server.update_history_status(history_id, "published")
        self.assertEqual(server.today_usage_count(), 1)

    def test_click_publish_requires_platform_confirmation(self) -> None:
        payload = {
            "folder_path": "/tmp/A",
            "folder_name": "A",
            "images": ["/tmp/A/01.jpg"],
            "title": "未确认的提交",
            "content": "正文",
        }
        history_id = server.record_history(payload, "prefilled")
        result = {"ok": True, "json": {"success": True, "status": "发布完成"}}
        with patch.object(server, "run_xhs_cli", return_value=result), patch.object(
            server.time, "sleep", return_value=None
        ):
            clicked = server.click_publish(history_id)
        self.assertFalse(clicked["ok"])
        self.assertTrue(clicked["uncertain"])
        self.assertEqual(server.history_rows(1)[0]["status"], "unknown")

    def test_click_publish_marks_accepted_after_platform_confirmation(self) -> None:
        payload = {
            "folder_path": "/tmp/A",
            "folder_name": "A",
            "images": ["/tmp/A/01.jpg"],
            "title": "已确认的提交",
            "content": "正文",
        }
        history_id = server.record_history(payload, "prefilled")
        result = {
            "ok": True,
            "json": {
                "success": True,
                "confirmed": True,
                "url": "https://creator.xiaohongshu.com/publish/publish?published=true",
            },
        }
        with patch.object(server, "run_xhs_cli", return_value=result), patch.object(
            server.time, "sleep", return_value=None
        ):
            clicked = server.click_publish(history_id)
        self.assertTrue(clicked["ok"])
        self.assertEqual(server.history_rows(1)[0]["status"], "accepted")

    def test_queue_processes_private_batch_serially_after_platform_confirmation(self) -> None:
        from PIL import Image

        image = Path(self.tmp.name) / "01.jpg"
        Image.new("RGB", (80, 80), (220, 180, 120)).save(image, "JPEG")
        calls: list[str] = []

        def fake_cli(args, timeout=180):
            calls.append(args[0])
            if args[0] == "fill-publish":
                return {"ok": True, "json": {"success": True}}
            if args[0] == "click-publish":
                return {
                    "ok": True,
                    "json": {
                        "success": True,
                        "confirmed": True,
                        "url": "https://creator.xiaohongshu.com/publish/publish?published=true",
                    },
                }
            raise AssertionError(f"unexpected CLI call: {args[0]}")

        for index in range(3):
            server.enqueue_job(
                {
                    "folder_path": f"{self.tmp.name}/batch-{index}",
                    "folder_name": f"批量-{index}",
                    "images": [str(image)],
                    "title": f"私密批量测试{index}",
                    "content": "仅自己可见的批量测试正文",
                    "tags": ["测试"],
                    "visibility": "仅自己可见",
                    "publish_mode": "scheduled",
                    "schedule_time": f"2099-01-01T21:{index:02d}",
                    "profile_dir": "Default",
                },
                f"2099-01-01T21:{index:02d}",
            )

        with patch.object(server, "switch_chrome_profile", return_value={"ready": True}), patch.object(
            server, "run_xhs_cli", side_effect=fake_cli
        ), patch.object(server.time, "sleep", return_value=None):
            while True:
                job = server.claim_next_job()
                if not job:
                    break
                self.assertTrue(server.process_queue_job(job))

        jobs = server.queue_rows()
        self.assertEqual([job["status"] for job in jobs], ["submitted", "submitted", "submitted"])
        history = server.history_rows(10)
        self.assertEqual(len(history), 3)
        self.assertTrue(all(row["status"] == "scheduled" for row in history))
        self.assertTrue(all(row["visibility"] == "仅自己可见" for row in history))
        self.assertEqual(calls, ["fill-publish", "click-publish"] * 3)

    def test_infer_shooting_date_from_folder_name(self) -> None:
        self.assertEqual(infer_shooting_date("26.1.12郜奕汐", date(2026, 5, 14)), "2026-01-12")
        self.assertEqual(infer_shooting_date("11.7艺考周佳灵", date(2026, 5, 14)), "2025-11-07")

    def test_infer_shooting_date_from_image_filename(self) -> None:
        self.assertEqual(
            infer_shooting_date_from_sources("周佳灵", [Path("2026-01-12_周佳灵_0001.jpg")], date(2026, 5, 14)),
            "2026-01-12",
        )

    def test_content_warnings_catches_length(self) -> None:
        warnings = content_warnings(
            {
                "title": "这是一个超过二十个中文字符的小红书测试标题",
                "content": "正文",
                "tags": [str(i) for i in range(11)],
                "images": [],
            }
        )
        self.assertTrue(any("标题超过" in item for item in warnings))
        self.assertTrue(any("话题超过" in item for item in warnings))

    def test_content_warnings_catches_past_schedule(self) -> None:
        warnings = content_warnings(
            {
                "title": "测试标题",
                "content": "正文",
                "tags": [],
                "images": [],
                "schedule_time": "2000-01-01T08:00",
            }
        )
        self.assertTrue(any("定时发布时间" in item for item in warnings))

    def test_resolve_schedule_fixed_time_uses_next_available_slot(self) -> None:
        self.assertEqual(
            resolve_schedule_time("早上 10:00", datetime(2026, 5, 15, 9, 0)),
            "2026-05-15T10:00",
        )
        self.assertEqual(
            resolve_schedule_time("早上 10:00", datetime(2026, 5, 15, 12, 0)),
            "2026-05-16T10:00",
        )

    def test_resolve_schedule_range_picks_minute_inside_window(self) -> None:
        value = resolve_schedule_time(
            "晚上 22:00-24:00",
            datetime(2026, 5, 15, 12, 0),
            random.Random(1),
        )
        picked = datetime.fromisoformat(value)
        self.assertEqual(picked.date(), date(2026, 5, 15))
        self.assertGreaterEqual(picked.hour * 60 + picked.minute, 22 * 60)
        self.assertLessEqual(picked.hour * 60 + picked.minute, 24 * 60)

    def test_choose_image_group_limits_and_sorts(self) -> None:
        images = ["/tmp/03.jpg", "/tmp/01.jpg", "/tmp/02.jpg"]
        self.assertEqual(choose_image_group(images, 2), ["/tmp/01.jpg", "/tmp/03.jpg"])

    def test_choose_image_group_supports_platform_limit_and_never_exceeds_it(self) -> None:
        images = [f"/tmp/{index:02}.jpg" for index in range(24)]
        self.assertEqual(len(choose_image_group(images)), server.MAX_NOTE_IMAGES)
        self.assertEqual(len(choose_image_group(images, 100)), server.MAX_NOTE_IMAGES)

    def test_publish_image_limit_is_enforced_without_silent_truncation(self) -> None:
        server.validate_note_image_count([f"{index}.jpg" for index in range(server.MAX_NOTE_IMAGES)])
        with self.assertRaisesRegex(server.PublishError, "最多发布 18 张"):
            server.validate_note_image_count([f"{index}.jpg" for index in range(server.MAX_NOTE_IMAGES + 1)])

    def test_imported_images_are_copied_into_managed_local_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = server.store_imported_image("../../来自外部目录/photo.jpg", b"image bytes", Path(tmp))
            self.assertEqual(target.name, "photo.jpg")
            self.assertTrue(target.is_relative_to(Path(tmp)))
            self.assertEqual(target.read_bytes(), b"image bytes")
            with self.assertRaisesRegex(ValueError, "只支持拖入"):
                server.store_imported_image("clip.mov", b"video", Path(tmp))


if __name__ == "__main__":
    unittest.main()
