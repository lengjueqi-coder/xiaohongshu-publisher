from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import publishing_resources
import server


class _JsonResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": json.dumps({"ok": True})}}],
        }).encode("utf-8")


class BackendCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(server, "DB_PATH", Path(self.temp.name) / "test.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        server.init_db()

    def test_local_topics_parse_pasted_text_and_list_with_same_rules(self):
        self.assertEqual(
            publishing_resources.parse_topics("#艺考照，演员资料照\n#艺考照; 北京写真"),
            ["艺考照", "演员资料照", "北京写真"],
        )
        self.assertEqual(
            publishing_resources.parse_topics(["#艺考照 #演员资料照", "北京写真、棚拍"]),
            ["艺考照", "演员资料照", "北京写真", "棚拍"],
        )
        with self.assertRaisesRegex(ValueError, "1–10"):
            publishing_resources.parse_topics("")
        with self.assertRaisesRegex(ValueError, "1–10"):
            publishing_resources.parse_topics([f"话题{i}" for i in range(11)])
        with self.assertRaisesRegex(ValueError, "文字"):
            publishing_resources.parse_topics(["艺考照", 3])

    def test_local_templates_reject_duplicate_names_and_freeze_exact_topics(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            publishing_resources.normalize_templates([
                {"name": "常用", "tags": "#艺考照"},
                {"name": " 常用 ", "tags": "#演员资料照"},
            ])
        templates = publishing_resources.normalize_templates([
            {"name": "常用", "tags": "#艺考照 #演员资料照"},
        ])
        resolved = publishing_resources.resolve_template(
            {"local_topic_template": "常用", "tags": ["旧话题"], "topic_template": "平台模板"},
            templates,
        )
        self.assertEqual(resolved["tags"], ["艺考照", "演员资料照"])
        self.assertEqual(resolved["topic_template"], "")

    def test_pptoken_uses_its_own_model_even_with_deepseek_override(self):
        server.db_set_setting("text_provider", "pptoken")
        server.db_set_setting("text_base_url", "https://pp.example/v1")
        server.db_set_setting("pptoken_model", "vendor/model-a")
        server.db_set_setting("pptoken_api_key", "pp-secret")
        with patch.object(server.urllib.request, "urlopen", return_value=_JsonResponse()) as request:
            result = server.call_deepseek_json(
                "system", "prompt", profile_dir="Default", model="deepseek-flash", strict=True,
            )
        self.assertEqual(result, {"ok": True})
        http_request = request.call_args.args[0]
        self.assertEqual(http_request.full_url, "https://pp.example/v1/chat/completions")
        self.assertEqual(json.loads(http_request.data)["model"], "vendor/model-a")

    def test_copy_reports_actual_provider_and_model(self):
        server.db_set_setting("text_provider", "pptoken")
        server.db_set_setting("text_base_url", "https://pp.example/v1")
        server.db_set_setting("pptoken_model", "vendor/model-b")
        with patch.object(
            server,
            "call_deepseek_json",
            return_value={"title": "测试标题", "content": "测试正文"},
        ):
            copy = server.generate_deepseek_copy({"profile_dir": "Default", "deepseek_model": "deepseek-flash"})
        self.assertEqual(copy["provider"], "pptoken")
        self.assertEqual(copy["provider_label"], "PPToken")
        self.assertEqual(copy["model"], "vendor/model-b")
        self.assertEqual(copy["engine"], "PPToken · vendor/model-b")

    def test_ai_topics_are_ignored_and_only_explicit_topics_are_preserved(self):
        generated = {
            "title": "测试标题",
            "content": "测试正文",
            "tags": ["#唐唐", "文件夹名", "AI自动话题"],
        }
        with patch.object(server, "call_deepseek_json", return_value=generated):
            explicit = server.generate_deepseek_copy({
                "profile_dir": "Default",
                "model_name": "唐唐",
                "tags": ["#唐唐", "用户明确话题"],
            })
            empty = server.generate_deepseek_copy({
                "profile_dir": "Default",
                "model_name": "唐唐",
                "folder_name": "文件夹名",
            })
        self.assertEqual(explicit["tags"], ["#唐唐", "用户明确话题"])
        self.assertEqual(empty["tags"], [])

    def test_profile_reports_note_detail_content_and_topic_completeness(self):
        notes = [
            {"title": "完整", "content": "正文", "tags": ["话题"], "detail_status": "complete"},
            {"title": "空详情", "content": "", "tags": [], "detail_status": "empty"},
            {
                "title": "失败",
                "content": "",
                "tags": [],
                "detail_status": "failed",
                "detail_error": "RuntimeError: detail unavailable",
            },
        ]
        browser_result = {
            "ok": True,
            "json": {"success": True, "notes": {"notes": notes}, "datacenter": {"text": ""}},
        }
        with patch.object(server, "run_xhs_cli", return_value=browser_result), patch.object(
            server,
            "call_deepseek_json",
            return_value={"positioning": "摄影工作室", "topics": ["摄影"]},
        ):
            result = server.generate_account_profile(max_count=20)
        completeness = result["profile"]["source_completeness"]
        self.assertEqual(completeness["requested"], 20)
        self.assertEqual(completeness["read"], 3)
        self.assertEqual(completeness["detail_success"], 2)
        self.assertEqual(completeness["detail_failed"], 1)
        self.assertEqual(completeness["content_success"], 1)
        self.assertEqual(completeness["content_empty"], 1)
        self.assertEqual(completeness["topic_success"], 1)
        self.assertEqual(completeness["topic_empty"], 1)
        self.assertEqual(completeness["with_content"], 1)
        self.assertEqual(completeness["with_topics"], 1)
        self.assertIn("1 条笔记详情读取失败", result["source_error"])
        self.assertIn("detail unavailable", result["source_error"])

    def test_model_connection_accepts_unsaved_form_settings_without_persisting(self):
        server.db_set_setting("text_provider", "deepseek")
        temporary = {
            "text_provider": "pptoken",
            "text_base_url": "https://pp.example/v1",
            "pptoken_model": "vendor/temporary",
            "pptoken_api_key": "temporary-secret",
        }
        with patch.object(server, "call_deepseek_json", return_value={"ok": True}) as call:
            result = server.check_text_model_connection("Default", temporary)
        self.assertEqual(result["provider"], "pptoken")
        self.assertEqual(result["model"], "vendor/temporary")
        self.assertEqual(call.call_args.kwargs["settings"], temporary)
        self.assertEqual(server.account_setting("text_provider", profile_dir="Default"), "deepseek")

    def test_resource_sync_updates_successful_empty_list_and_keeps_failed_cache(self):
        server.db_set_setting("publishing_resources:Default", {
            "templates": [{"name": "旧模板"}],
            "albums": ["旧合集"],
            "synced_at": "old-full-sync",
        })
        result = {
            "templates": [],
            "albums": [],
            "status": {
                "templates": {"ok": True, "count": 0, "topics_complete": True, "message": "模板为空"},
                "albums": {"ok": False, "count": 0, "message": "合集入口不可用"},
            },
            "errors": ["合集入口不可用"],
        }
        with patch.object(server, "select_profile_connection"), patch.object(
            server.publishing_resources, "read_resources", return_value=result,
        ):
            saved = server.sync_publishing_resources("Default")
        self.assertEqual(saved["templates"], [])
        self.assertEqual(saved["albums"], ["旧合集"])
        self.assertTrue(saved["partial"])
        self.assertFalse(saved["ok"])
        self.assertEqual(saved["synced_at"], "old-full-sync")
        self.assertIn("templates_synced_at", saved)
        self.assertNotIn("albums_synced_at", saved)

    def test_resource_sync_caches_failure_without_claiming_success(self):
        with patch.object(server, "select_profile_connection", side_effect=RuntimeError("扩展未连接")):
            saved = server.sync_publishing_resources("Default")
        self.assertFalse(saved["ok"])
        self.assertFalse(saved["partial"])
        self.assertEqual(saved["status"]["templates"]["message"], "扩展未连接")
        self.assertNotIn("synced_at", saved)


if __name__ == "__main__":
    unittest.main()
