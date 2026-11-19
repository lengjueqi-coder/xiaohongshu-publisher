"""Account-scoped publishing plans and durable, bounded task production."""
from __future__ import annotations

import hashlib
import json
import random
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from publishing_policy import PLAN_LEAD_MINUTES, require_schedule_lead, schedule_epoch

import imagefx
import mac_notifications as notifications
import copy_quality


DEFAULT_PLAN = {
    "root_path": "", "mode": "review", "timezone": "Asia/Shanghai", "daily_count": 1,
    "windows": ["21:00-22:00"], "ahead_days": 3, "min_images": 4, "max_images": 8,
    "selection": "ordered", "layout": "original", "copy_mode": "local",
    "styles": ["praise", "professional", "process", "opinion", "story", "painpoint", "knowledge", "advantage"],
    "copy_library": [], "allow_template_fallback": False, "tags": [], "mentions": [],
    "album": "", "location": "", "topic_template": "", "original": True,
    "visibility": "公开可见", "expected_user_id": "", "account_name": "",
    "fx_params": {}, "submission_hour_limit": 3, "submission_day_limit": 12,
    "window_mode": "random", "fixed_window": "",
}


def init_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS automation_plans (
        profile_dir TEXT PRIMARY KEY, config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'paused', last_error TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL, last_scan_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS automation_materials (
        profile_dir TEXT NOT NULL, folder_path TEXT NOT NULL, job_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(profile_dir,folder_path))""")
    notifications.init_tables(db)
    db.execute("""CREATE TABLE IF NOT EXISTS automation_receipts (
        job_id INTEGER PRIMARY KEY, next_check REAL NOT NULL DEFAULT 0, receipt TEXT NOT NULL DEFAULT '{}')""")


class AutomationEngine:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.alert_thread = None
        self.last_storage_alert = 0
        self.cached_notification_settings = notifications.settings(app)

    def storage_failure(self, exc):
        # The database itself may live on an unplugged external disk. This fallback
        # must not depend on that database or its audio cache to warn the operator.
        now = time.monotonic()
        if self.last_storage_alert and now - self.last_storage_alert < 3600:
            return
        self.last_storage_alert = now
        print(f"自动发布存储暂不可用：{exc}", file=__import__("sys").stderr, flush=True)
        if self.cached_notification_settings["enabled"]:
            try:
                notifications.deliver(self.app, {"profile_dir": "本机后台", "message": "自动发布无法读取本地存储，请检查素材磁盘和软件状态。"},
                                      {**self.cached_notification_settings, "audio": "silent" if self.cached_notification_settings["audio"] == "silent" else "sound", "burst_count": 1})
            except Exception as notify_exc:
                print(f"存储异常的 Mac 提醒也失败：{notify_exc}", file=__import__("sys").stderr, flush=True)

    def get_plan(self, profile):
        with self.app.db_connection() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM automation_plans WHERE profile_dir=?", (profile,)).fetchone()
        if row:
            result = dict(row)
            result["config"] = {**DEFAULT_PLAN, **json.loads(result["config"])}
            return result
        config = {**DEFAULT_PLAN, "root_path": self.app.account_setting("root_path", "", profile),
                  "visibility": self.app.account_setting("visibility", "公开可见", profile),
                  "fx_params": self.app.get_fx_params(profile_dir=profile),
                  "album": self.app.account_setting("default_album", "", profile),
                  "location": self.app.account_setting("default_location", "", profile),
                  "topic_template": self.app.account_setting("last_local_topic_template", "", profile),
                  "tags": self.app.account_setting("last_tags", [], profile),
                  "original": self.app.account_setting("original_enabled", True, profile)}
        return {"profile_dir": profile, "config": config, "enabled": False, "state": "paused", "last_error": ""}

    def save_plan(self, profile, raw):
        with self.lock:
            current = self.get_plan(profile)
            if current["enabled"]:
                raise ValueError("请先暂停计划再修改规则，已生成的任务保持原设置")
            config = {**current["config"], **{k: v for k, v in raw.items() if k in DEFAULT_PLAN}}
            if profile not in {p["dir"] for p in self.app.chrome_profiles()}:
                raise ValueError("找不到该 Chrome 用户资料")
            root = Path(config["root_path"]).expanduser().resolve()
            if not root.is_dir():
                raise ValueError("素材目录不存在，请选择可用的文件夹")
            config["root_path"] = str(root)
            ZoneInfo(config["timezone"])
            if config["mode"] not in {"review", "automatic"} or config["layout"] not in {"original", "2x2", "3x3", "collage_only"}:
                raise ValueError("运行模式或拼图方式无效")
            if config["copy_mode"] not in {"local", "ai", "library"} or config["selection"] not in {"ordered", "random"}:
                raise ValueError("选图或文案方式无效")
            for key, lower, upper in (("daily_count", 1, 10), ("ahead_days", 1, 7), ("min_images", 1, 18), ("max_images", 1, 18), ("submission_hour_limit", 1, 10), ("submission_day_limit", 1, 50)):
                config[key] = int(config[key])
                if not lower <= config[key] <= upper:
                    raise ValueError(f"{key} 必须在 {lower}–{upper} 之间")
            if config["min_images"] > config["max_images"]:
                raise ValueError("最少选图数量不能超过最多数量")
            if config["mode"] == "automatic" and config["daily_count"] > 2:
                raise ValueError("全自动发布每天最多 2 条，请调低每天发布数量")
            needed = 9 if config["layout"] == "3x3" else 4 if config["layout"] in {"2x2", "collage_only"} else 1
            if config["min_images"] < needed:
                raise ValueError(f"当前拼图要求至少选 {needed} 张，请调高最少选图数量，避免拼图留空")
            common_windows = self.app.account_setting("schedule_windows", [], profile)
            if not isinstance(common_windows, list) or not common_windows:
                raise ValueError("请先在账号画像下方设置常用发布时间段")
            common_windows = [str(item).strip() for item in common_windows if str(item).strip()]
            if not common_windows:
                raise ValueError("请先在账号画像下方设置常用发布时间段")
            config["window_mode"] = str(config.get("window_mode") or "random").strip()
            if config["window_mode"] not in {"random", "fixed"}:
                raise ValueError("每天使用时间段的方式无效")
            if config["window_mode"] == "fixed":
                fixed_window = str(config.get("fixed_window") or "").strip()
                if fixed_window not in common_windows:
                    raise ValueError("固定时间段必须来自账号常用发布时间段")
                config["fixed_window"] = fixed_window
                config["windows"] = [fixed_window]
            else:
                config["fixed_window"] = ""
                config["windows"] = common_windows
            if len(config["windows"]) > 12:
                raise ValueError("最多设置 12 个常用发布时间段")
            for window in config["windows"]:
                self.app.parse_schedule_window(str(window))
            if not config["styles"] or any(s not in self.app.COPY_STYLES for s in config["styles"]):
                raise ValueError("请选择有效的文案风格")
            if config["visibility"] not in {"公开可见", "仅自己可见", "仅互关好友可见"}:
                raise ValueError("可见范围无效")
            if not isinstance(config["copy_library"], list) or len(config["copy_library"]) > 200:
                raise ValueError("文案库格式不正确，最多保存 200 组")
            if config["copy_mode"] == "library" and not config["copy_library"]:
                raise ValueError("请先添加配套标题、正文和话题的文案库")
            for entry in config["copy_library"]:
                self.validate_copy(entry)
            config["tags"] = [str(t).strip().lstrip("#") for t in config["tags"] if str(t).strip()][:10]
            config["mentions"] = [str(t).strip() for t in config["mentions"] if str(t).strip()][:10]
            # Identity may only be bound from a verified live read, not client text.
            config["expected_user_id"] = current["config"]["expected_user_id"]
            config["account_name"] = current["config"]["account_name"]
            config["fx_params"] = self.app.get_fx_params(config.get("fx_params"), profile_dir=profile)
            config["fx_params"]["output_mode"] = "merge"
            # Plan assets remain stable even if the account uploads another logo later.
            for key in ("logo_path", "watermark_path"):
                source = Path(config["fx_params"].get(key) or "")
                if source.is_file():
                    digest = hashlib.sha256(source.read_bytes()).hexdigest()
                    target = self.app.APP_ROOT / "data" / "plan-assets" / (digest + source.suffix.lower())
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.resolve() != target.resolve():
                        shutil.copy2(source, target)
                    config["fx_params"][key] = str(target)
            with self.app.db_connection() as db:
                db.execute("""INSERT INTO automation_plans(profile_dir,config,updated_at) VALUES(?,?,?)
                    ON CONFLICT(profile_dir) DO UPDATE SET config=excluded.config,updated_at=excluded.updated_at""",
                           (profile, json.dumps(config, ensure_ascii=False), self.app.now_iso()))
            return self.get_plan(profile)

    @staticmethod
    def validate_copy(copy):
        if not isinstance(copy, dict) or not str(copy.get("title", "")).strip() or not str(copy.get("content", "")).strip():
            raise ValueError("文案必须包含配套的标题和正文")
        # The platform's own length check still runs before submission.
        if not isinstance(copy["title"], str) or not isinstance(copy["content"], str) or not isinstance(copy.get("tags", []), list):
            raise ValueError("标题、正文和话题格式不正确")
        if len(copy["title"]) > 20 or len(copy["content"]) > 1000:
            raise ValueError("标题最多 20 字、正文最多 1000 字")

    def bind_identity(self, profile):
        with self.lock, self.app.BROWSER_LOCK:
            if self.get_plan(profile)["enabled"]:
                raise ValueError("请先暂停计划")
            if profile != self.app.active_profile_dir():
                raise ValueError("请先切换到要绑定的运营账号")
            identity = self.app.read_connected_identity(self.app.XHS_SKILL_DIR)
            if identity.get("status") != "verified" or not identity.get("user_id"):
                raise ValueError("尚未读取到小红书用户 ID，请打开该账号已登录的小红书主页后重试")
            plan = self.get_plan(profile)
            config = {**plan["config"], "expected_user_id": identity["user_id"], "account_name": identity["nickname"]}
            with self.app.db_connection() as db:
                saved = db.execute("UPDATE automation_plans SET config=?,updated_at=? WHERE profile_dir=?", (json.dumps(config, ensure_ascii=False), self.app.now_iso(), profile))
                if not saved.rowcount:
                    raise ValueError("请先保存规则，再绑定当前小红书账号")
            self.app.db_set_setting(f"account_identity:{profile}", {**identity, "checked_at": self.app.now_iso()})
            return self.get_plan(profile)

    def set_enabled(self, profile, enabled):
        with self.lock:
            plan = self.get_plan(profile)
            config = plan["config"]
            if enabled:
                with self.app.db_connection() as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='distribution_plans'").fetchone():
                        other=db.execute('SELECT config FROM distribution_plans WHERE profile=?',(profile,)).fetchone()
                        if other:
                            distribution=json.loads(other[0])
                            if distribution.get('enabled') and 'xhs' in distribution.get('targets',[]):raise ValueError('请先暂停多平台每日计划，避免两个计划重复发布小红书')
                if not plan["config"].get("expected_user_id"):
                    raise ValueError("请先保存计划并绑定当前小红书账号")
                if not Path(plan["config"]["root_path"]).is_dir():
                    raise ValueError("素材磁盘或目录不可用")
                common = self.app.account_setting("schedule_windows", [], profile)
                common = [str(item).strip() for item in common if str(item).strip()] if isinstance(common, list) else []
                if not common:
                    raise ValueError("请先在账号画像下方设置常用发布时间段")
                if config.get("window_mode") == "fixed":
                    fixed = str(config.get("fixed_window") or "").strip()
                    if fixed not in common:
                        raise ValueError("固定时间段必须来自账号常用发布时间段")
                    config["windows"], config["fixed_window"] = [fixed], fixed
                else:
                    config["windows"], config["fixed_window"] = common, ""
                with self.app.db_connection() as db:
                    uncertain = db.execute("SELECT COUNT(*) FROM publish_jobs WHERE profile_dir=? AND status='unknown'", (profile,)).fetchone()[0]
                if uncertain:
                    raise ValueError("该账号存在状态不明的任务，请先核对处理，避免重复发布")
            with self.app.db_connection() as db:
                db.execute('BEGIN IMMEDIATE')
                if enabled and db.execute("SELECT 1 FROM sqlite_master WHERE name='distribution_plans'").fetchone():
                    other=db.execute('SELECT config FROM distribution_plans WHERE profile=?',(profile,)).fetchone()
                    if other and json.loads(other[0]).get('enabled') and 'xhs' in json.loads(other[0]).get('targets',[]):raise ValueError('请先暂停多平台每日计划')
                db.execute("UPDATE automation_plans SET config=?,enabled=?,state=?,last_error='',updated_at=? WHERE profile_dir=?", (json.dumps(config, ensure_ascii=False), int(enabled), "running" if enabled else "paused", self.app.now_iso(), profile))
                # Manual tasks are never enabled or paused by an automation plan.
                db.execute("UPDATE publish_jobs SET status=?,updated_at=? WHERE profile_dir=? AND status=? AND json_extract(payload,'$.automation')=1",
                           ("queued" if enabled else "paused", self.app.now_iso(), profile, "paused" if enabled else "queued"))
            if enabled:
                self.app.db_set_setting("queue_enabled", True)
                self.app.QUEUE_WAKE.set()
            return self.get_plan(profile)

    def incident(self, profile, message, kind="publish_error"):
        with self.app.db_connection() as db:
            db.execute("UPDATE automation_plans SET enabled=0,state='attention',last_error=?,updated_at=? WHERE profile_dir=?", (str(message)[:1000], self.app.now_iso(), profile))
            db.execute("UPDATE publish_jobs SET status='paused',updated_at=? WHERE profile_dir=? AND status='queued' AND json_extract(payload,'$.automation')=1", (self.app.now_iso(), profile))
        notifications.add_alert(self.app, profile, profile, kind, message)

    def slot(self, profile, config, now):
        zone = ZoneInfo(config["timezone"])
        local_now = now.astimezone(zone)
        with self.app.db_connection() as db:
            rows = db.execute("SELECT schedule_time FROM publish_jobs WHERE profile_dir=? AND status NOT IN ('cancelled','failed')", (profile,)).fetchall()
            used = [r[0] for r in rows if r[0]]
            # Manual history which has no matching queued job also reserves capacity.
            used += [r[0] for r in db.execute("""SELECT h.schedule_time FROM publish_history h WHERE h.profile_dir=?
                AND h.status IN ('published','scheduled','accepted','reviewing') AND h.schedule_time!=''
                AND NOT EXISTS(SELECT 1 FROM publish_jobs j WHERE j.history_id=h.id)""", (profile,))]
        for offset in range(config["ahead_days"] + 1):
            day = local_now.date() + timedelta(days=offset)
            count = sum(s[:10] == day.isoformat() for s in used)
            if count >= config["daily_count"]:
                continue
            candidates = []
            for window in config["windows"]:
                parsed = self.app.parse_schedule_window(window)
                start, end = parsed["start"], parsed["end"]
                if parsed["kind"] == "fixed":
                    minutes = [start]
                else:
                    if end <= start:
                        end += 1440
                    minutes = list(range(start, end))
                    random.shuffle(minutes)
                for minute in minutes:
                    dt = datetime.combine(day, datetime.min.time(), tzinfo=zone) + timedelta(minutes=minute)
                    if dt.date() != day:  # spillover is counted on its actual calendar day
                        if sum(s[:10] == dt.date().isoformat() for s in used) >= config["daily_count"]:
                            continue
                    if dt >= local_now + timedelta(minutes=PLAN_LEAD_MINUTES) and dt.strftime("%Y-%m-%dT%H:%M") not in used:
                        candidates.append(dt)
                        break
            if candidates:
                return random.choice(candidates)
        return None

    def build_batch(self, profile, limit=10, now=None):
        now = now or datetime.now(timezone.utc)
        with self.lock:
            plan = self.get_plan(profile)
            config = plan["config"]
            if not config["root_path"]:
                raise ValueError("请先保存素材目录及发布规则")
            if config["mode"] == "automatic":
                config["daily_count"] = max(1, min(2, int(config["daily_count"] or 1)))
            with self.app.db_connection() as db:
                used = {r[0] for r in db.execute("SELECT folder_path FROM automation_materials WHERE profile_dir=?", (profile,))}
                used.update(r[0] for r in db.execute("SELECT json_extract(payload,'$.folder_path') FROM publish_jobs WHERE profile_dir=? AND status!='cancelled' AND json_extract(payload,'$.automation')=1", (profile,)))
            candidates = [f for f in self.app.scan_folders(config["root_path"]) if f.path not in used]
            created = []
            for folder in candidates[:max(1, min(30, int(limit)))]:
                slot = self.slot(profile, config, now)
                if not slot:
                    break
                payload = self.make_payload(profile, config, folder, slot)
                status = "queued" if plan["enabled"] and config["mode"] == "automatic" else "draft"
                job = self.app.enqueue_job(payload, payload["schedule_time"], status=status)
                with self.app.db_connection() as db:
                    db.execute("INSERT INTO automation_materials(profile_dir,folder_path,job_id,created_at) VALUES(?,?,?,?)", (profile, folder.path, job["id"], self.app.now_iso()))
                created.append(job["id"])
            with self.app.db_connection() as db:
                db.execute("UPDATE automation_plans SET last_scan_at=?,state=? WHERE profile_dir=?", (self.app.now_iso(), "running" if plan["enabled"] else "paused", profile))
            if not candidates and plan["enabled"]:
                notifications.add_alert(self.app, profile, profile, "materials_empty", "素材已用完，等待你补充新子文件夹；计划仍会定期扫描。")
            elif candidates:
                with self.app.db_connection() as db:
                    db.execute("UPDATE automation_alerts SET acknowledged_at=? WHERE profile_dir=? AND kind='materials_empty' AND acknowledged_at IS NULL", (self.app.now_iso(), profile))
            self.app.QUEUE_WAKE.set()
            return created

    def make_payload(self, profile, config, folder, slot):
        config = dict(config)
        # The saved plan is a snapshot; explicit empty fields and switches are authoritative.
        source_images = list(folder.images)
        if len(source_images) < config["min_images"]:
            raise ValueError(f"素材组“{folder.name}”只有 {len(source_images)} 张图片，少于规则要求的 {config['min_images']} 张")
        if config["selection"] == "random":
            random.shuffle(source_images)
        count = random.randint(config["min_images"], config["max_images"])
        selected = source_images[:count]
        if not selected:
            raise ValueError("素材组没有可用图片")
        task_dir = self.app.APP_ROOT / "data" / "tasks" / uuid.uuid4().hex
        task_dir.mkdir(parents=True)
        try:
            images = []
            for index, source in enumerate(selected):
                path = Path(source)
                target = task_dir / f"{index+1:02d}{path.suffix.lower()}"
                shutil.copy2(path, target)
                images.append(str(target))
            if config["layout"] != "original":
                layout = "2x2" if config["layout"] == "collage_only" else config["layout"]
                needed = 9 if layout == "3x3" else 4
                if len(images) < 2:
                    raise ValueError("拼图至少需要 2 张图，请调整规则或补充素材")
                collage = imagefx.make_collage(images[:needed], layout, 8, "#ffffff", task_dir / "cover.jpg")
                images = [str(collage)] if config["layout"] == "collage_only" else [str(collage), *images[:self.app.MAX_NOTE_IMAGES - 1]]
            self.app.validate_note_image_count(images, require_one=True)
            style = random.choice(config["styles"])
            recent = copy_quality.recent_copies(self.app, profile)
            if config["copy_mode"] == "library":
                copy = random.choice(config["copy_library"])
            else:
                args = {"style": style, "folder_name": folder.name, "model_name": folder.model_name,
                        "profile_dir": profile, "notes": copy_quality.material_notes(folder.path),
                        "recent_copies": recent[:20],
                        "copy_engine": "local" if config["copy_mode"] == "local" else "auto"}
                if config["copy_mode"] == "ai":
                    copy = self.app.generate_deepseek_copy(args)
                    if not copy and not config["allow_template_fallback"]:
                        raise ValueError("AI 文案生成失败，未允许回退模板，计划已暂停")
                    copy = copy or self.app.generate_template_copy(args)
                else:
                    copy = self.app.generate_template_copy(args)
            self.validate_copy(copy)
            # Review drafts remain editable; unattended publishing must stop on repetition.
            if config["mode"] == "automatic":
                copy_quality.ensure_distinct(copy, recent)
            tags = list(dict.fromkeys(config["tags"]))[:10]
            schedule_time = slot.strftime("%Y-%m-%dT%H:%M")
            schedule_epoch = slot.timestamp()
            if datetime.fromtimestamp(schedule_epoch, tz=slot.tzinfo).strftime("%Y-%m-%dT%H:%M") != schedule_time:
                raise ValueError("定时发布时间生成异常，请重新生成任务")
            payload = {"automation": True, "profile_dir": profile, "folder_path": folder.path,
                       "material_mode": "folder", "material_root": config["root_path"], "archive_source_images": selected,
                       "folder_name": folder.name, "images": images, "model_name": folder.model_name,
                       "style": style, "title": copy["title"], "content": copy["content"], "tags": tags,
                       "copy_engine": copy.get("engine", config["copy_mode"]), "ai_used": bool(copy.get("ai_used")),
                       "publish_mode": "scheduled", "schedule_time": schedule_time,
                       "schedule_epoch": schedule_epoch, "timezone": config["timezone"],
                       "submit_after": datetime.now().isoformat(timespec="seconds"),
                       "expected_user_id": config["expected_user_id"], "strict_fields": True,
                       "submission_hour_limit": config["submission_hour_limit"],
                       "submission_day_limit": config["submission_day_limit"],
                       **{k: config[k] for k in ("album", "location", "topic_template", "original", "visibility", "mentions")},
                       "fx_params": config["fx_params"]}
            return self.app.freeze_payload(payload, task_dir)
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise

    def approve(self, profile, ids):
        with self.lock:
            plan = self.get_plan(profile)
            expected_user_id = plan["config"]["expected_user_id"] or self.app.account_identity(profile).get("user_id")
            if not expected_user_id:
                raise ValueError("请先绑定当前运营账号")
            with self.app.db_connection() as db:
                for job_id in ids:
                    row = db.execute("SELECT payload,status FROM publish_jobs WHERE id=? AND profile_dir=?", (int(job_id), profile)).fetchone()
                    if not row or row[1] != "draft":
                        raise ValueError("所选任务已变化，请刷新后重试")
                    payload = json.loads(row[0])
                    require_schedule_lead(payload)
                    payload["expected_user_id"] = expected_user_id
                    db.execute("UPDATE publish_jobs SET payload=?,status='queued',updated_at=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), self.app.now_iso(), int(job_id)))
            self.app.db_set_setting("queue_enabled", True)
            self.app.QUEUE_WAKE.set()

    def apply_settings(self, profile):
        """把当前已保存的发布设置同步到尚未提交的任务。

        只改可见范围、合集、地点、话题模板、原创、@ 这些"发布设置"；发布时间和
        图片文案属于生成时冻结的内容，不在这里改动。
        """
        keys = ("visibility", "album", "location", "topic_template", "original", "mentions")
        with self.lock, self.app.db_connection() as db:
            config = self.get_plan(profile)["config"]
            now = time.time()
            rows = db.execute(
                "SELECT id,payload FROM publish_jobs WHERE profile_dir=? AND status IN ('draft','queued','paused','failed') AND json_extract(payload,'$.automation')=1",
                (profile,),
            ).fetchall()
            changed = 0
            for job_id, raw in rows:
                payload = json.loads(raw)
                if payload.get("schedule_epoch", 0) <= now + 300:
                    continue
                for key in keys:
                    if key in config:
                        payload[key] = config[key]
                db.execute(
                    "UPDATE publish_jobs SET payload=?,updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), self.app.now_iso(), int(job_id)),
                )
                changed += 1
        return {"ok": True, "changed": changed}

    def build_review_batch(self, profile, total, daily, root_path=""):
        """按"本次条数 / 每天条数"生成待审核队列（发布笔记页的批量计划）。"""
        total = int(total)
        daily = int(daily)
        if not 1 <= total <= 30:
            raise ValueError("本次发布条数需在 1–30 之间")
        if not 1 <= daily <= 5:
            raise ValueError("每天条数需在 1–5 之间")
        with self.lock:
            config = dict(self.get_plan(profile)["config"])
            folder_root = str(root_path or config.get("root_path") or "").strip()
            if not folder_root:
                raise ValueError("请先在发布笔记页选择素材文件夹")
            folder_path = Path(folder_root).expanduser().resolve()
            if not folder_path.is_dir():
                raise ValueError("素材文件夹不存在或不可访问")
            if not config.get("styles"):
                raise ValueError("请先到自动发布页保存文案风格等规则")
            if not config.get("windows"):
                raise ValueError("请先到账号画像设置常用发布时间段，并在自动发布页保存规则")
            config["root_path"] = str(folder_path)
            config["daily_count"] = daily
            config["submission_day_limit"] = max(int(config.get("submission_day_limit") or 1), total)
            config["submission_hour_limit"] = max(int(config.get("submission_hour_limit") or 1), min(12, total))
            if not config.get("expected_user_id"):
                identity = self.app.account_identity(profile)
                if not identity.get("user_id"):
                    raise ValueError("请先到小红书页面确认已登录当前账号")
                config["expected_user_id"] = identity["user_id"]
            now = datetime.now(timezone.utc)
            with self.app.db_connection() as db:
                used = {r[0] for r in db.execute("SELECT folder_path FROM automation_materials WHERE profile_dir=?", (profile,))}
                used.update(r[0] for r in db.execute(
                    "SELECT json_extract(payload,'$.folder_path') FROM publish_jobs WHERE profile_dir=? AND status!='cancelled' AND json_extract(payload,'$.automation')=1",
                    (profile,),
                ))
            candidates = [f for f in self.app.scan_folders(config["root_path"]) if f.path not in used]
            if not candidates:
                raise ValueError("素材文件夹里没有新的子文件夹；每个直接放图片的子文件夹只会用一次")
            created = []
            for folder in candidates[:total]:
                slot = self.slot(profile, config, now)
                if not slot:
                    break
                payload = self.make_payload(profile, config, folder, slot)
                payload["reviewed"] = False
                payload["automation"] = 1
                job = self.app.enqueue_job(payload, payload["schedule_time"], status="draft")
                with self.app.db_connection() as db:
                    db.execute(
                        "INSERT INTO automation_materials(profile_dir,folder_path,job_id,created_at) VALUES(?,?,?,?)",
                        (profile, folder.path, job["id"], self.app.now_iso()),
                    )
                created.append(job["id"])
            if not created:
                raise ValueError("没有生成任何队列：请检查素材子文件夹数量、发布时间段和提前准备天数")
            return created

    def confirm_drafts(self, profile, ids):
        """把勾选/确认过的草稿标记为已审核；只有已审核的才会被批量提交。"""
        changed = 0
        with self.lock, self.app.db_connection() as db:
            for job_id in ids:
                row = db.execute(
                    "SELECT payload,status FROM publish_jobs WHERE id=? AND profile_dir=?", (int(job_id), profile)
                ).fetchone()
                if not row or row[1] != "draft":
                    continue
                payload = json.loads(row[0])
                payload["reviewed"] = True
                db.execute(
                    "UPDATE publish_jobs SET payload=?,updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), self.app.now_iso(), int(job_id)),
                )
                changed += 1
        return changed

    def update_draft(self, profile, job_id, fields):
        """在发布笔记页修改某一条草稿；修改后必须重新确认。"""
        allowed = {
            "title", "content", "tags", "visibility", "album", "location",
            "topic_template", "original", "shooting_date", "mentions", "schedule_time",
        }
        with self.lock, self.app.db_connection() as db:
            row = db.execute(
                "SELECT payload,status FROM publish_jobs WHERE id=? AND profile_dir=?", (int(job_id), profile)
            ).fetchone()
            if not row or row[1] != "draft":
                raise ValueError("只能修改尚未提交的草稿，请刷新后重试")
            payload = json.loads(row[0])
            for key in allowed:
                if key in fields:
                    payload[key] = fields[key]
            payload["reviewed"] = False
            schedule = str(fields.get("schedule_time") or "").strip()
            if "schedule_time" in fields and not schedule:
                raise ValueError("定时任务的发布时间不能为空")
            if schedule:
                epoch = schedule_epoch({**payload, "schedule_time": schedule, "schedule_epoch": None})
                payload["schedule_epoch"] = epoch
                require_schedule_lead(payload)
                db.execute(
                    "UPDATE publish_jobs SET payload=?,schedule_time=?,updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), schedule, self.app.now_iso(), int(job_id)),
                )
            else:
                db.execute(
                    "UPDATE publish_jobs SET payload=?,updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), self.app.now_iso(), int(job_id)),
                )

    def reviewed_draft_ids(self, profile):
        with self.app.db_connection() as db:
            rows = db.execute(
                "SELECT id,payload FROM publish_jobs WHERE profile_dir=? AND status='draft' ORDER BY id ASC", (profile,)
            ).fetchall()
        return [int(job_id) for job_id, raw in rows if json.loads(raw).get("reviewed")]

    def cancel(self, profile, job_id):
        with self.lock, self.app.db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM publish_jobs WHERE id=? AND profile_dir=?", (int(job_id), profile)).fetchone()
            if not row or row[0] not in {"draft", "queued", "paused", "failed"}:
                raise ValueError("只能取消尚未提交的任务；状态不明的任务需要先核对")
            db.execute("UPDATE publish_jobs SET status='cancelled',updated_at=? WHERE id=?", (self.app.now_iso(), int(job_id)))
            db.execute("DELETE FROM automation_materials WHERE profile_dir=? AND job_id=?", (profile, int(job_id)))

    def resolve(self, profile, job_id, accepted):
        with self.lock, self.app.db_connection() as db:
            row = db.execute("SELECT status,history_id FROM publish_jobs WHERE id=? AND profile_dir=?", (int(job_id), profile)).fetchone()
            if not row or row[0] != "unknown":
                raise ValueError("任务不处于状态不明，请刷新")
            db.execute("UPDATE publish_jobs SET status=?,last_error='',updated_at=? WHERE id=?", ("submitted" if accepted else "cancelled", self.app.now_iso(), int(job_id)))
            if row[1]:
                db.execute("UPDATE publish_history SET status=? WHERE id=?", ("accepted" if accepted else "cancelled", row[1]))
            if not accepted:
                db.execute("DELETE FROM automation_materials WHERE profile_dir=? AND job_id=?", (profile, int(job_id)))

    def snapshot(self, profile):
        with self.app.db_connection() as db:
            db.row_factory = sqlite3.Row
            jobs = []
            for row in db.execute("SELECT * FROM publish_jobs WHERE profile_dir=? ORDER BY id DESC LIMIT 100", (profile,)):
                item = dict(row)
                item["payload"] = json.loads(item["payload"])
                jobs.append(item)
        return {"ok": True, "profile_dir": profile, "plan": self.get_plan(profile), "jobs": jobs,
                "common_windows": self.app.account_setting("schedule_windows", [], profile),
                "alerts": notifications.alerts(self.app), "notifications": notifications.settings(self.app)}

    def tick(self):
        with self.app.db_connection() as db:
            profiles = [r[0] for r in db.execute("SELECT profile_dir FROM automation_plans WHERE enabled=1")]
        for profile in profiles:
            try:
                self.build_batch(profile, limit=3)
            except Exception as exc:
                self.incident(profile, f"自动制作失败：{exc}。请检查规则或素材后恢复计划。", "prepare_error")
        self.reconcile_due()

    def next_receipt_job(self, now=None):
        """Return one submitted task ready for a management-page receipt check."""
        now = time.time() if now is None else float(now)
        with self.app.db_connection() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT j.*,h.created_at AS history_created_at,h.status AS history_status,
                r.next_check AS receipt_next_check,r.receipt AS receipt_json
                FROM publish_jobs j JOIN publish_history h ON h.id=j.history_id
                LEFT JOIN automation_receipts r ON r.job_id=j.id
                WHERE j.status='submitted'
                AND h.status IN ('scheduled','reviewing','accepted')
                AND COALESCE(r.next_check,0)<=?
                ORDER BY j.id""", (now,)).fetchall()
        for raw in rows:
            row = dict(raw)
            completed = row.get("completed_at") or row.get("started_at") or row.get("history_created_at")
            try:
                completed_epoch = datetime.fromisoformat(completed).timestamp()
            except (TypeError, ValueError):
                completed_epoch = 0
            if completed_epoch and completed_epoch <= now - 20:
                return row
        return None

    def reconcile_due(self):
        """Verify all accepted tasks in note management; never resubmit them."""
        now = time.time()
        row = self.next_receipt_job(now)
        if not row:
            return
        payload = json.loads(row["payload"])
        payload["receipt_reference_time"] = row.get("completed_at") or row.get("started_at") or row.get("history_created_at")
        try:
            from publish_checks import read_management, match_receipt
            with self.app.BROWSER_LOCK:
                if self.app.manual_pending(row["profile_dir"]):
                    return  # Never navigate away from a human's unsubmitted draft.
                self.app.switch_chrome_profile(row["profile_dir"])
                self.app.check_task_identity(payload)
                receipt = match_receipt(read_management(self.app.XHS_SKILL_DIR), payload)
            if receipt["status"] in {"published", "reviewing", "scheduled", "rejected"}:
                self.app.update_history_status(row["history_id"], receipt["status"])
            if receipt["status"] == "published":
                with self.app.db_connection() as db:
                    archive_row = db.execute(
                        "SELECT archive_result FROM publish_history WHERE id=?", (row["history_id"],)
                    ).fetchone()
                archive_result = str((archive_row or [""])[0] or "")
                if archive_result.startswith("未自动归档"):
                    notifications.add_alert(
                        self.app,
                        row["profile_dir"],
                        row["profile_dir"],
                        "archive_error",
                        f"任务 #{row['id']} 已在笔记管理确认发布，但素材{archive_result}。请手动核对归档。",
                    )
            due_epoch = float(payload.get("schedule_epoch") or 0)
            reference_epoch = due_epoch or datetime.fromisoformat(payload["receipt_reference_time"]).timestamp()
            if receipt["status"] == "rejected" or (now-reference_epoch > 7200 and receipt["status"] != "published"):
                self.app.db_set_setting("queue_enabled", False)
                self.incident(row["profile_dir"], f"任务 #{row['id']} 到点后的平台结果需要处理：{receipt.get('reason')}。请在平台核对。", "receipt_error")
            if receipt["status"] == "scheduled" and due_epoch > now:
                next_check = max(now + 1800, due_epoch + 300)
            elif receipt["status"] in {"reviewing", "unconfirmed"}:
                next_check = now + 600
            else:
                next_check = now + 1800
        except Exception as exc:
            receipt = {"status": "unconfirmed", "reason": str(exc)[:300]}
            self.app.db_set_setting("queue_enabled", False)
            self.incident(row["profile_dir"], f"任务 #{row['id']} 无法核对平台结果：{exc}。已暂停后续发布。", "receipt_error")
            next_check = now + 3600
        with self.app.db_connection() as db:
            db.execute("INSERT INTO automation_receipts(job_id,next_check,receipt) VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET next_check=excluded.next_check,receipt=excluded.receipt", (row["id"], next_check, json.dumps(receipt, ensure_ascii=False)))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def loop():
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    self.storage_failure(exc)
                self.stop.wait(30)
        def alerts_loop():
            while not self.stop.is_set():
                try:
                    self.cached_notification_settings = notifications.settings(self.app)
                    notifications.pump(self.app)
                except Exception as exc:
                    self.storage_failure(exc)
                self.stop.wait(5)
        self.thread = threading.Thread(target=loop, name="xhs-plan-producer", daemon=True)
        self.alert_thread = threading.Thread(target=alerts_loop, name="xhs-mac-alerts", daemon=True)
        self.thread.start()
        self.alert_thread.start()
