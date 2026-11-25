from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent / "vendor/xiaohongshu-skills/scripts"))
from xhs.bridge import BridgePage
from xhs.errors import CDPError, PublishError
from xhs.publish import require_empty_publish_page


class BackgroundPageTests(unittest.TestCase):
    def test_old_extension_is_never_sent_owned_tab_commands(self):
        page = BridgePage(scope="publish")
        with patch.object(page, "_request", return_value={"clients": ["profile"]}) as rpc:
            with self.assertRaisesRegex(CDPError, "重新加载"):
                page.navigate("https://creator.xiaohongshu.com/publish/publish")
        rpc.assert_called_once_with("ping_server")

    def test_replaced_reviewed_tab_is_never_clicked(self):
        page = BridgePage(scope="publish", create=False, expected_tab=42)
        status = {"clients": ["profile"], "capabilities": {"profile": ["xhs_background_tabs"]}}
        with patch.object(page, "_request", side_effect=[status, {"id": 81}]) as rpc:
            with self.assertRaisesRegex(CDPError, "标签已变化"):
                page.trusted_click_at(10, 10)
        self.assertEqual(rpc.call_count, 2)
        self.assertIsNone(page.tab_id)

    def test_restored_draft_or_unreadable_form_stops_new_upload(self):
        for result in (True, None):
            page = Mock()
            page.evaluate.return_value = result
            with self.assertRaisesRegex(PublishError, "已有草稿"):
                require_empty_publish_page(page)
            page.set_file_input.assert_not_called()
