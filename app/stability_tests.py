from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import copy_quality
import publish_checks
import server
from automation_engine import AutomationEngine, DEFAULT_PLAN
from publishing_policy import require_schedule_lead


class StabilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        db = patch.object(server, "DB_PATH", self.root / "test.sqlite3")
        db.start()
        self.addCleanup(db.stop)
        server.init_db()

    def test_form_rejects_wrong_long_body_extra_images_and_extra_text(self):
        payload = {"title": "标题", "content": "这是一篇需要完整保留的正文", "images": ["a"], "tags": ["摄影"]}
        state = {"title": "标题", "content": payload["content"] + " #摄影", "image_count": 1,
                 "original": True, "visibility": "公开可见"}
        publish_checks.validate_state(state, payload)
        for changes in ({"content": "另外一篇完全不同但长度足够的正文"}, {"image_count": 2},
                        {"content": state["content"] + " 混入的广告"}, {"original": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                publish_checks.validate_state({**state, **changes}, payload)

    def test_manual_submit_never_clicks_on_failed_readback_and_keeps_pending(self):
        payload = {"profile_dir": "Default", "publish_mode": "immediate", "title": "测试", "content": "正文", "images": ["a"]}
        with patch.object(server, "prepare_publish", return_value={"history_id": 7, "payload": payload}), \
                patch.object(publish_checks, "verify", side_effect=ValueError("图片不一致")), \
                patch.object(server, "click_publish") as click:
            result = server.submit_publish(payload)
        self.assertFalse(result["submitted"])
        self.assertEqual(server.manual_pending("Default")["history_id"], 7)
        click.assert_not_called()

    def test_prepared_readback_uses_transformed_images_and_resolved_topics(self):
        payload = {"publish_mode": "immediate", "images": ["source"], "tags": []}
        effective = {**payload, "images": ["source", "watermark"], "tags": ["摄影"]}
        with patch.object(publish_checks, "verify") as verify:
            server.verify_prepared_publish({"verification_payload": effective}, payload)
        self.assertEqual(verify.call_args.args[1], effective)

    def test_schedule_checked_again_after_upload(self):
        payload = {"publish_mode": "scheduled", "schedule_time": (datetime.now() + timedelta(minutes=59)).isoformat()}
        with patch.object(publish_checks, "verify"), self.assertRaisesRegex(ValueError, "1 小时"):
            server.verify_prepared_publish({"payload": payload}, payload)

    def test_legacy_click_requires_reviewed_manual_draft_and_single_use(self):
        draft = {"profile_dir": "Default", "title": "标题", "content": "正文", "expected_user_id": "owner"}
        history_id = server.record_history(draft, "prefilled")
        with patch.object(server, "select_profile_connection"), patch.object(server, "check_task_identity") as identity, \
                patch.object(server, "verify_prepared_publish") as verify, patch.object(server, "click_publish", return_value={"ok":True}) as click:
            with self.assertRaisesRegex(server.PublishError, "核对凭证"):
                server.submit_manual_pending(history_id, {})
            server.db_set_setting("manual_pending:Default", {"history_id":history_id, "verification_payload":draft})
            with self.assertRaisesRegex(server.PublishError, "核对凭证"):
                server.submit_manual_pending(history_id + 1, {})
            verify.side_effect = ValueError("官方表单发生变化")
            with self.assertRaisesRegex(ValueError, "发生变化"):
                server.submit_manual_pending(history_id, {})
            click.assert_not_called()
            verify.side_effect = None
            self.assertTrue(server.submit_manual_pending(history_id, {})["ok"])
            with self.assertRaises(server.PublishError):
                server.submit_manual_pending(history_id, {})
            click.assert_called_once_with(history_id)
            identity.assert_called_with(draft)

    def test_queue_rejects_short_lead_before_touching_browser(self):
        scheduled = (datetime.now() + timedelta(minutes=40)).isoformat(timespec="minutes")
        job = server.enqueue_job({"title": "测试", "publish_mode": "scheduled"}, scheduled)
        with patch.object(server, "switch_chrome_profile") as switch, patch.object(server, "click_publish") as click:
            self.assertFalse(server.process_queue_job(job))
        switch.assert_not_called()
        click.assert_not_called()

    def test_plan_slots_reserve_upload_buffer(self):
        engine = AutomationEngine(server)
        now = datetime(2026, 11, 17, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        config = {**DEFAULT_PLAN, "windows": ["20:00-23:00"], "ahead_days": 0}
        self.assertGreaterEqual(engine.slot("Default", config, now), now + timedelta(minutes=90))

    def test_schedule_honors_named_timezone_and_rejects_stale_epoch(self):
        payload = {"publish_mode": "scheduled", "schedule_time": "2026-11-17T21:00", "timezone": "Asia/Shanghai"}
        now = datetime(2026, 11, 17, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        require_schedule_lead(payload, now=now)
        with self.assertRaisesRegex(ValueError, "时区记录不一致"):
            require_schedule_lead({**payload, "schedule_epoch": now}, now=now)

    def test_edit_draft_uses_plan_timezone(self):
        engine = AutomationEngine(server)
        slot = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=2)
        raw = slot.strftime("%Y-%m-%dT%H:%M")
        job = server.enqueue_job({"title": "测试", "publish_mode": "scheduled", "timezone": "Asia/Shanghai"}, raw, "draft")
        engine.update_draft("Default", job["id"], {"schedule_time": raw})
        payload = server.queue_rows()[0]["payload"]
        self.assertEqual(payload["schedule_epoch"], datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    def test_plan_settings_never_overwrite_manual_jobs(self):
        engine = AutomationEngine(server)
        for automatic in (False, True):
            server.enqueue_job({"title": str(automatic), "publish_mode": "immediate", "visibility": "仅自己可见",
                                "automation": automatic, "schedule_epoch": time.time()+9000}, "", "draft")
        engine.apply_settings("Default")
        jobs = server.queue_rows()
        self.assertEqual(jobs[0]["payload"]["visibility"], "仅自己可见")
        self.assertEqual(jobs[1]["payload"]["visibility"], "公开可见")

    def test_quality_detects_repetition_and_keeps_other_accounts_separate(self):
        copy = {"title": "选片的三个建议", "content": "资料照选片先关注用途和人物辨识度，再检查是否与其他照片重复。"}
        server.enqueue_job({**copy, "profile_dir": "Default", "publish_mode": "immediate"}, "", "draft")
        with self.assertRaisesRegex(ValueError, "过于相似"):
            copy_quality.ensure_distinct(copy, copy_quality.recent_copies(server, "Default"))
        copy_quality.ensure_distinct(copy, copy_quality.recent_copies(server, "Profile 1"))

    def test_material_notes_are_optional_and_size_bounded(self):
        self.assertEqual(copy_quality.material_notes(self.root), "")
        notes = self.root / "发布说明.txt"
        notes.write_text("只用已确认的拍摄事实", encoding="utf-8-sig")
        self.assertEqual(copy_quality.material_notes(self.root), "只用已确认的拍摄事实")
        notes.write_bytes(b"x" * 24001)
        with self.assertRaises(ValueError):
            copy_quality.material_notes(self.root)

    def test_http_blocks_cross_site_mutation_traversal_and_database_download(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(httpd.shutdown)
        def request(method, path, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
            try:
                conn.request(method, path, headers=headers or {})
                res = conn.getresponse()
                res.read()
                return res.status
            finally:
                conn.close()
        self.assertEqual(request("GET", "/api/version"), 200)
        self.assertEqual(request("POST", "/api/config", {"Origin": "https://example.com"}), 403)
        self.assertEqual(request("GET", "/api/version", {"Host": "attacker.example"}), 403)
        self.assertEqual(request("GET", "/static/../../data/assistant.sqlite3"), 404)
        self.assertEqual(request("GET", "/api/image?path=" + server.urllib.parse.quote(str(server.DB_PATH))), 404)

    def test_producer_wakeup_does_not_shorten_submission_gap(self):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.object(server, "QUEUE_STOP", stop), patch.object(server, "QUEUE_WAKE") as wake, \
                patch.object(server, "queue_master_enabled", return_value=True), \
                patch.object(server, "claim_next_job", return_value={"id": 1}), \
                patch.object(server, "process_queue_job", return_value=True):
            server.queue_worker_loop()
        wake.wait.assert_not_called()
        self.assertGreaterEqual(stop.wait.call_args.args[0], server.QUEUE_GAP_MIN_SECONDS)

    def test_saved_plan_keeps_empty_fields_and_explicit_original(self):
        engine = AutomationEngine(server)
        source = self.root / "photo.jpg"
        source.write_bytes(b"mocked-image-bytes")
        folder = SimpleNamespace(images=[str(source)], path=str(self.root), name="一组", model_name="")
        config = {**DEFAULT_PLAN, "root_path": str(self.root), "min_images": 1, "max_images": 1,
                  "album": "", "topic_template": "", "original": True}
        server.db_set_setting("original_enabled", False)
        server.db_set_setting("default_album", "不该自动选择的合集")
        with patch.object(server, "APP_ROOT", self.root), patch.object(server, "freeze_payload", side_effect=lambda p, _: p):
            payload = engine.make_payload("Default", config, folder, datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=2))
        self.assertTrue(payload["original"])
        self.assertEqual(payload["album"], "")
        self.assertEqual(payload["topic_template"], "")

    def test_original_switch_can_be_disabled_without_confirming_declaration(self):
        import sys
        sys.path.insert(0, str(server.XHS_SKILL_DIR / "scripts"))
        from xhs import publish
        page = Mock()
        page.evaluate.return_value = "clicked"
        with patch.object(publish, "sleep_random"), patch.object(publish, "_confirm_original_declaration") as confirm:
            publish._set_original(page, False)
        confirm.assert_not_called()
        page.evaluate.return_value = "unknown"
        with self.assertRaises(publish.PublishError):
            publish._set_original(page, False)


class BridgeOriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_accepts_cli_and_extension_but_rejects_websites(self):
        import importlib.util
        import websockets
        path = server.XHS_SKILL_DIR / "scripts" / "bridge_server.py"
        spec = importlib.util.spec_from_file_location("isolated_bridge_server", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        bridge = module.BridgeServer()
        async with websockets.serve(bridge.handle, "127.0.0.1", 0, origins=module.ALLOWED_ORIGINS) as listener:
            port = listener.sockets[0].getsockname()[1]
            for origin in (None, "chrome-extension://" + "a" * 32):
                async with websockets.connect(f"ws://127.0.0.1:{port}", origin=origin, proxy=None) as ws:
                    await ws.send(json.dumps({"role": "cli", "method": "ping_server"}))
                    response = json.loads(await ws.recv())
                    self.assertEqual(response["result"]["version"], 4)
            with self.assertRaises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(f"ws://127.0.0.1:{port}", origin="https://example.com", proxy=None):
                    self.fail("Unexpected cross-site connection")


if __name__ == "__main__":
    unittest.main()
