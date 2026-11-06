from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server


class ScheduleAndProfileRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(server, "DB_PATH", Path(self.temp.name) / "test.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        server.init_db()

    def _scheduled_payload(self, schedule_time: str = "") -> dict:
        return {
            "profile_dir": "Default",
            "publish_mode": "scheduled",
            "schedule_window": "23:00",
            "schedule_time": schedule_time,
            "title": "排期校验测试",
            "content": "正文",
            "images": ["/tmp/test-image.jpg"],
            "tags": [],
        }

    def test_fixed_window_applies_six_minute_buffer_today_but_not_tomorrow(self) -> None:
        planned = server.resolve_planned_schedule_time(
            "21:00",
            profile_dir="Default",
            now_ref=datetime(2026, 5, 15, 20, 55),
        )
        self.assertEqual(planned, "2026-05-16T21:00")

    def test_profile_context_defaults_to_active_and_rejects_stale_payload(self) -> None:
        server.db_set_setting("active_chrome_profile", "Profile 1")
        self.assertEqual(server.require_active_profile({}), "Profile 1")
        self.assertEqual(server.require_active_profile({"profile_dir": "Profile 1"}), "Profile 1")
        with self.assertRaisesRegex(server.PublishError, "账号已切换"):
            server.require_active_profile({"profile_dir": "Default"})

    def test_prepare_publish_rejects_manual_past_time_before_cli(self) -> None:
        payload = self._scheduled_payload("2000-01-01T08:00")
        with patch.object(server, "run_xhs_cli") as cli:
            with self.assertRaisesRegex(server.PublishError, "至少晚于当前时间 5 分钟"):
                server.prepare_publish(payload)
        cli.assert_not_called()

    def test_prepare_publish_only_replans_an_explicit_auto_suggestion(self) -> None:
        payload = self._scheduled_payload("2000-01-01T08:00")
        payload["schedule_auto"] = True
        result = {"ok": True, "stdout": "", "stderr": "", "json": {}}
        with patch.object(server, "run_xhs_cli", return_value=result), patch.object(
            server.imagefx, "apply_for_publish", return_value=(payload["images"], [])
        ), patch.object(server, "prepare_images_for_xhs", return_value=(payload["images"], [])):
            prepared = server.prepare_publish(payload)
        planned = datetime.fromisoformat(prepared["payload"]["schedule_time"])
        self.assertGreater(planned, datetime.now() + timedelta(minutes=5))
        self.assertEqual(planned.hour, 23)

    def test_prepare_publish_excludes_its_queue_reservation_from_limit(self) -> None:
        server.db_set_setting("daily_limit", 1)
        future = (datetime.now() + timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
        payload = self._scheduled_payload(future.isoformat(timespec="minutes"))
        job = server.enqueue_job(payload, payload["schedule_time"])
        result = {"ok": True, "stdout": "", "stderr": "", "json": {}}
        with patch.object(server, "run_xhs_cli", return_value=result), patch.object(
            server.imagefx, "apply_for_publish", return_value=(payload["images"], [])
        ), patch.object(server, "prepare_images_for_xhs", return_value=(payload["images"], [])):
            prepared = server.prepare_publish(payload, reservation_job_id=job["id"])
        self.assertEqual(prepared["payload"]["schedule_time"], future.isoformat(timespec="minutes"))


if __name__ == "__main__":
    unittest.main()
