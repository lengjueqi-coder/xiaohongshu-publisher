from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

import server
import imagefx
import mac_notifications as notifications
import publish_checks
from automation_engine import AutomationEngine


class ContinuousPublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patches = [patch.object(server, "DB_PATH", self.root / "test.sqlite3"),
                        patch.object(server, "APP_ROOT", self.root),
                        patch.object(server, "PREPARED_DIR", self.root / "prepared"),
                        patch.object(server, "chrome_profiles", return_value=[{"dir":"Default","label":"A"},{"dir":"Profile 1","label":"B"}])]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        server.init_db()
        self.engine = AutomationEngine(server)
        p = patch.object(server, "AUTOMATION", self.engine)
        p.start(); self.addCleanup(p.stop)
        self.materials = self.root / "materials"
        self.materials.mkdir()
        for group in ("A", "B", "C", "D", "E"):
            folder = self.materials / "2026" / "11月" / group
            folder.mkdir(parents=True)
            for index in range(4):
                Image.new("RGB", (90,120), (30+index*40,100,150)).save(folder/f"{index:02d}.jpg")

    def save(self, **overrides):
        return self.engine.save_plan("Default", {"root_path":str(self.materials), "min_images":4,"max_images":4,**overrides})

    def bind(self):
        identity = {"status":"verified","user_id":"user-aaaa","nickname":"测试账号","logged_in":True}
        with patch.object(server, "read_connected_identity", return_value=identity):
            self.engine.bind_identity("Default")

    def test_text_watermark_published_and_output_names_do_not_collide(self):
        a = self.materials/"2026/11月/A/00.jpg"
        b = self.materials/"2026/11月/B/00.jpg"
        params = {**imagefx.DEFAULT_PARAMS, "watermark_enabled":True,"watermark_text":"TEST"}
        paths, notes = imagefx.apply_for_publish([str(a),str(b)],params,self.root/"rendered")
        self.assertFalse(notes)
        self.assertEqual(len(set(paths)), 2)
        self.assertIsNotNone(ImageChops.difference(Image.open(a).convert("RGB"),Image.open(paths[0]).convert("RGB")).getbbox())

    def test_scanner_ignores_own_generated_outputs(self):
        folder = self.materials/"2026/11月/A"
        (folder/"AI封面").mkdir()
        Image.new("RGB",(20,20)).save(folder/"AI封面/cover.jpg")
        Image.new("RGB",(20,20)).save(folder/"四宫格_20261105.jpg")
        folders=server.scan_folders(str(self.materials))
        self.assertEqual(len(folders),5)
        self.assertTrue(all(len(f.images)==4 for f in folders))

    def test_account_settings_do_not_leak_between_profiles(self):
        server.db_set_setting("watermark_text","A 的水印")
        server.db_set_setting("default_album","A 合集")
        server.db_set_setting("active_chrome_profile","Profile 1")
        self.assertEqual(server.db_get_setting("watermark_text",""),"")
        server.db_set_setting("watermark_text","B 的水印")
        self.assertEqual(server.account_setting("watermark_text",profile_dir="Default"),"A 的水印")
        self.assertEqual(server.account_setting("watermark_text",profile_dir="Profile 1"),"B 的水印")

    def test_freeze_survives_source_and_settings_changes_and_detects_tampering(self):
        source = self.materials/"2026/11月/A/00.jpg"
        frozen=server.freeze_payload({"images":[str(source)],"profile_dir":"Default","fx_params":{"watermark_enabled":True,"watermark_text":"固定"}})
        original_bytes=Path(frozen["images"][0]).read_bytes()
        Image.new("RGB",(90,120),"red").save(source)
        server.db_set_setting("watermark_text","新水印")
        server.verify_frozen_images(frozen)
        self.assertEqual(Path(frozen["images"][0]).read_bytes(),original_bytes)
        Path(frozen["images"][0]).write_bytes(b"changed")
        with self.assertRaisesRegex(server.PublishError,"改动"):
            server.verify_frozen_images(frozen)

    def test_review_batch_creates_complete_immutable_drafts_and_does_not_reconsume(self):
        self.save(layout="2x2",copy_mode="library",copy_library=[{"title":"成套标题","content":"与标题配套的正文","tags":["摄影"]}])
        ids=self.engine.build_batch("Default",3)
        self.assertEqual(len(ids),3)
        jobs=server.queue_rows()
        self.assertTrue(all(j["status"]=="draft" for j in jobs))
        self.assertTrue(all(j["payload"]["title"]=="成套标题" for j in jobs))
        self.assertTrue(all(len(j["payload"]["images"])==5 for j in jobs))
        self.assertIsNone(server.claim_next_job())
        for job in jobs:
            server.verify_frozen_images(job["payload"])
        more=self.engine.build_batch("Default",3)
        paths=[j["payload"]["folder_path"] for j in server.queue_rows()]
        self.assertEqual(len(paths),len(set(paths)))
        self.assertLessEqual(len(more),1)

    def test_automatic_mode_requires_binding_and_queues_only_after_enable(self):
        self.save(mode="automatic")
        with self.assertRaisesRegex(ValueError,"绑定"):
            self.engine.set_enabled("Default",True)
        self.bind(); self.engine.set_enabled("Default",True)
        self.engine.build_batch("Default",1)
        self.assertEqual(server.queue_rows()[0]["status"],"queued")
        self.engine.set_enabled("Default",False)
        self.assertEqual(server.queue_rows()[0]["status"],"paused")

    def test_ai_failure_does_not_silently_publish_template(self):
        self.save(copy_mode="ai")
        with patch.object(server,"generate_deepseek_copy",return_value=None):
            with self.assertRaisesRegex(ValueError,"AI 文案"):
                self.engine.build_batch("Default",1)
        self.assertFalse(server.queue_rows())

    def test_too_few_images_do_not_silently_violate_saved_rule(self):
        self.save(min_images=5,max_images=6)
        with self.assertRaisesRegex(ValueError,"少于规则"):
            self.engine.build_batch("Default",1)
        self.assertFalse(server.queue_rows())

    def test_collage_rule_requires_enough_images_to_fill_grid(self):
        for layout,needed in (("2x2",4),("collage_only",4),("3x3",9)):
            with self.assertRaisesRegex(ValueError,"避免拼图留空"):
                self.save(layout=layout,min_images=1,max_images=9)
            self.save(layout=layout,min_images=needed,max_images=9)

    def test_approval_is_account_scoped_and_cancel_releases_progress(self):
        self.save(); self.bind()
        ids=self.engine.build_batch("Default",1)
        self.engine.approve("Default",ids)
        self.assertEqual(server.queue_rows()[0]["status"],"queued")
        with self.assertRaises(ValueError):
            self.engine.cancel("Profile 1",ids[0])
        self.engine.cancel("Default",ids[0])
        with server.db_connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM automation_materials").fetchone()[0],0)

    def test_plan_asset_is_copied_and_not_changed_by_later_upload(self):
        logo=self.root/"logo.png";Image.new("RGBA",(20,20),"white").save(logo)
        plan=self.save(fx_params={"logo_enabled":True,"logo_path":str(logo)})
        saved=Path(plan["config"]["fx_params"]["logo_path"])
        self.assertNotEqual(saved,logo)
        data=saved.read_bytes();Image.new("RGBA",(20,20),"red").save(logo)
        self.assertEqual(saved.read_bytes(),data)

    def test_submission_checks_identity_and_form_before_click(self):
        self.save();self.bind()
        self.engine.build_batch("Default",1)
        job=server.queue_rows()[0]
        job["payload"]["strict_fields"]=True
        with patch.object(server,"switch_chrome_profile"),patch.object(server,"read_connected_identity",return_value={"status":"verified","user_id":"WRONG"}),patch.object(server,"click_publish") as click:
            self.assertFalse(server.process_queue_job(job))
        click.assert_not_called()
        self.assertEqual(server.queue_rows()[0]["status"],"failed")
        self.assertEqual(self.engine.get_plan("Default")["state"],"attention")
        self.assertEqual(len(notifications.alerts(server)),1)

    def test_unknown_submit_pauses_and_is_never_retried_automatically(self):
        self.save(); self.bind(); self.engine.build_batch("Default",1)
        job=server.queue_rows()[0]
        with patch.object(server,"switch_chrome_profile"),patch.object(server,"check_task_identity"),patch.object(server,"prepare_publish",return_value={"history_id":1}),patch.object(publish_checks,"verify"),patch.object(server,"click_publish",return_value={"ok":False,"message":"未收到确认","result":{}}):
            self.assertFalse(server.process_queue_job(job))
        self.assertEqual(server.queue_rows()[0]["status"],"unknown")
        self.assertIsNone(server.claim_next_job())
        with self.assertRaisesRegex(ValueError,"状态不明"):
            self.engine.set_enabled("Default",True)

    def test_whole_browser_transaction_is_locked(self):
        events=[]
        class Lock:
            def __enter__(self):events.append("lock")
            def __exit__(self,*a):events.append("unlock")
        with patch.object(server,"BROWSER_LOCK",Lock()),patch.object(server,"_process_queue_job",side_effect=lambda j:events.append("process") or True):
            server.process_queue_job({})
        self.assertEqual(events,["lock","process","unlock"])

    def test_submission_frequency_defers_instead_of_losing_job(self):
        server.enqueue_job({"title":"先前","content":"正文","images":["x"],"publish_mode":"immediate"},"")
        server.update_job(1,status="submitted",started_at=server.now_iso())
        server.enqueue_job({"title":"下一条","content":"正文","images":["x"],"publish_mode":"immediate","submission_hour_limit":1},"")
        job=server.claim_next_job()
        self.assertTrue(server.defer_for_rate_limit(job))
        self.assertIsNone(server.claim_next_job())
        self.assertEqual(server.queue_rows()[1]["status"],"queued")

    def test_restart_preserves_drafts_but_pauses_inflight(self):
        self.save();self.bind();self.engine.build_batch("Default",2)
        jobs=server.queue_rows();server.update_job(jobs[0]["id"],status="submitting")
        server.init_db()
        self.assertEqual([j["status"] for j in server.queue_rows()],["unknown","draft"])
        self.assertFalse(server.db_get_setting("queue_enabled"))
        self.assertTrue(notifications.alerts(server))

    def test_repeated_alert_count_and_acknowledgement_persist(self):
        notifications.save_settings(server,{"repeat":"count","repeat_count":3,"interval_minutes":10})
        aid=notifications.add_alert(server,"Default","Default","test","需要处理")
        with patch.object(notifications,"deliver") as deliver:
            notifications.pump(server,now=100)
            notifications.pump(server,now=101)
            notifications.pump(server,now=701)
            notifications.pump(server,now=1302)
            notifications.pump(server,now=2000)
            self.assertEqual(deliver.call_count,3)
            second=notifications.add_alert(server,"Profile 1","Profile 1","test","另一事项")
            notifications.pump(server,now=2001)
            self.assertEqual(deliver.call_count,4)  # exhausted alert cannot starve other alerts
            notifications.acknowledge(server,second)
            notifications.pump(server,now=3000)
            self.assertEqual(deliver.call_count,4)
        self.assertEqual(notifications.alerts(server)[1]["id"],aid)

    def test_hourly_alert_and_failed_delivery_retry_without_losing_incident(self):
        notifications.save_settings(server,{"repeat":"interval","interval_minutes":60})
        notifications.add_alert(server,"Default","Default","test","需要处理")
        with patch.object(notifications,"deliver",side_effect=RuntimeError("设备不可用")):
            notifications.pump(server,now=100)
        self.assertIn("设备不可用",notifications.alerts(server)[0]["delivery_error"])
        with patch.object(notifications,"deliver") as deliver:
            notifications.pump(server,now=200)
            notifications.pump(server,now=3799)
            self.assertEqual(deliver.call_count,1)
            notifications.pump(server,now=3800)
            self.assertEqual(deliver.call_count,2)

    def test_receipt_distinguishes_review_rejection_and_publication(self):
        payload={"title":"测试标题","schedule_time":"2026-11-05T21:00"}
        base="测试标题\n2026-11-05 21:00\n"
        for detail,status in (("审核中","reviewing"),("审核不通过","rejected"),("浏览\n0\n点赞\n0","published")):
            self.assertEqual(publish_checks.match_receipt([{"text":base+detail}],payload)["status"],status)
        self.assertEqual(publish_checks.match_receipt([{"text":base+"浏览 点赞"}]*2,payload)["status"],"unconfirmed")

    def test_required_field_verification_fails_for_private_and_original_mismatch(self):
        payload={"title":"标题","content":"正文","images":["a"],"visibility":"仅自己可见","original":True,"tags":["摄影"]}
        state={"title":"标题","content":"正文 #摄影","image_count":1,"visibility":"仅自己可见","original":True,"rows":[],"risky":False}
        publish_checks.validate_state(state,payload)
        for key,value in (("visibility","公开可见"),("original",False),("risky",True),("image_count",0)):
            with self.assertRaises(ValueError):
                publish_checks.validate_state({**state,key:value},payload)

    def test_active_plan_blocks_updates(self):
        self.save();self.bind();self.engine.set_enabled("Default",True)
        self.assertIn("自动发布",server.update_install_block_reason())

    def test_unsaved_plan_cannot_pretend_identity_binding_succeeded(self):
        with patch.object(server,"read_connected_identity",return_value={"status":"verified","user_id":"test-uid","nickname":"测试"}):
            with self.assertRaisesRegex(ValueError,"先保存"):
                self.engine.bind_identity("Default")

    def test_three_sounds_stop_immediately_after_acknowledgement(self):
        aid=notifications.add_alert(server,"Default","Default","test","需要处理")
        def acknowledge_during_first_sound(*args):
            notifications.acknowledge(server,aid)
        with patch.object(notifications,"sys") as system,patch.object(notifications.subprocess,"run"),patch.object(notifications.time,"sleep"),patch.object(notifications,"play_audio",side_effect=acknowledge_during_first_sound) as audio:
            system.platform="darwin"
            notifications.deliver(server,{"id":aid,"message":"提醒","profile_dir":"Default"},{**notifications.DEFAULTS,"burst_count":3})
        self.assertEqual(audio.call_count,1)

    def test_custom_recording_is_copied_and_reimport_is_safe(self):
        source=self.root/"my-voice.wav";source.write_bytes(b"RIFF-local-test")
        config=notifications.import_audio(server,source)
        saved=Path(config["custom_audio"])
        self.assertNotEqual(saved,source)
        self.assertEqual(saved.read_bytes(),source.read_bytes())
        self.assertEqual(notifications.import_audio(server,saved)["custom_audio"],str(saved))
        with self.assertRaises(ValueError):
            notifications.import_audio(server,self.root/"missing.exe")

    def test_timezone_mismatch_stops_scheduled_submission(self):
        payload={"title":"标题","content":"正文","images":["a"],"original":False,"schedule_time":"2026-11-06T21:00","timezone":"Asia/Shanghai","visibility":"仅自己可见"}
        state={"title":"标题","content":"正文","image_count":1,"original":False,"visibility":"仅自己可见","schedule":"2026-11-06 21:00","schedule_on":True,"timezone":"America/Los_Angeles"}
        with self.assertRaisesRegex(ValueError,"时区"):
            publish_checks.validate_state(state,payload)
        publish_checks.validate_state({**state,"timezone":"Asia/Shanghai"},payload)

    def test_failed_multi_approval_rolls_back_entire_selection(self):
        self.save();self.bind();ids=self.engine.build_batch("Default",2)
        self.engine.cancel("Default",ids[1])
        with self.assertRaises(ValueError):
            self.engine.approve("Default",ids)
        self.assertEqual(server.queue_rows()[0]["status"],"draft")

    def test_pause_only_affects_this_accounts_automatic_tasks(self):
        self.save(mode="automatic");self.bind();self.engine.set_enabled("Default",True)
        self.engine.build_batch("Default",1)
        manual=server.enqueue_job({"profile_dir":"Default","title":"人工任务","publish_mode":"immediate"},"")
        other=server.enqueue_job({"profile_dir":"Profile 1","title":"另一个账号","automation":True,"publish_mode":"immediate"},"")
        self.engine.incident("Default","账号异常")
        statuses={j["id"]:j["status"] for j in server.queue_rows()}
        self.assertEqual(statuses[1],"paused")
        self.assertEqual(statuses[manual["id"]],"queued")
        self.assertEqual(statuses[other["id"]],"queued")

    def test_past_scheduled_task_fails_before_browser_operations(self):
        payload={"strict_fields":True,"schedule_epoch":time.time()-100,"automation":True,"profile_dir":"Default"}
        job=server.enqueue_job({**payload,"title":"过期","content":"正文","images":["x"]},"2000-01-01T12:00")
        with patch.object(server,"switch_chrome_profile") as switch:
            self.assertFalse(server.process_queue_job(job))
        switch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
