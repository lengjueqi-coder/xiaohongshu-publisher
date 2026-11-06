"""Regression coverage for rendering, AI error handling and template cleanup."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageChops, ImageStat
import imagefx
import server

sys.path.insert(0, str(server.XHS_SKILL_DIR / "scripts"))
from xhs import publish as form


class PreviewCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "photo.png"
        Image.new("RGB", (640, 960), (80, 80, 80)).save(self.source)
        self.db = patch.object(server, "DB_PATH", self.root / "test.sqlite3")
        self.db.start(); self.addCleanup(self.db.stop)
        server.init_db()

    def test_white_watermark_never_darkens_and_opacity_is_monotonic(self):
        results = []
        for opacity in (0, 25, 70):
            params = {**imagefx.DEFAULT_PARAMS, "watermark_enabled": True,
                      "watermark_text": "唐唐", "watermark_opacity": opacity}
            image = imagefx.compose_one(self.source, params)
            self.assertGreaterEqual(image.getextrema()[0][0], 80)
            results.append(ImageStat.Stat(image).mean[0])
        self.assertEqual(results[0], 80)
        self.assertLess(results[0], results[1])
        self.assertLess(results[1], results[2])

    def test_logo_opacity_and_vertical_horizontal_coordinates(self):
        logo = self.root / "logo.png"
        Image.new("RGBA", (40,40), (255,255,255,255)).save(logo)
        params = {**imagefx.DEFAULT_PARAMS,"logo_enabled":True,"logo_path":str(logo),
                  "logo_position":"25%,75%","logo_opacity":50,"logo_scale":10}
        result = imagefx.compose_one(self.source, params)
        self.assertAlmostEqual(result.getpixel((480,240))[0], 168, delta=1)
        self.assertEqual(result.getpixel((160,720))[0], 80)
        self.assertEqual(server.get_fx_params({"logo_opacity":25})["logo_opacity"],25)

    def test_preview_uses_full_resolution_composite_then_downscale(self):
        Image.new("RGB", (1800,2400), (80,80,80)).save(self.source)
        params = {**imagefx.DEFAULT_PARAMS,"watermark_enabled":True,"watermark_text":"TEST"}
        full = imagefx.compose_one(self.source, params)
        from PIL import ImageOps
        expected = ImageOps.contain(full,(1600,1600))
        buffer=io.BytesIO(); expected.save(buffer,format="JPEG",quality=88)
        self.assertEqual(imagefx.render_preview(self.source,params),buffer.getvalue())
        paths, notes=imagefx.apply_for_publish([str(self.source)],params,self.root/"output")
        self.assertFalse(notes)
        buffer=io.BytesIO(); full.save(buffer,format="JPEG",quality=92)
        self.assertEqual(Path(paths[0]).read_bytes(),buffer.getvalue())

    def test_template_has_no_folder_album_or_location_paragraphs(self):
        result=server.generate_copy({"copy_engine":"local","folder_name":"26.1.12样片", "album":"私有专辑",
                                     "location":"某个地点","notes":"备注"})
        for word in ("26.1.12", "私有专辑", "某个地点", "这组素材关键词"):
            self.assertNotIn(word,result["content"])

    def test_ai_failure_is_explicit_and_strict_mode_never_returns_template(self):
        with patch.object(server,"call_deepseek_json",side_effect=server.PublishError("余额不足")):
            fallback=server.generate_copy({"copy_engine":"auto"})
            self.assertFalse(fallback["ai_used"])
            self.assertIn("余额不足",fallback["warning"])
            with self.assertRaisesRegex(server.PublishError,"余额不足"):
                server.generate_copy({"copy_engine":"deepseek"})

    def test_polish_receives_current_text_preserves_topics_and_account(self):
        with patch.object(server,"call_deepseek_json",return_value={"title":"更自然标题","content":"当前正文的润色"}) as call:
            result=server.polish_copy({"title":"用户手改标题","content":"用户手改正文","tags":["原话题"],"profile_dir":"Profile 1"})
            self.assertEqual(result["tags"],["原话题"])
            self.assertIn("用户手改正文",call.call_args.args[1])
            self.assertEqual(call.call_args.kwargs["profile_dir"],"Profile 1")

    def test_generation_does_not_send_management_metadata(self):
        with patch.object(server,"call_deepseek_json",return_value={"title":"标题","content":"正文","tags":["摄影"]}) as call:
            server.generate_deepseek_copy({"folder_name":"26.1.12客户", "album":"管理合集", "location":"管理地点"})
            self.assertNotIn("26.1.12客户",call.call_args.args[1])
            self.assertNotIn("管理合集",call.call_args.args[1])

    def test_template_matching_rejects_ambiguous_and_unrelated_rows(self):
        rows=[{"idx":0,"name":"写真"},{"idx":4,"name":"艺考照"}]
        self.assertEqual(form._pick_template(rows,"艺考照")["idx"],4)
        self.assertIsNone(form._pick_template(rows,"证件照"))
        self.assertIsNone(form._pick_template(rows+[{"idx":5,"name":"艺考录制"}],"艺考"))

    def test_template_always_closes_and_clicks_matched_row(self):
        actions=[]
        def state(page,action="list",target=-1):
            actions.append((action,target))
            return {"found":True,"rows":[{"idx":3,"name":"艺考照"}],"applied":True}
        with patch.object(form,"_click_topic_template_entry",return_value=True), patch.object(form,"_template_state",side_effect=state), patch.object(form,"_close_topic_template") as close:
            self.assertTrue(form._apply_topic_template_best_effort(Mock(),"艺考照"))
            self.assertIn(("apply",3),actions)
            close.assert_called_once()
        with patch.object(form,"_click_topic_template_entry",return_value=True), patch.object(form,"_template_state",return_value={"found":True,"rows":[]}), patch.object(form,"_close_topic_template") as close:
            self.assertFalse(form._apply_topic_template_best_effort(Mock(),"不存在"))
            close.assert_called_once()

    def test_template_close_failure_stops_instead_of_typing_behind_popup(self):
        with patch.object(form,"_click_topic_template_entry",return_value=False), patch.object(form,"_close_topic_template",side_effect=form.PublishError("弹窗未关闭")):
            with self.assertRaisesRegex(form.PublishError,"未关闭"):
                form._apply_topic_template_best_effort(Mock(),"模板")

    def test_topic_is_sent_in_one_bridge_call(self):
        page=Mock(); page.evaluate.return_value=True
        form._input_single_tag(page,".ql-editor","演员资料照")
        page.type_text.assert_called_once_with("#演员资料照",delay_ms=100)

    def test_publish_mode_only_changes_switch_when_needed(self):
        page = Mock()
        page.evaluate.side_effect = [True, True, False]
        with patch.object(form, "sleep_random"):
            form._set_schedule_enabled(page, True)
            page.click_element.assert_not_called()
            form._set_schedule_enabled(page, False)
        page.click_element.assert_called_once_with(form.SCHEDULE_SWITCH)

    def test_publish_mode_stops_if_switch_cannot_be_confirmed(self):
        page = Mock(); page.evaluate.return_value = None
        with self.assertRaisesRegex(form.PublishError, "无法确认"):
            form._set_schedule_enabled(page, True)


if __name__ == "__main__":
    unittest.main()
