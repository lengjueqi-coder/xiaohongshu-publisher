from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dt_time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imagefx
import cover_service
import browser_profiles
import publishing_resources
from automation_engine import AutomationEngine, init_tables as init_automation_tables
import mac_notifications
from account_identity import read_connected_identity, safe_avatar_url
from update_service import UpdateError, check_for_update, launch_update


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parent
DB_PATH = APP_ROOT / "data" / "assistant.sqlite3"
SELFTEST_LOG_PATH = PROJECT_ROOT / "04_MVP开发验收记录.md"
_BUNDLED_XHS_SKILL_DIR = APP_ROOT / "app" / "vendor" / "xiaohongshu-skills"
_EXTERNAL_XHS_SKILL_DIR = PROJECT_ROOT / "tool-eval" / "xiaohongshu-skills"
XHS_SKILL_DIR = _BUNDLED_XHS_SKILL_DIR if _BUNDLED_XHS_SKILL_DIR.exists() else _EXTERNAL_XHS_SKILL_DIR
DEFAULT_TEST_ROOT = PROJECT_ROOT / "小红书软件测试"
PREPARED_DIR = APP_ROOT / "data" / "prepared"
IMPORTED_DIR = APP_ROOT / "data" / "imported"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMPORT_IMAGE_BYTES = 100 * 1024 * 1024
MAX_NOTE_IMAGES = 18
APP_VERSION = "2026.11.9"


COPY_STYLES = {
    "praise": "夸赞出镜人型",
    "professional": "专业展示型",
    "process": "制作过程分享型",
    "opinion": "观点讨论型",
    "story": "案例故事型",
}


@dataclass
class FolderCandidate:
    name: str
    path: str
    model_name: str
    image_count: int
    video_count: int
    latest_mtime: float
    latest_mtime_label: str
    shooting_date: str
    published_count: int
    last_used_at: str | None
    score_reason: str
    images: list[str]
    material_signature: str
    duplicate_status: str
    relative_path: str = ""
    year: str = ""
    month: str = ""
    hierarchy: list[str] | None = None
    display_name: str = ""


class PublishError(Exception):
    """A user-facing publish preparation or browser automation error."""


BROWSER_LOCK = threading.RLock()
QUEUE_WAKE = threading.Event()
QUEUE_STOP = threading.Event()
QUEUE_THREAD: threading.Thread | None = None
HTTP_SERVER: ThreadingHTTPServer | None = None
QUEUE_GAP_MIN_SECONDS = 60
QUEUE_GAP_MAX_SECONDS = 150
QUEUE_RESERVING_STATUSES = ("queued", "submitting", "reviewed_page", "draft", "paused", "unknown")
AUTOMATION: AutomationEngine | None = None
ACCOUNT_KEYS = {"root_path", "default_album", "default_location", "default_schedule_time", "default_schedule_window", "schedule_windows", "topic_templates", "local_topic_templates", "text_provider", "text_base_url", "pptoken_api_key", "pptoken_model", "daily_limit", "visibility", "publish_mode", "original_enabled", "copy_engine", "deepseek_model", "deepseek_api_key", "apiyi_key", "apiyi_base", "moments_tag", "fx_enabled", "logo_enabled", "logo_path", "logo_position", "logo_margin", "logo_scale", "logo_opacity", "watermark_enabled", "watermark_text", "watermark_path", "watermark_position", "watermark_opacity", "watermark_scale", "output_mode"}


@contextmanager
def db_connection():
    """Open a short-lived SQLite connection with explicit commit/close semantics."""
    db = sqlite3.connect(DB_PATH, timeout=30)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                folder_path TEXT NOT NULL,
                folder_name TEXT NOT NULL,
                image_paths TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL,
                copy_style TEXT NOT NULL,
                model_name TEXT,
                album TEXT,
                location TEXT,
                original INTEGER NOT NULL DEFAULT 1,
                visibility TEXT NOT NULL DEFAULT '公开可见',
                status TEXT NOT NULL DEFAULT 'prefilled',
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                image_manifest TEXT,
                material_signature TEXT,
                schedule_time TEXT,
                schedule_window TEXT,
                topic_template TEXT,
                shooting_date TEXT,
                shooting_location TEXT,
                warnings TEXT,
                profile_dir TEXT NOT NULL DEFAULT 'Default'
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                schedule_time TEXT NOT NULL,
                profile_dir TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                history_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        existing_cols = {row[1] for row in db.execute("PRAGMA table_info(publish_history)").fetchall()}
        for col, ddl in {
            "image_manifest": "ALTER TABLE publish_history ADD COLUMN image_manifest TEXT",
            "material_signature": "ALTER TABLE publish_history ADD COLUMN material_signature TEXT",
            "schedule_time": "ALTER TABLE publish_history ADD COLUMN schedule_time TEXT",
            "schedule_window": "ALTER TABLE publish_history ADD COLUMN schedule_window TEXT",
            "topic_template": "ALTER TABLE publish_history ADD COLUMN topic_template TEXT",
            "shooting_date": "ALTER TABLE publish_history ADD COLUMN shooting_date TEXT",
            "shooting_location": "ALTER TABLE publish_history ADD COLUMN shooting_location TEXT",
            "warnings": "ALTER TABLE publish_history ADD COLUMN warnings TEXT",
            "profile_dir": "ALTER TABLE publish_history ADD COLUMN profile_dir TEXT NOT NULL DEFAULT 'Default'",
            "archive_info": "ALTER TABLE publish_history ADD COLUMN archive_info TEXT",
            "archive_result": "ALTER TABLE publish_history ADD COLUMN archive_result TEXT",
        }.items():
            if col not in existing_cols:
                db.execute(ddl)
        db.execute("UPDATE publish_history SET profile_dir = 'Default' WHERE profile_dir IS NULL OR profile_dir = ''")
        recovered_jobs = db.execute(
            """
            UPDATE publish_jobs
            SET status = 'unknown', last_error = '服务重启时任务处于提交中，需人工确认', updated_at = ?
            WHERE status IN ('submitting', 'reviewed_page')
            """,
            (now_iso(),),
        ).rowcount
        init_automation_tables(db)
        owner = db.execute("SELECT value FROM settings WHERE key='active_chrome_profile'").fetchone()
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('legacy_settings_owner',?)", (owner[0] if owner else json.dumps("Default"),))
        if db.execute("SELECT COUNT(*) FROM settings WHERE key!='legacy_settings_owner'").fetchone()[0] == 0:
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                ("root_path", json.dumps(str(DEFAULT_TEST_ROOT), ensure_ascii=False)),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                ("default_album", json.dumps("演员资料照", ensure_ascii=False)),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                ("default_location", json.dumps("", ensure_ascii=False)),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                ("default_schedule_time", json.dumps("", ensure_ascii=False)),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                ("schedule_windows", json.dumps(["早上 10:00", "晚上 22:00-24:00"], ensure_ascii=False)),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                ("topic_templates", json.dumps(["演员资料", "艺考录制"], ensure_ascii=False)),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                ("copy_engine", json.dumps("auto", ensure_ascii=False)),
            )
        # Keep settings portable with the application directory. Existing values are preserved.
        if db.execute("SELECT 1 FROM settings WHERE key = 'queue_enabled'").fetchone() is None:
            db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?)",
                ("queue_enabled", json.dumps(False)),
            )
        if recovered_jobs:
            db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("queue_enabled", json.dumps(False)),
            )
            db.execute("UPDATE automation_plans SET enabled=0,state='attention',last_error='服务重启时存在提交中任务，请先核对平台结果'")
            db.execute("INSERT INTO automation_alerts(profile_dir,plan_id,kind,message,created_at) VALUES(?,?,?,?,?)", ("全部账号", "", "restart", "服务重启时存在提交中的任务，已暂停。请先核对平台结果，避免重复发布。", now_iso()))
        db.commit()


def db_get_setting(key: str, default: Any = None) -> Any:
    if key in ACCOUNT_KEYS:
        return account_setting(key, default)
    with db_connection() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return row[0]


def db_set_setting(key: str, value: Any) -> None:
    if key in ACCOUNT_KEYS:
        key = f"account_defaults:{active_profile_dir()}:{key}"
    with db_connection() as db:
        db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        db.commit()


def account_setting(key: str, default: Any = None, profile_dir: str | None = None) -> Any:
    profile = profile_dir or active_profile_dir()
    with db_connection() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (f"account_defaults:{profile}:{key}",)).fetchone()
        owner = db.execute("SELECT value FROM settings WHERE key='legacy_settings_owner'").fetchone()
        if row is None and profile == (json.loads(owner[0]) if owner else "Default"):
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def save_publish_preferences(profile_dir: str, values: dict[str, Any]) -> None:
    allowed = {
        "visibility": {"公开可见", "仅自己可见", "仅互关好友可见"},
        "publish_mode": {"immediate", "scheduled"},
        "original_enabled": None,
        "default_schedule_window": None,
    }
    for key, options in allowed.items():
        if key not in values:
            continue
        if key == "original_enabled" and type(values[key]) is not bool:
            raise ValueError("原创声明设置必须为布尔值")
        if key == "default_schedule_window":
            window = str(values[key] or "").strip()
            windows = account_setting(
                "schedule_windows",
                ["早上 10:00", "晚上 22:00-24:00"],
                profile_dir=profile_dir,
            )
            if not isinstance(windows, list):
                windows = ["早上 10:00", "晚上 22:00-24:00"]
            if window and window not in {str(item) for item in windows if item}:
                raise ValueError("默认时间段必须来自当前账号已保存的时间段列表")
            values[key] = window
        elif options is not None and values[key] not in options:
            raise ValueError(f"无效的发布设置：{key}")
    with db_connection() as db:
        for key in allowed:
            if key in values:
                db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (f"account_defaults:{profile_dir}:{key}", json.dumps(values[key], ensure_ascii=False)))


# ---------- AI 封面（API易）配置：流程与 Key 均迁移自样片工厂已验证实现 ----------

APIYI_BASE_DEFAULT = "https://api.apiyi.com"
APIYI_KEY_ENV = "APIYI_API_KEY"

APIYI_MODELS = [
    {
        "id": "gemini-3.1-flash-image-preview",
        "label": "Nano Banana 2 快速版（约¥0.18/张）",
        "model": "gemini-3.1-flash-image-preview",
        "protocol": "gemini-native",
        "resolutions": ["1k", "2k", "4k"],
        "default_resolution": "2k",
    },
    {
        "id": "gemini-3-pro-image",
        "label": "Nano Banana Pro 高质量（约¥0.52/张）",
        "model": "gemini-3-pro-image",
        "protocol": "gemini-native",
        "resolutions": ["2k", "4k"],
        "default_resolution": "4k",
    },
]

COVER_TEMPLATES_DEFAULT = [
    "保留人物与构图，整体调成暖调胶片感，轻微颗粒，适合小红书封面",
    "清透日系色调，肤色白皙通透，画面干净明亮",
    "柔和电影感光晕，突出人物主体，背景轻微虚化",
    "服装细节严格保持不变，背景替换为简约奶油色摄影棚背景",
    "画面顶部留出标题文字区域，构图整体下移，高级简约排版感",
    "整体提亮并增强对比，突出服装质感与色彩",
    "复古港风调色，浓郁胶片质感，情绪氛围感强",
    "高级灰调，杂志大片质感，锐度适中",
]


def mask_key(key: str) -> str:
    key = str(key or "")
    if len(key) <= 10:
        return "****" if key else ""
    return f"{key[:8]}****{key[-4:]}"


def ensure_ai_defaults() -> None:
    """初始化本地接入配置，不把共享密钥写进源码或新数据库。"""
    if db_get_setting("apiyi_key") is None:
        env_key = os.environ.get(APIYI_KEY_ENV, "").strip()
        if env_key:
            db_set_setting("apiyi_key", env_key)
    if db_get_setting("apiyi_base") is None:
        db_set_setting("apiyi_base", APIYI_BASE_DEFAULT)
    if db_get_setting("cover_templates") is None:
        db_set_setting("cover_templates", COVER_TEMPLATES_DEFAULT)


def save_cover_settings(payload: dict) -> None:
    # Empty password inputs preserve the stored credential.
    base = cover_service.normalize_base_url(payload["apiyi_base"]) if payload.get("apiyi_base") else None
    if base:
        db_set_setting("apiyi_base", base)
    if str(payload.get("apiyi_key") or "").strip():
        db_set_setting("apiyi_key", str(payload["apiyi_key"]).strip())


# ---------- 多账号（Chrome Profile 隔离）：每个 Profile 独立保存一个小红书登录态 ----------

CHROME_DATA_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


def chrome_profiles() -> list[dict[str, str]]:
    """列出本机 Chrome 的所有 Profile（含用户起的昵称）。"""
    out: list[dict[str, str]] = []
    if not CHROME_DATA_DIR.is_dir():
        return out
    dirs = ["Default"] + sorted(p.name for p in CHROME_DATA_DIR.glob("Profile *"))
    for name in dirs:
        prefs = CHROME_DATA_DIR / name / "Preferences"
        label = name
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
            label = str(((data.get("profile") or {}).get("name")) or name)
        except Exception:
            pass
        out.append({"dir": name, "label": label})
    return out


def account_identity(profile_dir: str) -> dict[str, Any]:
    saved = db_get_setting(f"account_identity:{profile_dir}", {})
    if not isinstance(saved, dict):
        return {}
    return {**saved, "avatar": safe_avatar_url(saved.get("avatar"))}


def account_list() -> dict[str, Any]:
    # Listing cached labels must remain responsive while browser jobs hold the lock.
    profiles = [{**profile, "identity": account_identity(profile["dir"])} for profile in chrome_profiles()]
    return {"ok": True, "profiles": profiles, "active": active_profile_dir()}


def clear_account_identity(profile_dir: str) -> None:
    db_set_setting(f"account_identity:{profile_dir}", {
        "nickname": "", "avatar": "", "user_id": "", "logged_in": False,
        "status": "logged_out", "checked_at": now_iso(),
    })


def refresh_account_identity(profile_dir: str | None = None) -> dict[str, Any]:
    """Bind a live read to the selected profile, leaving cached identity on disconnect."""
    with BROWSER_LOCK:
        profile = profile_dir or active_profile_dir()
        if profile != active_profile_dir():
            raise PublishError("账号已经切换，请刷新当前账号资料")
        try:
            select_profile_connection(profile)
            identity = read_connected_identity(XHS_SKILL_DIR)
            identity["checked_at"] = now_iso()
        except Exception as exc:
            identity = {
                **account_identity(profile),
                "status": "unavailable",
                "error": str(exc)[:300],
            }
        db_set_setting(f"account_identity:{profile}", identity)
        return {"ok": True, "profile": profile, "identity": identity}


def wait_extension_ready(seconds: int = 30) -> bool:
    """轮询等待 Chrome 扩展接入 Bridge。"""
    for _ in range(seconds):
        try:
            result = run_xhs_cli(["check-login"], timeout=20)
            if result["ok"] and "not connected" not in result["stdout"].lower():
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


FX_KEYS = (
    "fx_enabled",
    "logo_enabled",
    "logo_path",
    "logo_position",
    "logo_margin",
    "logo_scale",
    "logo_opacity",
    "watermark_enabled",
    "watermark_text",
    "watermark_path",
    "watermark_position",
    "watermark_opacity",
    "watermark_scale",
    "output_mode",
)


def get_fx_params(overrides: dict[str, Any] | None = None, profile_dir: str | None = None) -> dict[str, Any]:
    stored = {key: account_setting(key, profile_dir=profile_dir) for key in FX_KEYS}
    stored = {k: v for k, v in stored.items() if v is not None}
    merged = {**imagefx.DEFAULT_PARAMS, **stored}
    if overrides:
        for key in FX_KEYS:
            if key in overrides and overrides[key] is not None:
                merged[key] = overrides[key]
    return merged


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def store_imported_image(filename: str, raw: bytes, target_root: Path | None = None) -> Path:
    if not raw:
        raise ValueError("没有收到图片数据")
    if len(raw) > MAX_IMPORT_IMAGE_BYTES:
        raise ValueError("单张拖入图片不能超过 100MB")
    safe_name = Path(str(filename or "").replace("\\", "/")).name.strip()
    suffix = Path(safe_name).suffix.lower()
    if not safe_name or suffix not in IMAGE_EXTENSIONS:
        raise ValueError("只支持拖入 JPG、PNG、WebP、GIF 或 HEIC 图片")
    folder = Path(target_root or IMPORTED_DIR) / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / safe_name
    target.write_bytes(raw)
    return target


def validate_note_image_count(images: list[Any], require_one: bool = False) -> None:
    if require_one and not images:
        raise PublishError("每条笔记至少需要 1 张图片")
    if len(images) > MAX_NOTE_IMAGES:
        raise PublishError(f"小红书图文最多发布 {MAX_NOTE_IMAGES} 张图片，请从列表移除多余图片后再继续")


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def text_response(handler: BaseHTTPRequestHandler, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
    data = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")  # 防止浏览器缓存旧页面/旧脚本
    handler.end_headers()
    handler.wfile.write(data)


def error_response(handler: BaseHTTPRequestHandler, message: str, status: int = 400, details: Any = None) -> None:
    json_response(handler, {"ok": False, "error": message, "details": details}, status)


def image_files(folder: Path) -> list[Path]:
    files: list[Path] = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
            # 跳过本软件合成的产物（-logo 后缀 / prepared 压缩副本），避免当素材二次发布
            if p.stem.endswith("-logo") or p.stem.endswith("-xhs") or p.name.startswith(("四宫格_", "九宫格_", "collage-")):
                continue
            files.append(p)
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def file_manifest(paths: list[str] | list[Path]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists() or not p.is_file():
            continue
        stat = p.stat()
        manifest.append(
            {
                "name": p.name,
                "path": str(p),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    return sorted(manifest, key=lambda item: item["name"])


def material_signature(paths: list[str] | list[Path]) -> str:
    manifest = file_manifest(paths)
    parts = [f"{item['name']}:{item['size']}:{item['mtime']}" for item in manifest]
    return "|".join(parts)


def prepare_images_for_xhs(paths: list[str]) -> tuple[list[str], list[str]]:
    """Return upload-safe image paths without mutating originals."""
    PREPARED_DIR.mkdir(parents=True, exist_ok=True)
    prepared: list[str] = []
    notes: list[str] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists() or not p.is_file():
            continue
        if p.stat().st_size <= MAX_IMAGE_BYTES:
            prepared.append(str(p))
            continue
        source_key = hashlib.sha256(str(p.resolve()).encode()).hexdigest()[:16]
        target = PREPARED_DIR / f"{p.stem}-{source_key}-xhs.jpg"
        max_dims = [2600, 2200, 1800, 1500, 1200]
        for dim in max_dims:
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-Z", str(dim), str(p), "--out", str(target)],
                text=True,
                capture_output=True,
                timeout=60,
            )
            if target.exists() and target.stat().st_size <= MAX_IMAGE_BYTES:
                prepared.append(str(target))
                notes.append(f"{p.name} 已压缩到 {target.name}")
                break
        else:
            if target.exists():
                prepared.append(str(target))
                notes.append(f"{p.name} 已压缩但仍可能超过15MB，请验收时确认")
            else:
                prepared.append(str(p))
                notes.append(f"{p.name} 超过15MB且压缩失败，已使用原图")
    return prepared, notes


def freeze_payload(payload: dict[str, Any], task_dir: Path | None = None) -> dict[str, Any]:
    """Freeze reviewed image bytes and effects so execution cannot change them."""
    if not payload.get("frozen_images"):
        payload = publishing_resources.resolve_template(payload, account_setting("local_topic_templates", [], payload.get("profile_dir")))
    images = payload.get("images") or []
    validate_note_image_count(images, require_one=True)
    if payload.get("frozen_images"):
        verify_frozen_images(payload)
        return payload
    payload = {**payload, "archive_info": make_archive_info(payload)}
    task_dir = task_dir or APP_ROOT / "data" / "tasks" / uuid.uuid4().hex
    task_dir.mkdir(parents=True, exist_ok=True)
    fx = get_fx_params(payload.get("fx_params"), profile_dir=payload.get("profile_dir"))
    fx["output_mode"] = "merge"
    for enabled, path_key in (("logo_enabled", "logo_path"), ("watermark_enabled", "watermark_path")):
        if fx.get(enabled) and not (enabled == "watermark_enabled" and fx.get("watermark_text")) and not Path(fx.get(path_key) or "").is_file():
            raise PublishError("已开启的 LOGO/图片水印文件不存在，请重新选择")
    copied = []
    for index, raw in enumerate(images):
        source = Path(raw)
        if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS:
            raise PublishError(f"图片不存在或格式不支持：{source.name}")
        target = task_dir / f"source-{index:02d}{source.suffix.lower()}"
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        copied.append(str(target))
    outputs, notes = imagefx.apply_for_publish(copied, fx, task_dir / "rendered")
    if notes:
        raise PublishError("图片处理未完成：" + "；".join(notes))
    outputs, notes = prepare_images_for_xhs(outputs)
    if any("失败" in n or "仍可能" in n for n in notes):
        raise PublishError("图片压缩未完成：" + "；".join(notes))
    final, manifest = [], []
    for index, raw in enumerate(outputs):
        source = Path(raw)
        target = task_dir / f"upload-{index:02d}{source.suffix.lower()}"
        shutil.copy2(source, target)
        from PIL import Image
        with Image.open(target) as image:
            image.verify()
        final.append(str(target))
        manifest.append({"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    return {**payload, "images": final, "source_images": copied, "fx_params": fx, "frozen_images": manifest}


def archive_manifest(folder: Path) -> list[list[Any]]:
    entries = []
    for current, dirs, files in os.walk(folder, followlinks=False):
        base = Path(current)
        for name in sorted(dirs + files):
            path = base / name
            if path.is_symlink():
                raise ValueError("素材组包含符号链接")
            stat = path.stat()
            entries.append([str(path.relative_to(folder)), stat.st_size, stat.st_mtime_ns, path.is_dir()])
        if base == folder and any(name != "AI封面" for name in dirs):
            raise ValueError("素材组包含其他子文件夹")
    return sorted(entries)


def make_archive_info(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("material_mode") != "folder":
        return {"skip": "临时或混合选图，不移动原素材"}
    try:
        raw_root = Path(os.path.abspath(os.path.expanduser(str(payload.get("material_root") or ""))))
        root = raw_root.resolve(strict=True)
        raw_folder = Path(os.path.abspath(os.path.expanduser(str(payload.get("folder_path") or ""))))
        raw_folder.relative_to(raw_root)
        folder = raw_folder.resolve(strict=True)
        relative = folder.relative_to(root)
        if not relative.parts or "已发布" in relative.parts or root == Path(root.anchor):
            raise ValueError("素材组不能是根目录或已发布目录")
        for path in [raw_folder, *raw_folder.parents]:
            if path.is_symlink():
                raise ValueError("素材路径包含符号链接")
            if path == raw_root:
                break
        if not folder.is_dir():
            raise ValueError("素材组不存在")
        for image in payload.get("archive_source_images", payload.get("images") or []):
            resolved = Path(image).resolve(strict=True)
            if not resolved.is_relative_to(folder) and not resolved.is_relative_to(PREPARED_DIR.resolve()):
                raise ValueError("图片来自其他文件夹")
        return {"root": str(root), "folder": str(folder), "manifest": archive_manifest(folder)}
    except (OSError, ValueError) as exc:
        return {"skip": f"未自动归档：{exc}"}


def archive_history(history_id: int) -> str:
    try:
        with db_connection() as db:
            row = db.execute("SELECT archive_info, archive_result FROM publish_history WHERE id=?", (history_id,)).fetchone()
        if not row:
            return "记录不存在"
        if row[1] and str(row[1]).startswith("已归档："):
            return row[1]
        info = json.loads(row[0] or "{}")
        if info.get("skip"):
            result = info["skip"]
        elif not info.get("root") or not info.get("folder"):
            result = "旧记录没有素材来源快照，未移动"
        else:
            root, folder = Path(info["root"]), Path(info["folder"])
            relative = folder.relative_to(root)
            if not relative.parts or folder.is_symlink() or folder.resolve() != folder or root.resolve() != root:
                raise ValueError("素材路径已变化")
            if archive_manifest(folder) != info.get("manifest"):
                raise ValueError("素材文件夹内容已变化")
            target = root / "已发布" / relative
            if target.exists() or target.is_symlink():
                raise ValueError("归档目标已存在")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.parent.resolve() != target.parent:
                raise ValueError("归档路径包含符号链接")
            # macOS renamex_np atomically refuses an existing destination.
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            if sys.platform == "darwin":
                if libc.renamex_np(os.fsencode(folder), os.fsencode(target), 0x00000004) != 0:
                    raise OSError(ctypes.get_errno(), "归档移动失败")
            else:
                raise ValueError("此系统不支持安全归档移动")
            result = f"已归档：{target}"
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        result = f"未自动归档：{exc}"
    with db_connection() as db:
        db.execute("UPDATE publish_history SET archive_result=? WHERE id=?", (result, history_id))
    return result


def verify_frozen_images(payload: dict[str, Any]) -> None:
    manifest = payload.get("frozen_images") or []
    if [m["path"] for m in manifest] != payload.get("images"):
        raise PublishError("任务图片清单发生变化，请重新制作")
    for item in manifest:
        path = Path(item["path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise PublishError("已确认的图片已被改动或删除，已停止提交")


def video_count(folder: Path) -> int:
    return sum(1 for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)


def infer_model_name(folder_name: str) -> str:
    name = re.sub(r"^\d{4}[-_.年]\d{1,2}[-_.月]\d{1,2}[日]?\s*[-_ ]*", "", folder_name)
    name = re.sub(r"^\d{2,4}[.年-]\d{1,2}(?:[.月-]\d{1,2})?[日]?\s*[-_ ]*", "", name)
    name = re.sub(r"^(模特|演员|艺人)\s*[-_:：]?\s*", "", name)
    name = re.sub(r"^(艺考|演员|模特|艺人)\s*", "", name)
    parts = re.split(r"[-_｜| ]+", name)
    fallback = ""
    for part in parts:
        cleaned = part.strip()
        if cleaned and not fallback:
            fallback = cleaned
        if cleaned and not re.search(r"(棚拍|演员|资料|形象|见组|写真|照片|成片)", cleaned):
            return cleaned[:12]
    return (fallback or folder_name)[:12]


def infer_shooting_date(folder_name: str, today_ref: date | None = None) -> str:
    """Infer a shooting date from common folder names, returning YYYY-MM-DD or empty."""
    today_ref = today_ref or date.today()
    patterns = [
        (r"(20\d{2})[-_.年](\d{1,2})[-_.月](\d{1,2})[日]?", "full"),
        (r"(?<!\d)(\d{2})[-_.年](\d{1,2})[-_.月](\d{1,2})[日]?", "short_year"),
        (r"(?<!\d)(\d{1,2})[.月/-](\d{1,2})[日]?", "month_day"),
    ]
    for pattern, kind in patterns:
        match = re.search(pattern, folder_name)
        if not match:
            continue
        try:
            if kind == "full":
                year, month, day = map(int, match.groups())
            elif kind == "short_year":
                yy, month, day = map(int, match.groups())
                year = 2000 + yy
            else:
                month, day = map(int, match.groups())
                year = today_ref.year
                candidate = date(year, month, day)
                if candidate > today_ref:
                    year -= 1
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def infer_shooting_date_from_sources(folder_name: str, image_paths: list[Path], today_ref: date | None = None) -> str:
    """Infer shooting date from folder name, image filename, EXIF/metadata, else 3-5 days before publish day."""
    from_name = infer_shooting_date(folder_name, today_ref)
    if from_name:
        return from_name

    for image_path in image_paths:
        from_file_name = infer_shooting_date(image_path.name, today_ref)
        if from_file_name:
            return from_file_name

    for image_path in image_paths[:5]:
        metadata_date = read_image_capture_date(image_path)
        if metadata_date:
            return metadata_date

    # 兜底：无法识别时，取发布日往前推 3~5 天之间的随机一天
    today_ref = today_ref or date.today()
    return (today_ref - timedelta(days=random.randint(3, 5))).isoformat()


def read_image_capture_date(image_path: Path) -> str:
    """Read camera capture date with macOS metadata; return YYYY-MM-DD or empty."""
    try:
        proc = subprocess.run(
            ["mdls", "-raw", "-name", "kMDItemContentCreationDate", str(image_path)],
            text=True,
            capture_output=True,
            timeout=8,
        )
    except Exception:
        return ""
    raw = proc.stdout.strip()
    if not raw or raw == "(null)":
        return ""
    match = re.search(r"(20\d{2})-(\d{2})-(\d{2})", raw)
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def parse_schedule_window(raw: str) -> dict[str, Any]:
    """Parse labels like '早上 10:00' or '晚上 22:00-24:00'."""
    label = raw.strip()
    matches = re.findall(r"(?<!\d)([01]?\d|2[0-4]):([0-5]\d)(?!\d)", label)
    if not label or not matches:
        raise ValueError("发布时间段需要包含 HH:MM，例如：晚上 22:00-24:00")
    minutes: list[int] = []
    for hour_raw, minute_raw in matches[:2]:
        hour = int(hour_raw)
        minute = int(minute_raw)
        if hour == 24 and minute != 0:
            raise ValueError("24 点只能写成 24:00")
        minutes.append(hour * 60 + minute)
    if len(minutes) == 1:
        return {"label": label, "kind": "fixed", "start": minutes[0], "end": minutes[0]}
    return {"label": label, "kind": "range", "start": minutes[0], "end": minutes[1]}


def _datetime_from_day_and_minute(day: date, minute: int) -> datetime:
    if minute >= 24 * 60:
        return datetime.combine(day + timedelta(days=1), dt_time.min)
    return datetime.combine(day, dt_time(hour=minute // 60, minute=minute % 60))


def resolve_schedule_time(raw_window: str, now_ref: datetime | None = None, rng: random.Random | None = None) -> str:
    """Pick the next usable time inside a configured window, precise to minute."""
    now_ref = now_ref or datetime.now()
    rng = rng or random.Random()
    parsed = parse_schedule_window(raw_window)
    start = int(parsed["start"])
    end = int(parsed["end"])

    if parsed["kind"] == "fixed":
        for offset in range(3):
            candidate = _datetime_from_day_and_minute(now_ref.date() + timedelta(days=offset), start)
            if candidate > now_ref:
                return candidate.isoformat(timespec="minutes")
        raise ValueError("没有找到可用的未来发布时间")

    for offset in range(3):
        base_day = now_ref.date() + timedelta(days=offset)
        start_dt = _datetime_from_day_and_minute(base_day, start)
        end_day = base_day if end > start else base_day + timedelta(days=1)
        end_dt = _datetime_from_day_and_minute(end_day, end)
        earliest = max(start_dt, now_ref + timedelta(minutes=1))
        earliest = earliest.replace(second=0, microsecond=0)
        latest = end_dt.replace(second=0, microsecond=0)
        if earliest <= latest:
            span_minutes = int((latest - earliest).total_seconds() // 60)
            selected = earliest + timedelta(minutes=rng.randint(0, span_minutes))
            return selected.isoformat(timespec="minutes")
    raise ValueError("没有找到可用的未来发布时间")


def normalize_schedule_time(raw: str) -> str:
    """Normalize datetime-local/API values to a local minute without seconds."""
    value = str(raw or "").strip().replace(" ", "T")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("发布时间格式无法识别，请使用 YYYY-MM-DD HH:MM") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def reserved_schedule_times(
    profile_dir: str | None = None,
    exclude_job_id: int | None = None,
) -> list[str]:
    """Return future queue slots that still reserve an account's daily capacity."""
    profile = str(profile_dir or active_profile_dir())
    placeholders = ", ".join("?" for _ in QUEUE_RESERVING_STATUSES)
    params: list[Any] = [*QUEUE_RESERVING_STATUSES, profile]
    exclusion = ""
    if exclude_job_id is not None:
        exclusion = " AND id != ?"
        params.append(int(exclude_job_id))
    with db_connection() as db:
        rows = db.execute(
            f"""
            SELECT schedule_time
            FROM publish_jobs
            WHERE status IN ({placeholders})
              AND profile_dir = ?
              AND schedule_time != ''
              {exclusion}
            """,
            params,
        ).fetchall()
    values: list[str] = []
    for row in rows:
        try:
            values.append(normalize_schedule_time(row[0]))
        except ValueError:
            continue
    return values


def _planned_candidates_for_day(
    parsed: dict[str, Any],
    day: date,
    now_ref: datetime,
    rng: random.Random,
) -> list[datetime]:
    """Build shuffled minute candidates for one calendar window."""
    start = int(parsed["start"])
    end = int(parsed["end"])
    if parsed["kind"] == "fixed":
        candidate = _datetime_from_day_and_minute(day, start)
        # Keep the same six-minute browser-safety buffer used by ranges for
        # today's fixed slot. Future days must retain the exact configured time.
        if day == now_ref.date():
            return [candidate] if candidate >= now_ref + timedelta(minutes=6) else []
        return [candidate] if candidate > now_ref else []

    start_dt = _datetime_from_day_and_minute(day, start)
    end_day = day if end > start else day + timedelta(days=1)
    end_dt = _datetime_from_day_and_minute(end_day, end)
    # Treat 24:00 as the end boundary, not as a second slot on the next day.
    latest = end_dt - timedelta(minutes=1) if end == 24 * 60 else end_dt
    earliest = start_dt
    if day == now_ref.date():
        earliest = max(earliest, now_ref + timedelta(minutes=6))
    earliest = earliest.replace(second=0, microsecond=0)
    latest = latest.replace(second=0, microsecond=0)
    if earliest > latest:
        return []
    span_minutes = int((latest - earliest).total_seconds() // 60)
    offsets = list(range(span_minutes + 1))
    rng.shuffle(offsets)
    return [earliest + timedelta(minutes=offset) for offset in offsets]


def resolve_planned_schedule_time(
    raw_window: str,
    reserved_times: list[str] | None = None,
    profile_dir: str | None = None,
    now_ref: datetime | None = None,
) -> str:
    """Choose a future slot while respecting queued slots and the daily account limit."""
    now_ref = now_ref or datetime.now()
    profile_dir = profile_dir or active_profile_dir()
    if reserved_times is None:
        reserved_list = reserved_schedule_times(profile_dir)
    else:
        reserved_list = []
        for value in reserved_times:
            try:
                reserved_list.append(normalize_schedule_time(str(value)))
            except ValueError:
                continue
    reserved = set(reserved_list)
    daily_limit = max(1, int(account_setting("daily_limit", 3, profile_dir)))
    parsed = parse_schedule_window(raw_window)
    for offset in range(31):
        day = now_ref.date() + timedelta(days=offset)
        reference = now_ref if offset == 0 else datetime.combine(day, dt_time.min) - timedelta(minutes=1)
        candidates = _planned_candidates_for_day(parsed, day, reference, random.SystemRandom())
        for candidate_dt in candidates:
            candidate = candidate_dt.isoformat(timespec="minutes")
            candidate_day = candidate_dt.date()
            reserved_for_day = sum(1 for value in reserved_list if value[:10] == candidate_day.isoformat())
            if usage_count_for_day(candidate_day, profile_dir) + reserved_for_day >= daily_limit:
                continue
            if candidate not in reserved:
                return candidate
    raise ValueError("未来 31 天内没有符合每日上限和时间段的可用发布时间")


def validate_explicit_schedule_time(
    raw_time: str,
    profile_dir: str | None = None,
    reserved_times: list[str] | None = None,
    now_ref: datetime | None = None,
) -> str:
    """Validate a user-selected slot without silently changing it."""
    normalized = normalize_schedule_time(raw_time)
    now_ref = now_ref or datetime.now()
    selected = datetime.fromisoformat(normalized)
    if selected < now_ref + timedelta(minutes=5):
        raise ValueError("定时发布时间必须至少晚于当前时间 5 分钟")
    profile = profile_dir or active_profile_dir()
    reserved_list = reserved_schedule_times(profile) if reserved_times is None else reserved_times
    normalized_reserved = {normalize_schedule_time(value) for value in reserved_list}
    if normalized in normalized_reserved:
        raise ValueError("这个账号的该发布时间已被队列占用，请换一个时间")
    daily_limit = max(1, int(account_setting("daily_limit", 3, profile)))
    same_day = sum(1 for value in reserved_list if normalize_schedule_time(value)[:10] == normalized[:10])
    if usage_count_for_day(selected.date(), profile) + same_day >= daily_limit:
        raise ValueError(f"计划日已达到发布上限：{selected.date().isoformat()} {daily_limit}/{daily_limit}")
    return normalized


def resolve_queue_schedule(payload: dict[str, Any], requested_time: str = "") -> str:
    """Resolve a queue slot, preferring a valid requested time and replanning conflicts."""
    profile_dir = str(payload.get("profile_dir") or active_profile_dir())
    reserved = reserved_schedule_times(profile_dir)
    requested = str(requested_time or payload.get("schedule_time") or "").strip()
    # Replanning is opt-in. A missing flag represents a manually selected
    # time, so stale/conflicting requests must be rejected rather than moved.
    auto_schedule = payload.get("schedule_auto") is True
    if requested:
        try:
            return validate_explicit_schedule_time(requested, profile_dir, reserved)
        except ValueError:
            if not auto_schedule:
                raise
    window = str(payload.get("schedule_window") or "").strip()
    if not window:
        raise ValueError("请选择发布时间段，软件才能自动排期")
    return resolve_planned_schedule_time(window, reserved, profile_dir)


def get_folder_history(folder_path: str, signature: str = "") -> tuple[int, str | None]:
    """Return usage for this exact publish folder.

    The source directory is the unit of work. Its contents are intentionally not
    deduplicated across folders because the user has already curated them.
    ``signature`` remains in the signature for compatibility with old callers.
    """
    with db_connection() as db:
        row = db.execute(
            """
            SELECT COUNT(*), MAX(created_at)
            FROM publish_history
            WHERE status IN ('published','scheduled','accepted','reviewing')
              AND folder_path = ?
              AND profile_dir = ?
            """,
            (folder_path, active_profile_dir()),
        ).fetchone()
    return int(row[0] or 0), row[1]


def usage_count_for_day(day: date, profile_dir: str | None = None) -> int:
    """Count platform-accepted jobs scheduled for a calendar day."""
    prefix = day.isoformat()
    with db_connection() as db:
        row = db.execute(
            """
            SELECT COUNT(*)
            FROM publish_history
            WHERE status IN ('published','scheduled','accepted','reviewing')
              AND substr(CASE WHEN schedule_time != '' THEN schedule_time ELSE created_at END, 1, 10) = ?
              AND profile_dir = ?
            """,
            (prefix, profile_dir or active_profile_dir()),
        ).fetchone()
    return int(row[0] or 0)


def today_usage_count() -> int:
    return usage_count_for_day(date.today(), active_profile_dir())


def _calendar_context(root: Path, folder: Path) -> tuple[str, str, list[str], str]:
    """Infer year/month labels from the selected path and its publish unit.

    The selected root may itself be a year or month folder, so inspect the
    absolute folder path for calendar labels while keeping the returned
    hierarchy relative to the selected root.
    """
    relative = folder.relative_to(root) if folder != root else Path(folder.name)
    parts = list(relative.parts)
    year = ""
    month = ""
    path_parts = list(folder.parts)

    def month_number(value: str) -> int | None:
        value = value.rstrip("月")
        if not re.fullmatch(r"0?[1-9]|1[0-2]", value):
            return None
        return int(value)

    for index, part in enumerate(path_parts):
        full = re.search(r"(?<!\d)(20\d{2})[-_.年](\d{1,2})[-_.月]?(?:\d{1,2})?日?(?!\d)", part)
        if full:
            year, month = full.group(1), f"{int(full.group(2)):02d}"
            continue
        year_match = re.fullmatch(r"20\d{2}年?", part)
        if year_match:
            year = re.match(r"20\d{2}", part).group(0)
            if index + 1 < len(path_parts):
                next_month = month_number(path_parts[index + 1])
                if next_month is not None:
                    month = f"{next_month:02d}"
    return str(relative), year, month, parts


def scan_folders(root_path: str) -> list[FolderCandidate]:
    root = Path(os.path.expanduser(root_path)).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"素材根目录不存在或不是文件夹: {root}")

    candidates: list[FolderCandidate] = []
    # A publish unit is any directory that directly contains curated images.
    # Walking recursively supports both a flat root and year/month/archive roots.
    folders: list[Path] = [root]
    for current, dirs, _files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in {"AI封面", "prepared", "自动发布产物", "已发布", "__MACOSX"})
        current_path = Path(current)
        if current_path != root:
            folders.append(current_path)
    for folder in folders:
        imgs = image_files(folder)
        if not imgs:
            continue
        latest_mtime = max(p.stat().st_mtime for p in imgs)
        relative_path, year, month, hierarchy = _calendar_context(root, folder)
        candidates.append(
            FolderCandidate(
                name=folder.name,
                path=str(folder),
                model_name=infer_model_name(folder.name),
                image_count=len(imgs),
                video_count=video_count(folder),
                latest_mtime=latest_mtime,
                latest_mtime_label=datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M"),
                shooting_date=infer_shooting_date_from_sources(folder.name, imgs),
                published_count=0,
                last_used_at=None,
                score_reason="按文件夹独立处理，不做去重评分",
                images=[str(p) for p in imgs],
                material_signature="",
                duplicate_status="not_scored",
                relative_path=relative_path,
                year=year,
                month=month,
                hierarchy=hierarchy,
                display_name=relative_path if relative_path != "." else folder.name,
            )
        )

    candidates.sort(key=lambda c: (-c.latest_mtime, c.relative_path))
    return candidates


def choose_image_group(images: list[str], max_count: int = MAX_NOTE_IMAGES) -> list[str]:
    selected = images[: max(1, min(int(max_count), MAX_NOTE_IMAGES))]
    return sorted(selected, key=lambda p: Path(p).name)


def default_tags(model_name: str, style: str) -> list[str]:
    base = ["演员资料照", "形象照", "见组照", "摄影工作室", "棚拍"]
    if style == "praise":
        base += ["镜头表现力", "上镜"]
    elif style == "professional":
        base += ["演员资料", "职业形象照", "选角资料"]
    elif style == "process":
        base += ["拍摄花絮", "拍摄现场", "道具布置"]
    elif style == "opinion":
        base += ["演员成长", "拍照建议", "资料照怎么拍"]
    elif style == "story":
        base += ["拍摄案例", "客片分享", "写真案例"]
    deduped: list[str] = []
    for tag in base:
        clean = tag.strip().lstrip("#")
        if clean and clean not in deduped:
            deduped.append(clean)
    return deduped[:10]


def generate_copy(payload: dict[str, Any]) -> dict[str, Any]:
    engine = payload.get("copy_engine") or account_setting("copy_engine", "auto", payload.get("profile_dir"))
    if engine in ("auto", "deepseek", "anthropic"):
        try:
            return generate_deepseek_copy({**payload, "strict_ai": True})
        except PublishError as exc:
            # Auto may fall back, but the interface must explicitly say so.
            if engine == "deepseek":
                raise
            fallback = generate_template_copy(payload)
            fallback["warning"] = f"{exc}；本次使用本地模板"
            return fallback
    return generate_template_copy(payload)


def generate_template_copy(payload: dict[str, Any]) -> dict[str, Any]:
    style = payload.get("style") or "professional"
    model_name = (payload.get("model_name") or "这位小姐姐").strip()
    style_label = COPY_STYLES.get(style, COPY_STYLES["professional"])
    title_map = {
        "praise": f"{model_name}这组资料照状态太好了",
        "professional": "演员资料照别只拍漂亮",
        "process": "这组演员资料照的拍摄小记录",
        "opinion": "演员资料照到底要不要拍得很大片",
        "story": f"{model_name}这次资料照拍摄案例",
    }
    body_map = {
        "praise": f"今天这组里的{model_name}真的很上镜。眼神、肩颈状态和镜头前的松弛感都很自然，照片不是硬凹出来的漂亮，而是能看到她本人稳定、干净的气质。演员资料照最怕用力过猛，这组刚好把表现力和真实感留住了。",
        "professional": "演员资料照不是单纯把人拍漂亮，而是让选角老师快速看到一个人的气质、可塑性和镜头可信度。干净的光线、自然的表情、不过度设计的造型，往往比复杂布景更重要。照片要像本人，也要让人愿意多看一眼。",
        "process": "这组拍摄我们没有把画面做得很复杂，重点是先让人物在镜头前放松下来。拍摄过程中会不断微调站姿、眼神和手部动作，让照片既有资料照需要的清晰度，也保留一点现场的呼吸感。最后这组成片我自己也很喜欢。",
        "opinion": "演员资料照到底要不要拍得很“大片”？我的答案是不一定。真正有效的资料照，应该先让人相信这个演员，再让人记住这个演员。过度精修、过度造型，反而可能削弱真实感和投递时的可信度。",
        "story": f"这次拍摄前，我们先确定了一个目标：不要把{model_name}拍成统一模板里的漂亮照片，而是保留她自己的气质。前半段主要在找状态，后面眼神和肢体明显更稳定，照片也慢慢有了她自己的辨识度。",
    }
    tags = default_tags(model_name, style)
    return {
        "title": title_map.get(style, title_map["professional"])[:20],
        "content": body_map.get(style, body_map["professional"]),
        "tags": tags,
        "style": style,
        "style_label": style_label,
        "ai_used": False,
        "engine": "local-template",
    }


def generate_ai_copy(payload: dict[str, Any]) -> dict[str, Any] | None:
    return generate_deepseek_copy(payload)


def _ssl_context():
    """本机有代理拦截 HTTPS 时，Python 默认证书库会验证失败，改用 certifi。"""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


TEXT_PROVIDER_LABELS = {"deepseek": "DeepSeek", "pptoken": "PPToken"}


def text_model_config(
    profile_dir: str | None = None,
    settings: dict[str, Any] | None = None,
    deepseek_model_override: str | None = None,
) -> dict[str, str]:
    """Resolve one account's actual provider and model without crossing providers."""
    settings = settings if isinstance(settings, dict) else {}
    provider = str(
        settings["text_provider"] if "text_provider" in settings
        else account_setting("text_provider", "deepseek", profile_dir)
    ).strip()
    if provider not in TEXT_PROVIDER_LABELS:
        raise PublishError("模型平台无效")
    if provider == "pptoken":
        model = str(
            settings["pptoken_model"] if "pptoken_model" in settings
            else account_setting("pptoken_model", "", profile_dir)
        ).strip()
        base = str(
            settings["text_base_url"] if "text_base_url" in settings
            else account_setting("text_base_url", "", profile_dir)
        ).strip()
        supplied_key = str(settings.get("pptoken_api_key") or "").strip()
        token = supplied_key or str(account_setting("pptoken_api_key", "", profile_dir) or "").strip()
    else:
        configured_model = str(
            settings["deepseek_model"] if "deepseek_model" in settings
            else account_setting("deepseek_model", "deepseek-v4-pro", profile_dir)
        ).strip()
        model = str(deepseek_model_override or configured_model).strip()
        base = "https://api.deepseek.com"
        supplied_key = str(settings.get("deepseek_api_key") or "").strip()
        token = supplied_key or str(
            account_setting("deepseek_api_key", "", profile_dir) or os.environ.get("DEEPSEEK_API_KEY") or ""
        ).strip()
    return {
        "provider": provider,
        "provider_label": TEXT_PROVIDER_LABELS[provider],
        "model": model,
        "model_label": model,
        "base_url": base,
        "api_key": token,
    }


def public_text_model_config(config: dict[str, str]) -> dict[str, str]:
    return {key: config[key] for key in ("provider", "provider_label", "model", "model_label", "base_url")}


def call_deepseek_json(system: str, prompt: str, timeout: int = 120,
                       profile_dir: str | None = None, model: str | None = None,
                       strict: bool = False, settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Call the account-selected OpenAI-compatible JSON model."""
    model_config = text_model_config(profile_dir, settings, deepseek_model_override=model)
    label = model_config["provider_label"]
    token = model_config["api_key"]
    if not token:
        if strict:
            raise PublishError(f"尚未设置 {label} API Key")
        return None
    model = model_config["model"]
    base = model_config["base_url"]
    parsed_url = urllib.parse.urlsplit(base)
    if not model or parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        if strict:
            raise PublishError(f"请填写 {label} 的 HTTPS API 基础地址和模型名称")
        return None
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("模型未返回 JSON 对象")
        return parsed
    except Exception as exc:
        status = getattr(exc, "code", None)
        reason = {401: "API Key 无效", 402: "余额不足", 429: "请求过于频繁", 400: "模型或请求参数无效"}.get(status)
        message = f"{label} 调用失败（{reason or ('HTTP ' + str(status) if status else type(exc).__name__)}）"
        print(message)
        if strict:
            raise PublishError(message) from None
        return None


def check_text_model_connection(
    profile_dir: str,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Test either saved account settings or explicit unsaved form values."""
    model_config = text_model_config(profile_dir, settings)
    result = call_deepseek_json(
        '请只返回一个 JSON 对象。',
        '返回 {"ok":true}',
        profile_dir=profile_dir,
        timeout=30,
        strict=True,
        settings=settings,
    )
    if not result or result.get("ok") is not True:
        raise PublishError("模型未返回预期格式，请检查模型是否支持 JSON 输出")
    return {"ok": True, **public_text_model_config(model_config)}


def active_profile_dir() -> str:
    return str(db_get_setting("active_chrome_profile", "Default") or "Default")


def require_active_profile(payload: dict[str, Any] | None = None) -> str:
    """Return the active Chrome profile and reject stale request context."""
    active = active_profile_dir()
    requested = str((payload or {}).get("profile_dir") or "").strip()
    if requested and requested != active:
        raise PublishError("账号已切换，请刷新后再提交当前任务")
    return requested or active


def _profile_setting_key(profile_dir: str | None = None) -> str:
    profile = profile_dir or active_profile_dir()
    return f"account_profile:{profile}"


DEFAULT_COPY_PROMPTS = {
    "generate": "先理解下方账号画像中的行业、业务、目标受众及其实际需求，再结合用户提供的拍摄事实写标题和正文。关键词用于理解定位，不要机械堆砌。用行业内行的具体观察表达价值，避免空泛夸赞、营销套话和不实承诺。只写有依据的事实；不要虚构拍摄过程，不要把文件夹名、日期编号、地点、合集等内部管理信息自动写进正文，也不要将人名自动转成话题。",
    "regenerate": "保留当前风格，换一个表达角度重新写标题和正文。",
    "natural": "保持原意和真实信息，减少套话与夸张描述，让标题和正文更自然口语化。",
}


def get_copy_prompts(profile_dir: str | None = None) -> dict[str, str]:
    saved = db_get_setting(f"copy_prompts:{profile_dir or active_profile_dir()}", {})
    return {key: str(saved.get(key) or value) for key, value in DEFAULT_COPY_PROMPTS.items()} if isinstance(saved, dict) else dict(DEFAULT_COPY_PROMPTS)


def save_manual_profile(profile_dir: str, values: dict[str, Any]) -> dict[str, Any]:
    profile = dict(get_account_profile(profile_dir) or {})
    for key in ("manual_keywords", "manual_topics", "manual_notes"):
        profile[key] = str(values.get(key) or "").strip()[:10000]
    profile["source_profile"] = profile_dir
    db_set_setting(_profile_setting_key(profile_dir), profile)
    return profile


def get_account_profile(profile_dir: str | None = None) -> dict[str, Any] | None:
    """Read the saved profile for the active Chrome account."""
    raw = db_get_setting(_profile_setting_key(profile_dir), None)
    if raw is None and (profile_dir or active_profile_dir()) == "Default":
        # Migrate the original single-profile setting without losing it.
        raw = db_get_setting("account_profile", None)
    if isinstance(raw, dict) and raw:
        return raw
    return None


def generate_account_profile(
    max_count: int = 20,
    manual_keywords: str = "",
    manual_topics: str = "",
    manual_notes: str = "",
    expected_profile: str | None = None,
    source_mode: str = "recent",
) -> dict[str, Any]:
    """抓取创作平台近 N 条笔记 + 首页数据，交给 DeepSeek 总结为账号画像并保存。"""
    max_count = max(1, min(20, int(max_count)))
    if source_mode not in {"recent", "manual"}:
        raise PublishError("画像资料来源无效")
    manual_present = any((manual_keywords, manual_topics, manual_notes))
    if source_mode == "manual" and not manual_present:
        raise PublishError("请先填写账号关键词、长期话题或账号补充说明")
    source_error = ""
    with BROWSER_LOCK:
        source_profile = active_profile_dir()
        if expected_profile and source_profile != expected_profile:
            raise PublishError("账号已经切换，请在当前账号画像页面重新生成")
        result = {"ok": True, "json": {"success": True}}
        if source_mode == "recent":
            if manual_pending(source_profile):
                raise PublishError("此账号还有待人工提交的发布页，请先确认或放弃，读取笔记会离开该页面")
            try:
                result = run_xhs_cli(["creator-profile", "--max-count", str(max_count)], timeout=300)
            except Exception as exc:
                result = {"ok": False, "json": {}, "stderr": f"{type(exc).__name__}: {exc}"}
    data = result.get("json") or {}
    if not result.get("ok") or not data.get("success"):
        source_error = (data.get("error") or result.get("stderr") or "读取创作平台数据失败")[:300]
    notes = (data.get("notes") or {}).get("notes") or []
    home_text = ((data.get("datacenter") or {}).get("text") or "")[:3000]
    if not notes and not manual_present:
        return {"ok": False, "error": source_error or "未抓取到笔记数据，请填写关键词/话题或先登录创作平台"}
    notes_brief = "\n".join(
        f"- {n.get('title')}（{n.get('time')}）浏览{n.get('views')} 赞{n.get('likes')} 藏{n.get('collects')} 评{n.get('comments')}\n正文：{str(n.get('content') or '未读取')[:1500]}\n话题：{'、'.join(n.get('tags') or []) or '未读取'}"
        for n in notes[:max_count]
    )
    manual_context = f"""
【人工补充信息】
- 关键词：{manual_keywords or '未填写'}
- 常用话题：{manual_topics or '未填写'}
- 账号补充说明：{manual_notes or '未填写'}
""".strip()
    prompt = f"""
请根据以下小红书账号的可用数据，总结一份"账号画像"，用于指导后续文案生成。

【创作平台首页快照】
{home_text}

【近期笔记及数据】
{notes_brief or '未读取到近期笔记，请主要依据人工补充信息生成。'}
仅按实际取得的资料分析。缺少正文或话题时不要声称已理解全部内容，数据不足时不要编造表现规律。

{manual_context}

请输出严格 JSON（不要 Markdown，不要解释）：
{{
  "account_type": "账号类型，如 摄影工作室/个人摄影师",
  "positioning": "一句话账号定位",
  "track": "主赛道（细分到具体业务，如 艺考照/证件照/写真）",
  "audience": "目标受众画像（年龄/身份/需求）",
  "content_style": "内容风格与语气特点（从标题归纳）",
  "best_performing": "数据表现最好的内容类型及规律",
  "topics": ["建议长期使用的话题标签，5-10个，不带#"],
  "avoid": "应该避免的内容或做法"
}}
""".strip()
    model_config = text_model_config(source_profile)
    parsed = call_deepseek_json("你是资深小红书运营分析师，擅长从账号数据中提炼账号定位与内容策略。", prompt, timeout=120, profile_dir=source_profile)
    if not parsed or not parsed.get("positioning"):
        return {"ok": False, "error": "AI 总结画像失败，请重试"}
    detail_success = sum(
        str(note.get("detail_status") or "").lower() in {"complete", "empty", "success"}
        or (not note.get("detail_status") and ("content" in note or "tags" in note))
        for note in notes
    )
    detail_failed = len(notes) - detail_success
    content_success = sum(
        bool(str(note.get("content") or "").strip())
        for note in notes
        if str(note.get("detail_status") or "").lower() in {"complete", "empty", "success"}
        or (not note.get("detail_status") and ("content" in note or "tags" in note))
    )
    topic_success = sum(
        bool(note.get("tags"))
        for note in notes
        if str(note.get("detail_status") or "").lower() in {"complete", "empty", "success"}
        or (not note.get("detail_status") and ("content" in note or "tags" in note))
    )
    if detail_failed:
        detail_messages = list(dict.fromkeys(
            str(note.get("detail_error") or "").strip()
            for note in notes
            if str(note.get("detail_status") or "").lower() == "failed" and str(note.get("detail_error") or "").strip()
        ))
        detail_error = f"{detail_failed} 条笔记详情读取失败"
        if detail_messages:
            detail_error += "：" + "；".join(detail_messages[:2])
        source_error = "；".join(part for part in (source_error, detail_error) if part)[:500]

    parsed["generated_at"] = now_iso()
    parsed["note_count"] = len(notes)
    parsed["source_mode"] = source_mode
    parsed["source_completeness"] = {
        "requested": max_count if source_mode == "recent" else 0,
        "read": len(notes),
        "detail_success": detail_success,
        "detail_failed": detail_failed,
        "content_success": content_success,
        "content_empty": detail_success - content_success,
        "topic_success": topic_success,
        "topic_empty": detail_success - topic_success,
        # Keep the original keys for older frontends and saved profiles.
        "with_content": content_success,
        "with_topics": topic_success,
    }
    parsed["source_profile"] = source_profile
    parsed["ai_provider"] = model_config["provider"]
    parsed["ai_provider_label"] = model_config["provider_label"]
    parsed["ai_model"] = model_config["model"]
    parsed["manual_keywords"] = manual_keywords or "、".join(str(parsed.get(key) or "") for key in ("positioning", "track", "audience") if parsed.get(key))
    parsed["manual_topics"] = manual_topics or "、".join(parsed.get("topics") or [])
    parsed["manual_notes"] = manual_notes
    parsed["source_notes"] = notes[:max_count]
    if source_error:
        parsed["source_error"] = source_error
    db_set_setting(_profile_setting_key(source_profile), parsed)
    return {
        "ok": True,
        "profile": parsed,
        "source_notes": notes[:max_count],
        "source_error": source_error,
        "profile_dir": source_profile,
    }


def profile_prompt_block(profile_dir: str | None = None) -> str:
    """把账号画像转成 prompt 片段，注入文案生成；没有画像时返回空串。"""
    profile = get_account_profile(profile_dir)
    if not profile:
        return ""
    topics = "、".join(str(t) for t in (profile.get("topics") or [])[:10])
    return f"""
【账号画像（必须遵循）】
- 账号定位：{profile.get('positioning', '')}
- 主赛道：{profile.get('track', '')}
- 目标受众：{profile.get('audience', '')}
- 内容风格：{profile.get('content_style', '')}
- 表现好的规律：{profile.get('best_performing', '')}
- 推荐话题：{topics}
- 避免：{profile.get('avoid', '')}
- 手动关键词：{profile.get('manual_keywords', '')}
- 手动话题：{profile.get('manual_topics', '')}
- 用户补充：{profile.get('manual_notes', '')}
""".strip()


def _validated_copy(
    parsed: dict[str, Any] | None,
    payload: dict[str, Any],
    model_config: dict[str, str],
) -> dict[str, Any]:
    provider_label = model_config["provider_label"]
    if not isinstance(parsed, dict):
        raise PublishError(f"{provider_label} 未返回有效文案")
    title = str(parsed.get("title") or "").strip()
    content = str(parsed.get("content") or "").strip()
    tags = payload.get("tags", [])
    if not isinstance(tags, list):
        raise PublishError(f"{provider_label} 话题格式无效")
    if not title or not content or len(title) > 20 or len(content) > 1000:
        raise PublishError(f"{provider_label} 文案为空或超出字数限制，请重新生成")
    style = payload.get("style") or "professional"
    return {"title": title, "content": content, "tags": tags, "style": style,
            "style_label": COPY_STYLES.get(style, COPY_STYLES["professional"]),
            "ai_used": True,
            "engine": f'{provider_label} · {model_config["model"]}',
            "provider": model_config["provider"],
            "provider_label": provider_label,
            "model": model_config["model"]}


def generate_deepseek_copy(payload: dict[str, Any]) -> dict[str, Any] | None:
    profile_dir = payload.get("profile_dir")
    model_config = text_model_config(
        profile_dir,
        deepseek_model_override=str(payload.get("deepseek_model") or "") or None,
    )
    style = payload.get("style") or "professional"
    prompt = f"""
请为小红书账号生成一条新的图文文案，每次换一种表达和切入角度。
{get_copy_prompts(payload.get('profile_dir'))['generate']}
{get_copy_prompts(payload.get('profile_dir'))['regenerate'] if payload.get('previous_title') else ''}
{profile_prompt_block(payload.get('profile_dir'))}
要求：标题不超过20字；正文120-260字，口语自然，不夸大，不虚构拍摄经历或画面细节。
先结合账号定位和目标受众，理解他们在该摄影场景中的真实需求与顾虑，选择一个具体角度自然表达；只使用已提供的事实，不凭空补充客户经历、效果或行业数据。
不要生成或返回话题；话题只由用户明确填写或选定的模板决定。严格输出JSON：{{"title":"...","content":"..."}}。
只参考出镜人和用户备注；不要附加素材关键词、文件夹日期/编号、专辑或地点等管理信息。
风格：{COPY_STYLES.get(style, COPY_STYLES['professional'])}
出镜人：{payload.get('model_name') or '未提供'}
用户拍摄备注：{payload.get('notes') or '未提供'}
上次标题（本次请使用不同表达）：{str(payload.get('previous_title') or '')[:100]}
""".strip()
    try:
        parsed = call_deepseek_json("你是中文摄影文案编辑，只根据已知事实写作，输出JSON。", prompt,
                                    timeout=90, profile_dir=profile_dir,
                                    model=str(payload.get("deepseek_model") or "") or None, strict=True)
        return _validated_copy(parsed, payload, model_config)
    except PublishError:
        if payload.get("strict_ai"):
            raise
        return None


def polish_copy(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "").strip()
    if not title or not content:
        raise PublishError("请先填写当前标题和正文，再点击更自然")
    profile_dir = payload.get("profile_dir")
    model_config = text_model_config(
        profile_dir,
        deepseek_model_override=str(payload.get("deepseek_model") or "") or None,
    )
    prompt = f"""
润色下面当前正在编辑的标题和正文，使表达更自然、口语化，减少套话和夸张形容。
{get_copy_prompts(payload.get('profile_dir'))['natural']}
保持原意、人物、主题和所有真实信息；不增加拍摄细节、地点、日期或经历。
删除“这组素材关键词”“专辑：…；地点：…”等自动附加的管理元数据段落。
标题不超过20字，正文不超过1000字。只返回JSON：{{"title":"...","content":"..."}}。
{profile_prompt_block(payload.get('profile_dir'))}
当前文案（JSON数据）：{json.dumps({'title':title, 'content':content}, ensure_ascii=False)}
""".strip()
    parsed = call_deepseek_json("你是中文文案编辑。只润色给定文案，输出JSON。", prompt,
                                timeout=90, profile_dir=profile_dir,
                                model=str(payload.get("deepseek_model") or "") or None, strict=True)
    return _validated_copy(parsed, {**payload, "polish":True}, model_config)


def run_xhs_cli(args: list[str], timeout: int = 180) -> dict[str, Any]:
    bundled_python = APP_ROOT / ".venv" / "bin" / "python"
    if bundled_python.exists():
        prefix = [str(bundled_python)]
    elif shutil.which("uv"):
        prefix = ["uv", "run", "python"]
    else:
        prefix = [sys.executable]
    cmd = [*prefix, str(XHS_SKILL_DIR / "scripts" / "cli.py"), *args]
    started = time.monotonic()
    with BROWSER_LOCK:
        client = select_profile_connection()
        proc = subprocess.run(
            cmd,
            env={**os.environ, "XHS_BRIDGE_CLIENT": client},
            cwd=XHS_SKILL_DIR,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    elapsed = round(time.monotonic() - started, 2)
    if args and args[0] == "fill-publish":
        log_path = APP_ROOT / "data" / "publish-fill.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n[{now_iso()}] fill-publish {elapsed}s code={proc.returncode}\n{stderr}\n")
    parsed: Any = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    parsed = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "json": parsed,
        "elapsed_seconds": elapsed,
    }


MOMENTS_DIR = APP_ROOT / "moments"


def run_moments_cli(payload: dict[str, Any], timeout: int = 240) -> dict[str, Any]:
    """调用朋友圈引擎：payload 写入临时文件 → node cli.js --payload → 解析 RESULT_JSON 行。"""
    payload_file = Path(tempfile.gettempdir()) / f"moments-payload-{int(time.time())}.json"
    payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", "cli.js", "--payload", str(payload_file)],
            cwd=MOMENTS_DIR,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    finally:
        payload_file.unlink(missing_ok=True)
    parsed: Any = None
    marker = proc.stdout.rfind("RESULT_JSON:")
    if marker >= 0:
        try:
            parsed = json.loads(proc.stdout[marker + len("RESULT_JSON:"):].strip())
        except json.JSONDecodeError:
            pass
    return {
        "ok": proc.returncode == 0 and bool(parsed and parsed.get("ok")),
        "code": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "json": parsed,
    }


def content_warnings(payload: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    tags = [str(t).strip().lstrip("#") for t in payload.get("tags") or [] if str(t).strip()]
    schedule_time = (payload.get("schedule_time") or "").strip()
    if len(title) > 20:
        warnings.append("标题超过小红书 20 字限制")
    if len(tags) > 10:
        warnings.append("话题超过 10 个，将截取前 10 个")
    if schedule_time:
        try:
            scheduled_at = datetime.fromisoformat(schedule_time)
            if scheduled_at <= datetime.now():
                warnings.append("定时发布时间早于当前时间，请改为未来时间")
        except ValueError:
            warnings.append("定时发布时间格式无法识别")
    return warnings


def record_history(payload: dict[str, Any], status: str, warnings: list[str] | None = None) -> int:
    image_paths = payload.get("images") or []
    manifest = file_manifest(image_paths)
    signature = material_signature(image_paths)
    with db_connection() as db:
        cur = db.execute(
            """
            INSERT INTO publish_history (
                platform, folder_path, folder_name, image_paths, title, content, tags,
                copy_style, model_name, album, location, original, visibility, status, created_at,
                image_manifest, material_signature, schedule_time, schedule_window,
                topic_template, shooting_date, shooting_location, warnings, profile_dir, archive_info
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "xhs",
                payload.get("folder_path") or "",
                payload.get("folder_name") or "",
                json.dumps(image_paths, ensure_ascii=False),
                payload.get("title") or "",
                payload.get("content") or "",
                json.dumps(payload.get("tags") or [], ensure_ascii=False),
                payload.get("style") or "",
                payload.get("model_name") or "",
                payload.get("album") or "",
                payload.get("location") or "",
                1 if payload.get("original", True) else 0,
                payload.get("visibility") or "公开可见",
                status,
                now_iso(),
                json.dumps(manifest, ensure_ascii=False),
                signature,
                payload.get("schedule_time") or "",
                payload.get("schedule_window") or "",
                payload.get("topic_template") or "",
                payload.get("shooting_date") or "",
                payload.get("shooting_location") or payload.get("location") or "",
                json.dumps(warnings or [], ensure_ascii=False),
                payload.get("profile_dir") or active_profile_dir(),
                json.dumps(payload.get("archive_info") or {}, ensure_ascii=False),
            ),
        )
        db.commit()
        return int(cur.lastrowid)


def history_rows(limit: int = 50) -> list[dict[str, Any]]:
    with db_connection() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM publish_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("image_paths", "tags", "image_manifest", "warnings"):
            try:
                item[key] = json.loads(item[key])
            except Exception:
                pass
        result.append(item)
    return result


def _job_key(payload: dict[str, Any], schedule_time: str, profile_dir: str) -> str:
    material = {
        "profile_dir": profile_dir,
        "schedule_time": schedule_time,
        "folder_path": payload.get("folder_path") or "",
        "title": payload.get("title") or "",
        "content": payload.get("content") or "",
        "images": payload.get("images") or [],
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def queue_rows(limit: int = 100) -> list[dict[str, Any]]:
    with db_connection() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT jobs.*, history.status AS history_status, history.archive_result
               FROM (SELECT * FROM publish_jobs ORDER BY id DESC LIMIT ?) AS jobs
               LEFT JOIN publish_history AS history ON history.id = jobs.history_id
               ORDER BY jobs.id ASC""",
            (limit,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item["payload"])
        except Exception:
            item["payload"] = {}
        item["done"] = item["status"] == "submitted"
        result.append(item)
    return result


def enqueue_job(payload: dict[str, Any], schedule_time: str, status: str = "queued") -> dict[str, Any]:
    profile_dir = str(payload.get("profile_dir") or active_profile_dir())
    schedule_time = "" if payload.get("publish_mode") == "immediate" else normalize_schedule_time(schedule_time)
    payload = {**payload, "schedule_time": schedule_time, "profile_dir": profile_dir}
    key = _job_key(payload, schedule_time, profile_dir)
    created = now_iso()
    with db_connection() as db:
        db.row_factory = sqlite3.Row
        existing = db.execute("SELECT * FROM publish_jobs WHERE job_key = ?", (key,)).fetchone()
        if existing:
            row = dict(existing)
        else:
            cur = db.execute(
                """
                INSERT INTO publish_jobs(
                    job_key, payload, schedule_time, profile_dir, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, json.dumps(payload, ensure_ascii=False), schedule_time, profile_dir, status, created, created),
            )
            row = dict(db.execute("SELECT * FROM publish_jobs WHERE id = ?", (cur.lastrowid,)).fetchone())
        db.commit()
    try:
        row["payload"] = json.loads(row["payload"])
    except Exception:
        row["payload"] = payload
    row["done"] = row["status"] == "submitted"
    return row


def update_job(job_id: int, **fields: Any) -> None:
    allowed = {
        "status",
        "history_id",
        "attempts",
        "last_error",
        "updated_at",
        "started_at",
        "completed_at",
    }
    fields = {key: value for key, value in fields.items() if key in allowed}
    if not fields:
        return
    fields.setdefault("updated_at", now_iso())
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with db_connection() as db:
        db.execute(
            f"UPDATE publish_jobs SET {assignments} WHERE id = ?",
            (*fields.values(), int(job_id)),
        )
        db.commit()


def claim_next_job() -> dict[str, Any] | None:
    with db_connection() as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM publish_jobs WHERE status='queued' AND COALESCE(json_extract(payload,'$.submit_after'),'')<=? ORDER BY id ASC LIMIT 1",
            (now_iso(),),
        ).fetchone()
        if not row:
            db.commit()
            return None
        now = now_iso()
        db.execute(
            "UPDATE publish_jobs SET status = 'submitting', attempts = attempts + 1, started_at = ?, updated_at = ? WHERE id = ?",
            (now, now, row["id"]),
        )
        db.commit()
    item = dict(row)
    item["attempts"] = int(item.get("attempts") or 0) + 1
    item["status"] = "submitting"
    item["payload"] = json.loads(item["payload"])
    return item


def bridge_client(profile_dir: str | None = None) -> str:
    return str(db_get_setting(f"profile_bridge_client:{profile_dir or active_profile_dir()}", "") or "")


def manual_pending(profile_dir: str | None = None) -> dict[str, Any] | None:
    value = db_get_setting(f"manual_pending:{profile_dir or active_profile_dir()}")
    return value if isinstance(value, dict) and value.get("history_id") else None


def clear_manual_pending(history_id: int, profile_dir: str) -> None:
    pending = manual_pending(profile_dir)
    if pending and int(pending["history_id"]) == history_id:
        db_set_setting(f"manual_pending:{profile_dir}", None)


def sync_publishing_resources(profile_dir: str) -> dict[str, Any]:
    with BROWSER_LOCK:
        if profile_dir != active_profile_dir():
            raise PublishError("账号已切换，请重新同步")
        try:
            select_profile_connection(profile_dir)
            result = publishing_resources.read_resources(XHS_SKILL_DIR)
        except Exception as exc:
            message = str(exc)[:500]
            result = {
                "templates": [],
                "albums": [],
                "status": {
                    "templates": {"ok": False, "count": 0, "topics_complete": False, "message": message},
                    "albums": {"ok": False, "count": 0, "message": message},
                },
                "errors": [message],
            }
        old = db_get_setting(f"publishing_resources:{profile_dir}", {})
        old = old if isinstance(old, dict) else {}
        checked_at = now_iso()
        status = result.get("status") if isinstance(result.get("status"), dict) else {}
        # A failed read preserves the last confirmed list; a successful empty
        # read deliberately replaces stale data with an empty list.
        saved = {
            **old,
            "profile_dir": profile_dir,
            "checked_at": checked_at,
            "status": status,
            "errors": list(result.get("errors") or []),
        }
        successful = []
        for key in ("templates", "albums"):
            resource_status = status.get(key) if isinstance(status.get(key), dict) else {}
            if resource_status.get("ok") is True:
                saved[key] = list(result.get(key) or [])
                saved[f"{key}_synced_at"] = checked_at
                successful.append(key)
        saved["ok"] = len(successful) == 2
        saved["partial"] = bool(successful) and len(successful) != 2
        if saved["ok"]:
            saved["synced_at"] = checked_at
        db_set_setting(f"publishing_resources:{profile_dir}", saved)
        return saved


def select_profile_connection(profile_dir: str | None = None) -> str:
    target_profile = str(profile_dir or active_profile_dir())
    client = bridge_client(target_profile)
    if client:
        try:
            browser_profiles.select(client)
            return client
        except Exception as exc:
            selection_error = exc
    else:
        selection_error = RuntimeError("这个 Chrome 用户资料尚未绑定扩展，请先切换/连接账号")

    # A legacy bridge has exactly one extension but no client IDs. The newer
    # bridge is equally unambiguous when it reports exactly one live client.
    # This fallback is restricted to the already-active profile: applying it
    # to another profile would silently cross account boundaries.
    if target_profile == active_profile_dir():
        try:
            current = browser_profiles.reuse_single_connection(XHS_SKILL_DIR)
            if current is not None:
                return current
        except Exception as exc:
            selection_error = exc
    raise PublishError(str(selection_error)) from None


def switch_chrome_profile(profile_dir: str) -> dict[str, Any]:
    """Pair/select exactly one extension, retaining other Chrome windows."""
    profile_dir = str(profile_dir or "Default").strip()
    if profile_dir not in {p["dir"] for p in chrome_profiles()}:
        raise PublishError(f"未知浏览器账号目录：{profile_dir}")
    with BROWSER_LOCK:
        try:
            browser_profiles.ensure_server(XHS_SKILL_DIR)
            try:
                client = select_profile_connection(profile_dir)
            except Exception:
                client = browser_profiles.pair(profile_dir, XHS_SKILL_DIR)
                db_set_setting(f"profile_bridge_client:{profile_dir}", client)
            # Verify through the target connection before changing the UI account.
            identity = read_connected_identity(XHS_SKILL_DIR)
            if not identity.get("logged_in"):
                raise PublishError(f"Chrome「{profile_dir}」尚未登录小红书，请在该用户资料登录")
        except Exception as exc:
            old_client = bridge_client()
            if old_client:
                try:
                    browser_profiles.select(old_client)
                except Exception:
                    pass
            raise PublishError(str(exc)) from None
        db_set_setting("active_chrome_profile", profile_dir)
        db_set_setting(f"account_identity:{profile_dir}", {**identity, "checked_at": now_iso()})
    return {"ready": True, "profile": profile_dir, "login": {"ok": True}}


def prepare_publish(payload: dict[str, Any], reservation_job_id: int | None = None) -> dict[str, Any]:
    """Fill the XHS page and persist a prefilled history row, without clicking submit."""
    profile_dir = str(payload.get("profile_dir") or active_profile_dir())
    if manual_pending(profile_dir):
        raise PublishError("当前账号还有待人工确认的发布页面，请先确认已发布或放弃待确认记录，避免覆盖")
    if not payload.get("frozen_images"):
        payload = publishing_resources.resolve_template(payload, account_setting("local_topic_templates", [], profile_dir))
        payload = {**payload, "archive_info": make_archive_info(payload)}
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    images = payload.get("images") or []
    tags = [str(t).strip().lstrip("#") for t in payload.get("tags") or [] if str(t).strip()]
    if not title or not content or not images:
        raise PublishError("标题、正文和图片不能为空")
    validate_note_image_count(images, require_one=True)

    # Load reservations once and exclude the queue row currently being
    # submitted. Otherwise a retry would count its own slot twice and could
    # incorrectly trip the daily limit or force an unnecessary replan.
    reserved = reserved_schedule_times(profile_dir, exclude_job_id=reservation_job_id)
    schedule_time = "" if payload.get("publish_mode") == "immediate" else str(payload.get("schedule_time") or "").strip()
    if payload.get("publish_mode") == "scheduled" and not schedule_time:
        try:
            schedule_time = resolve_planned_schedule_time(
                str(payload.get("schedule_window") or ""),
                reserved_times=reserved,
                profile_dir=profile_dir,
            )
        except ValueError as exc:
            raise PublishError(str(exc)) from exc
        payload = {**payload, "schedule_time": schedule_time}
    elif schedule_time:
        try:
            schedule_time = validate_explicit_schedule_time(
                schedule_time,
                profile_dir=profile_dir,
                reserved_times=reserved,
            )
        except ValueError as exc:
            # A suggested slot may be replanned, but a manually entered slot
            # must fail loudly instead of silently changing the user's choice.
            if payload.get("schedule_auto") is True:
                try:
                    schedule_time = resolve_planned_schedule_time(
                        str(payload.get("schedule_window") or ""),
                        reserved_times=reserved,
                        profile_dir=profile_dir,
                    )
                except ValueError as replanning_exc:
                    raise PublishError(str(replanning_exc)) from replanning_exc
            else:
                raise PublishError(str(exc)) from exc
        payload = {**payload, "schedule_time": schedule_time}
    elif payload.get("publish_mode") == "immediate":
        payload = {**payload, "schedule_time": ""}

    daily_limit = int(account_setting("daily_limit", 3, payload.get("profile_dir")))
    if payload.get("automation") and AUTOMATION:
        daily_limit = int(AUTOMATION.get_plan(payload["profile_dir"])["config"]["daily_count"])
    try:
        scheduled_day = date.fromisoformat((schedule_time or date.today().isoformat())[:10])
    except ValueError:
        scheduled_day = date.today()
    usage = usage_count_for_day(scheduled_day, profile_dir)
    reserved_for_day = sum(1 for value in reserved if value[:10] == scheduled_day.isoformat())
    if usage + reserved_for_day >= daily_limit:
        raise PublishError(f"计划日已达到发布上限：{scheduled_day.isoformat()} {usage}/{daily_limit}")

    if payload.get("frozen_images"):
        verify_frozen_images(payload)
        upload_images, image_notes, fx_notes = images, [], []
    else:
        fx_params = get_fx_params(payload.get("fx_params"), profile_dir=profile_dir)
        fx_images, fx_notes = imagefx.apply_for_publish(images, fx_params, PREPARED_DIR)
        upload_images, image_notes = prepare_images_for_xhs(fx_images)
    warnings = content_warnings({**payload, "tags": tags[:10]}) + image_notes + fx_notes
    title_path = ""
    content_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix="-xhs-title.txt", encoding="utf-8", delete=False) as title_file:
            title_file.write(title)
            title_path = title_file.name
        with tempfile.NamedTemporaryFile("w", suffix="-xhs-content.txt", encoding="utf-8", delete=False) as content_file:
            content_file.write(content)
            content_path = content_file.name
        args = [
            "fill-publish",
            "--title-file",
            title_path,
            "--content-file",
            content_path,
            "--images",
            *upload_images,
        ]
        if tags:
            args += ["--tags", *tags[:10]]
        if payload.get("original", True):
            args.append("--original")
        args += ["--visibility", payload.get("visibility") or "公开可见"]
        if payload.get("album"):
            args += ["--album", str(payload.get("album"))]
        if payload.get("location"):
            args += ["--location", str(payload.get("location"))]
        if payload.get("topic_template"):
            args += ["--topic-template", str(payload.get("topic_template"))]
        if schedule_time:
            args += ["--schedule-at", schedule_time]
        if payload.get("shooting_date"):
            args += ["--shooting-date", str(payload.get("shooting_date"))]
        shooting_location = payload.get("shooting_location") or payload.get("location")
        if shooting_location:
            args += ["--shooting-location", str(shooting_location)]
        result = run_xhs_cli(args, timeout=240)
    finally:
        for path in (title_path, content_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    if not result["ok"]:
        raise PublishError((result.get("stderr") or result.get("stdout") or "填入发布页失败")[:500])
    for line in result.get("stderr", "").splitlines():
        if "WARNING" in line or "警告" in line:
            warnings.append(line[-300:])
    history_id = record_history({**payload, "tags": tags[:10], "profile_dir": profile_dir}, "prefilled", warnings)
    return {
        "ok": True,
        "history_id": history_id,
        "result": result,
        "warnings": warnings,
        "payload": payload,
        "manual_fields": {
            "album": payload.get("album") or "",
            "location": payload.get("location") or "",
            "schedule_time": payload.get("schedule_time") or "",
            "schedule_window": payload.get("schedule_window") or "",
            "shooting_date": payload.get("shooting_date") or "",
            "shooting_location": payload.get("shooting_location") or payload.get("location") or "",
            "note": "专辑/地点采用保守尝试；若页面未响应，请人工补选后再确认。",
        },
    }


def click_publish(history_id: int | None = None) -> dict[str, Any]:
    """Submit the reviewed page and mark history only after platform confirmation."""
    with BROWSER_LOCK:
        try:
            time.sleep(random.uniform(2.2, 5.5))
            result = run_xhs_cli(["click-publish"], timeout=90)
        except subprocess.TimeoutExpired as exc:
            if history_id:
                update_history_status(history_id, "unknown")
            raise PublishError("点击发布超时，平台状态不确定，请人工检查后再决定是否重试") from exc
    confirmation = result.get("json") if isinstance(result.get("json"), dict) else {}
    if result["ok"] and confirmation.get("confirmed") is True:
        if history_id:
            with db_connection() as db:
                history = db.execute("SELECT schedule_time FROM publish_history WHERE id=?", (history_id,)).fetchone()
            update_history_status(history_id, "scheduled" if history and history[0] else "accepted")
        return {"ok": True, "result": result, "confirmation": confirmation}
    if result["ok"]:
        message = "发布命令返回成功，但没有收到平台确认，状态不明，请人工检查"
        if history_id:
            update_history_status(history_id, "unknown")
        return {"ok": False, "uncertain": True, "message": message, "result": result}
    if history_id:
        update_history_status(history_id, "unknown")
    return {"ok": False, "result": result}


def verify_history_published(history_id: int, reference_time: str | None = None) -> dict[str, Any]:
    """Read the creator note manager and archive only after a unique published receipt."""
    with db_connection() as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM publish_history WHERE id=?", (int(history_id),)).fetchone()
        job = db.execute(
            "SELECT completed_at FROM publish_jobs WHERE history_id=? ORDER BY id DESC LIMIT 1",
            (int(history_id),),
        ).fetchone()
    if not row:
        raise ValueError("发布记录不存在")
    item = dict(row)
    receipt_reference_time = reference_time or (job[0] if job else "") or now_iso()
    profile = str(item.get("profile_dir") or "Default")
    expected_identity = account_identity(profile)
    with BROWSER_LOCK:
        select_profile_connection(profile)
        if expected_identity.get("user_id"):
            live_identity = read_connected_identity(XHS_SKILL_DIR)
            if live_identity.get("status") != "verified" or live_identity.get("user_id") != expected_identity["user_id"]:
                raise PublishError("当前小红书账号与这条记录不一致或登录已失效，已停止核验")
        from publish_checks import match_receipt, read_management
        receipt = match_receipt(
            read_management(XHS_SKILL_DIR),
            {
                "title": item["title"],
                "schedule_time": item.get("schedule_time") or "",
                "receipt_reference_time": receipt_reference_time,
                "visibility": item.get("visibility") or "公开可见",
            },
        )
    status = str(receipt.get("status") or "unconfirmed")
    if status in {"published", "reviewing", "scheduled", "rejected"}:
        update_history_status(history_id, status)
    archive_result = "尚未确认平台已发布，不移动素材"
    if status == "published":
        with db_connection() as db:
            saved = db.execute("SELECT archive_result FROM publish_history WHERE id=?", (int(history_id),)).fetchone()
        archive_result = (saved[0] if saved else "") or "平台已发布，但未生成归档结果"
    return {**receipt, "status": status, "archive_result": archive_result}


def update_history_status(history_id: int, status: str) -> None:
    with db_connection() as db:
        db.execute(
            "UPDATE publish_history SET status = ?, confirmed_at = ? WHERE id = ?",
            (status, now_iso() if status == "published" else None, int(history_id)),
        )
        db.commit()
    if status == "published":
        try:
            archive_history(history_id)
        except Exception as exc:
            print(f"记录 #{history_id} 归档失败：{exc}", file=sys.stderr, flush=True)


def check_task_identity(payload: dict[str, Any]) -> None:
    expected = payload.get("expected_user_id")
    if not expected:
        raise PublishError("任务未绑定小红书用户 ID，请先识别账号并重新确认任务")
    identity = read_connected_identity(XHS_SKILL_DIR)
    if identity.get("status") != "verified" or identity.get("user_id") != expected:
        raise PublishError("当前小红书账号与任务绑定账号不一致或登录失效，已阻止提交")


def defer_for_rate_limit(job: dict[str, Any]) -> bool:
    payload = job["payload"]
    now = datetime.now()
    hour_limit = max(1, int(payload.get("submission_hour_limit", 3)))
    day_limit = max(1, int(payload.get("submission_day_limit", 12)))
    with db_connection() as db:
        rows = db.execute("""SELECT started_at FROM publish_jobs WHERE profile_dir=? AND id!=?
            AND status IN ('submitted','submitting','reviewed_page','unknown') AND started_at>=?""",
            (job["profile_dir"], job["id"], (now-timedelta(days=1)).isoformat(timespec="seconds"))).fetchall()
        times = [datetime.fromisoformat(r[0]) for r in rows if r[0]]
        hour_times = sorted(t for t in times if t >= now-timedelta(hours=1))
        delays = []
        if len(hour_times) >= hour_limit:
            delays.append(hour_times[-hour_limit] + timedelta(hours=1, seconds=10))
        if len(times) >= day_limit:
            delays.append(sorted(times)[-day_limit] + timedelta(days=1, seconds=10))
        if not delays:
            return False
        payload = {**payload, "submit_after": max(delays).isoformat(timespec="seconds")}
        db.execute("UPDATE publish_jobs SET payload=?,status='queued',attempts=MAX(0,attempts-1),updated_at=? WHERE id=?",
                   (json.dumps(payload, ensure_ascii=False), now_iso(), job["id"]))
    return True


def process_queue_job(job: dict[str, Any]) -> bool:
    # One lock covers account selection, upload, final checks and submission.
    with BROWSER_LOCK:
        return _process_queue_job(job)


def _process_queue_job(job: dict[str, Any]) -> bool:
    payload = job["payload"]
    job_id = int(job["id"])
    submitted = False
    try:
        if payload.get("strict_fields"):
            if payload.get("schedule_epoch") and float(payload["schedule_epoch"]) <= time.time() + 300:
                raise PublishError("任务发布时间已过或不足 5 分钟，已停止补发，请取消后重新排期")
            if defer_for_rate_limit(job):
                return True
        switch_chrome_profile(str(job.get("profile_dir") or active_profile_dir()))
        if payload.get("strict_fields"):
            check_task_identity(payload)
        for attempt in range(2):
            try:
                prepared = prepare_publish(payload, reservation_job_id=job_id)
                break
            except (OSError, subprocess.TimeoutExpired):
                if attempt:
                    raise
                time.sleep(2)  # safe only before the final submit operation
        history_id = int(prepared["history_id"])
        if payload.get("strict_fields"):
            from publish_checks import verify
            verify(XHS_SKILL_DIR, payload, apply_mentions=True)
            check_task_identity(payload)
        update_job(job_id, history_id=history_id, status="reviewed_page")
        submitted = True
        clicked = click_publish(history_id)
        if not clicked["ok"]:
            message = (
                clicked.get("message")
                or clicked["result"].get("stderr")
                or clicked["result"].get("stdout")
                or "点击发布失败"
            )[:500]
            update_job(job_id, status="unknown", last_error=message)
            if AUTOMATION and payload.get("automation"):
                AUTOMATION.incident(job["profile_dir"], f"任务 #{job_id} 提交结果不明：{message}。请先到平台核对，禁止直接重试。")
            else:
                db_set_setting("queue_enabled", False)
                mac_notifications.add_alert(sys.modules[__name__], job["profile_dir"], "", "publish_error", f"任务 #{job_id} 提交结果不明，请到平台核对。")
            return False
        update_job(job_id, status="submitted", completed_at=now_iso(), last_error="")
        return True
    except Exception as exc:
        message = f"任务 #{job_id}：{exc}"
        update_job(job_id, status="unknown" if submitted else "failed", last_error=message[:1000])
        if AUTOMATION and payload.get("automation"):
            AUTOMATION.incident(job["profile_dir"], message)
        else:
            db_set_setting("queue_enabled", False)
            mac_notifications.add_alert(sys.modules[__name__], job["profile_dir"], "", "publish_error", message)
        return False


def queue_worker_loop() -> None:
    while not QUEUE_STOP.is_set():
        if not bool(db_get_setting("queue_enabled", False)):
            QUEUE_WAKE.wait(2)
            QUEUE_WAKE.clear()
            continue
        job = claim_next_job()
        if not job:
            QUEUE_WAKE.wait(2)
            QUEUE_WAKE.clear()
            continue
        if not process_queue_job(job):
            continue
        if bool(db_get_setting("queue_enabled", False)):
            QUEUE_WAKE.wait(random.uniform(QUEUE_GAP_MIN_SECONDS, QUEUE_GAP_MAX_SECONDS))
            QUEUE_WAKE.clear()


def start_queue_worker() -> None:
    global QUEUE_THREAD
    if QUEUE_THREAD and QUEUE_THREAD.is_alive():
        return
    QUEUE_STOP.clear()
    QUEUE_THREAD = threading.Thread(target=queue_worker_loop, name="xhs-queue-worker", daemon=True)
    QUEUE_THREAD.start()


def has_active_plans() -> bool:
    with db_connection() as db:
        return bool(db.execute("SELECT COUNT(*) FROM automation_plans WHERE enabled=1").fetchone()[0])


def update_install_block_reason() -> str:
    if has_active_plans():
        return "请先暂停自动发布计划，再安装更新"
    if bool(db_get_setting("queue_enabled", False)) or any(
        row["status"] in {"submitting", "reviewed_page"} for row in queue_rows()
    ):
        return "发布队列正在运行，请先暂停并等待当前任务结束后再更新"
    return ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{now_iso()}] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/automation":
            profile = urllib.parse.parse_qs(parsed.query).get("profile_dir", [active_profile_dir()])[0]
            json_response(self, automation_engine().snapshot(profile))
            return
        if path == "/api/notifications/options":
            import mac_runtime
            json_response(self, {"ok": True, "sounds": list(mac_notifications.SOUNDS), "voices": mac_notifications.voices(), "runtime": mac_runtime.status(sys.modules[__name__])})
            return
        if path == "/":
            html = (APP_ROOT / "app" / "templates" / "index.html").read_text("utf-8")
            text_response(self, html, "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            file_path = APP_ROOT / "app" / path.lstrip("/")
            if not file_path.exists():
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")  # 静态资源改动后立即生效
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/bridge-pair":
            body = "<!doctype html><meta charset='utf-8'><title>连接 Chrome 账号</title><h1>正在连接此 Chrome 用户资料</h1><p>连接成功后可关闭此页。如果一直未连接，请在此用户资料的 chrome://extensions 中重新加载新版 XHS Bridge，再回软件重试。</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/config":
            root_path = db_get_setting("root_path", str(DEFAULT_TEST_ROOT))
            model_config = public_text_model_config(text_model_config(active_profile_dir()))
            json_response(
                self,
                {
                    "ok": True,
                    "config": {
                        "root_path": root_path,
                        "default_album": db_get_setting("default_album", ""),
                        "default_location": db_get_setting("default_location", ""),
                        "default_schedule_time": db_get_setting("default_schedule_time", ""),
                        "default_schedule_window": db_get_setting("default_schedule_window", ""),
                        "schedule_windows": db_get_setting("schedule_windows", ["早上 10:00", "晚上 22:00-24:00"]),
                        "topic_templates": db_get_setting("topic_templates", ["演员资料", "艺考录制"]),
                        "local_topic_templates": db_get_setting("local_topic_templates", []),
                        "publishing_resources": db_get_setting(f"publishing_resources:{active_profile_dir()}", {}),
                        "manual_pending": manual_pending(),
                        "text_provider": db_get_setting("text_provider", "deepseek"),
                        "text_base_url": db_get_setting("text_base_url", ""),
                        "pptoken_model": db_get_setting("pptoken_model", ""),
                        "has_pptoken_key": bool(db_get_setting("pptoken_api_key", "")),
                        "text_model": model_config,
                        "text_provider_label": model_config["provider_label"],
                        "text_model_label": model_config["model_label"],
                        "daily_limit": db_get_setting("daily_limit", 3),
                        "visibility": db_get_setting("visibility", "公开可见"),
                        "publish_mode": db_get_setting("publish_mode", "scheduled"),
                        "original_enabled": db_get_setting("original_enabled", True),
                        "copy_engine": db_get_setting("copy_engine", "auto"),
                        "moments_tag": db_get_setting("moments_tag", ""),
                        "editor_app_path": db_get_setting("editor_app_path", ""),
                        "deepseek_model": db_get_setting("deepseek_model", "deepseek-v4-pro"),
                        "has_deepseek_key": bool(db_get_setting("deepseek_api_key", "") or os.environ.get("DEEPSEEK_API_KEY")),
                        "today_usage_count": today_usage_count(),
                        "xhs_skill_dir": str(XHS_SKILL_DIR),
                        "active_profile": active_profile_dir(),
                        "queue_enabled": bool(db_get_setting("queue_enabled", False)),
                        "app_version": APP_VERSION,
                    },
                },
            )
            return
        if path == "/api/version":
            json_response(self, {"ok": True, "version": APP_VERSION})
            return
        if path == "/api/update/check":
            try:
                json_response(self, check_for_update(APP_VERSION))
            except UpdateError as exc:
                error_response(self, str(exc), 502)
            return
        if path == "/api/status":
            daily_limit = int(db_get_setting("daily_limit", 3))
            json_response(
                self,
                {
                    "ok": True,
                    "status": {
                        "today_usage_count": today_usage_count(),
                        "daily_limit": daily_limit,
                        "remaining_today": max(0, daily_limit - today_usage_count()),
                        "test_root_exists": DEFAULT_TEST_ROOT.exists(),
                        "test_root": str(DEFAULT_TEST_ROOT),
                    },
                },
            )
            return
        if path == "/api/history":
            json_response(self, {"ok": True, "items": history_rows()})
            return
        if path == "/api/queue":
            json_response(
                self,
                {
                    "ok": True,
                    "items": queue_rows(),
                    "enabled": bool(db_get_setting("queue_enabled", False)),
                },
            )
            return
        if path == "/api/profile":
            with BROWSER_LOCK:
                profile_dir = urllib.parse.parse_qs(parsed.query).get("profile_dir", [active_profile_dir()])[0]
                data = {"ok": True, "profile": get_account_profile(profile_dir), "profile_dir": profile_dir,
                        "prompts": get_copy_prompts(profile_dir)}
            json_response(self, data)
            return
        if path == "/api/logo/settings":
            profile = urllib.parse.parse_qs(parsed.query).get("profile_dir", [active_profile_dir()])[0]
            json_response(self, {"ok": True, "settings": get_fx_params(profile_dir=profile)})
            return
        if path == "/api/image/preview":
            query = urllib.parse.parse_qs(parsed.query)
            p = Path(query.get("path", [""])[0])
            if not p.exists() or not p.is_file():
                self.send_error(404)
                return
            raw_overrides = {
                key: query.get(key, [None])[0]
                for key in (
                    "fx_enabled",
                    "logo_enabled",
                    "logo_position",
                    "logo_margin",
                    "logo_scale",
                    "logo_opacity",
                    "watermark_enabled",
                    "watermark_text",
                    "watermark_position",
                    "watermark_opacity",
                    "watermark_scale",
                    "output_mode",
                )
            }
            bool_keys = {"fx_enabled", "logo_enabled", "watermark_enabled"}
            overrides = {}
            for key, value in raw_overrides.items():
                if value is None:
                    continue
                if key in bool_keys:
                    overrides[key] = str(value).lower() in ("1", "true", "yes", "on")
                else:
                    overrides[key] = value
            params = get_fx_params(overrides, profile_dir=query.get("profile_dir", [None])[0])
            try:
                data = imagefx.render_preview(p, params)
            except Exception:
                self.send_error(500, "Image composition failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/image":
            query = urllib.parse.parse_qs(parsed.query)
            p = Path(query.get("path", [""])[0])
            if not p.exists() or not p.is_file():
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(str(p))[0] or "image/jpeg"
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        try:
            self._post()
        except PublishError as exc:
            error_response(self, str(exc), 409)
        except UpdateError as exc:
            error_response(self, str(exc), 502)
        except ValueError as exc:
            error_response(self, str(exc), 400)
        except subprocess.TimeoutExpired:
            error_response(self, "命令执行超时，发布页可能卡住了", 504)
        except Exception as exc:
            error_response(self, str(exc), 500)

    def import_material_image(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("图片数据长度无效") from exc
        if length <= 0:
            raise ValueError("没有收到图片数据")
        if length > MAX_IMPORT_IMAGE_BYTES:
            raise ValueError("单张拖入图片不能超过 100MB")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("图片数据未完整接收，请重新拖入")
        filename = urllib.parse.unquote(self.headers.get("X-File-Name", ""))
        target = store_imported_image(filename, raw)
        json_response(self, {
            "ok": True,
            "path": str(target),
            "folder_path": str(target.parent),
        })

    def _post(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/material/import":
            self.import_material_image()
            return
        payload = read_json(self)
        if path == "/api/profile/save":
            profile = str(payload.get("profile_dir") or active_profile_dir())
            if profile != active_profile_dir():
                raise PublishError("账号已切换，请重新打开账号画像")
            prompts = payload.get("prompts") or {}
            if not isinstance(prompts, dict):
                raise ValueError("提示词格式无效")
            saved = {key: str(prompts.get(key) or default).strip()[:10000] for key, default in DEFAULT_COPY_PROMPTS.items()}
            result = save_manual_profile(profile, payload)
            db_set_setting(f"copy_prompts:{profile}", saved)
            json_response(self, {"ok": True, "profile_dir": profile, "profile": result, "prompts": saved})
            return
        if path == "/api/publish/preferences":
            profile = str(payload.get("profile_dir") or active_profile_dir())
            if profile != active_profile_dir():
                raise PublishError("账号已切换，请刷新后重新设置发布方式")
            save_publish_preferences(profile, payload)
            json_response(self, {"ok": True})
            return
        if path.startswith("/api/automation/") or path.startswith("/api/notifications/"):
            self.automation_post(path, payload)
            return
        if path == "/api/update/install":
            block_reason = update_install_block_reason()
            if block_reason:
                error_response(self, block_reason, 409)
                return
            state = check_for_update(APP_VERSION)
            release = state["release"]
            requested_version = str(payload.get("version") or "").strip().removeprefix("v")
            if not state["update_available"] or requested_version != release["version"]:
                error_response(self, "版本信息已变化，请重新检查更新", 409)
                return
            launch_update(release, APP_ROOT, os.getpid())
            json_response(self, {"ok": True, "accepted": True, "version": release["version"]}, 202)
            QUEUE_STOP.set()
            QUEUE_WAKE.set()
            if HTTP_SERVER:
                threading.Timer(0.4, HTTP_SERVER.shutdown).start()
            return
        if path == "/api/config":
            if payload.get("profile_dir") and payload["profile_dir"] != active_profile_dir():
                raise PublishError("后台任务已切换账号，请刷新后再保存当前账号设置")
            if "original_enabled" in payload and type(payload["original_enabled"]) is not bool:
                raise ValueError("原创声明设置必须为布尔值")
            if "default_schedule_window" in payload:
                preferred_window = str(payload.get("default_schedule_window") or "").strip()
                schedule_windows = payload.get("schedule_windows")
                if schedule_windows is None:
                    schedule_windows = db_get_setting("schedule_windows", ["早上 10:00", "晚上 22:00-24:00"])
                if not isinstance(schedule_windows, list):
                    raise ValueError("发布时间段列表格式无效")
                if preferred_window and preferred_window not in {str(item) for item in schedule_windows if item}:
                    raise ValueError("默认时间段必须来自当前账号已保存的时间段列表")
            local_templates = None
            if "local_topic_templates" in payload:
                local_templates = publishing_resources.normalize_templates(payload["local_topic_templates"])
            if payload.get("text_provider", "deepseek") not in {"deepseek", "pptoken"}:
                raise ValueError("模型平台无效")
            try:
                save_cover_settings(payload)
            except cover_service.CoverError as exc:
                error_response(self, str(exc), exc.status)
                return
            for key in (
                "root_path",
                "default_album",
                "default_location",
                "default_schedule_time",
                "default_schedule_window",
                "schedule_windows",
                "topic_templates",
                "daily_limit",
                "visibility",
                "copy_engine",
                "original_enabled",
                "deepseek_model",
                "text_provider",
                "text_base_url",
                "pptoken_model",
                "moments_tag",
            ):
                if key in payload:
                    db_set_setting(key, payload[key])
            if payload.get("deepseek_api_key"):
                db_set_setting("deepseek_api_key", payload["deepseek_api_key"])
            if payload.get("pptoken_api_key"):
                db_set_setting("pptoken_api_key", payload["pptoken_api_key"])
            if local_templates is not None:
                db_set_setting("local_topic_templates", local_templates)
            json_response(self, {"ok": True})
            return
        if path == "/api/publishing/resources/sync":
            profile = str(payload.get("profile_dir") or active_profile_dir())
            json_response(self, {"ok": True, "profile_dir": profile, "resources": sync_publishing_resources(profile)})
            return
        if path == "/api/model/test":
            profile = str(payload.get("profile_dir") or active_profile_dir())
            if profile != active_profile_dir():
                raise PublishError("账号已切换，请刷新后重新测试模型")
            setting_keys = {
                "text_provider", "text_base_url", "pptoken_model", "pptoken_api_key",
                "deepseek_model", "deepseek_api_key",
            }
            temporary_settings = {key: payload[key] for key in setting_keys if key in payload}
            json_response(self, check_text_model_connection(profile, temporary_settings))
            return
        if path == "/api/schedule/preview":
            window = payload.get("schedule_window") or ""
            if not window:
                error_response(self, "请选择发布时间段", 400)
                return
            schedule_time = resolve_planned_schedule_time(
                str(window),
                profile_dir=str(payload.get("profile_dir") or active_profile_dir()),
            )
            json_response(self, {"ok": True, "schedule_time": schedule_time})
            return
        if path == "/api/queue/enqueue":
            queued_payload = payload.get("payload") or {}
            # Queue requests carry the actual task under ``payload``. Validate
            # both contexts before touching reservations or freezing files so a
            # stale page cannot enqueue work for another Chrome account.
            profile = require_active_profile(payload)
            nested_profile = require_active_profile(queued_payload)
            if nested_profile != profile:
                raise PublishError("请求中的账号资料不一致，请刷新后再提交")
            queued_payload = {**queued_payload, "profile_dir": profile}
            if manual_pending(profile):
                raise PublishError("当前账号还有待人工确认的发布页面，请先确认已发布或放弃待确认记录，避免覆盖")
            if not queued_payload.get("images") or not queued_payload.get("title") or not queued_payload.get("content"):
                error_response(self, "入队前必须确认图片、标题和正文", 400)
                return
            try:
                schedule_time = resolve_queue_schedule(
                    queued_payload,
                    str(payload.get("schedule_time") or queued_payload.get("schedule_time") or ""),
                )
            except ValueError as exc:
                error_response(self, str(exc), 409)
                return
            identity = account_identity(profile)
            if not identity.get("user_id"):
                raise PublishError("请先检测登录并识别当前小红书用户 ID")
            queued_payload = freeze_payload({**queued_payload, "schedule_time": schedule_time, "schedule_epoch": datetime.fromisoformat(schedule_time).timestamp(), "expected_user_id": identity["user_id"], "strict_fields": True})
            job = enqueue_job(queued_payload, schedule_time)
            db_set_setting("queue_enabled", True)
            start_queue_worker()
            QUEUE_WAKE.set()
            json_response(self, {"ok": True, "job": job})
            return
        if path == "/api/queue/start":
            pending = [row for row in queue_rows() if row["status"] == "queued"]
            if not pending:
                error_response(self, "没有待提交的已确认任务", 400)
                return
            db_set_setting("queue_enabled", True)
            start_queue_worker()
            QUEUE_WAKE.set()
            json_response(self, {"ok": True, "items": queue_rows(), "enabled": True})
            return
        if path == "/api/queue/pause":
            db_set_setting("queue_enabled", False)
            QUEUE_WAKE.set()
            json_response(self, {"ok": True, "items": queue_rows(), "enabled": False})
            return
        if path == "/api/scan":
            root = payload.get("root_path") or db_get_setting("root_path", "")
            candidates = scan_folders(root)
            json_response(self, {"ok": True, "items": [asdict(c) for c in candidates[:80]]})
            return
        if path == "/api/hot/open":
            profile = str(payload.get("profile_dir") or "")
            keyword = str(payload.get("keyword") or "").strip()
            if profile != active_profile_dir() or profile not in {p["dir"] for p in chrome_profiles()}:
                raise ValueError("当前 Chrome 账号已变化，请重新选择")
            if not keyword or len(keyword) > 200:
                raise ValueError("请输入不超过 200 字的搜索关键词")
            chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            url = "https://www.xiaohongshu.com/search_result?" + urllib.parse.urlencode({"keyword": keyword})
            subprocess.Popen([chrome, f"--profile-directory={profile}", "--new-tab", url])
            json_response(self, {"ok": True})
            return
        if path == "/api/recommend":
            root = payload.get("root_path") or db_get_setting("root_path", "")
            candidates = scan_folders(root)
            if not candidates:
                error_response(self, "没有找到包含图片的素材文件夹", 404)
                return
            candidate = candidates[0]
            data = asdict(candidate)
            data["selected_images"] = choose_image_group(candidate.images, int(payload.get("max_images") or MAX_NOTE_IMAGES))
            json_response(self, {"ok": True, "item": data})
            return
        if path == "/api/polish":
            json_response(self, {"ok": True, "copy": polish_copy(payload)})
            return
        if path == "/api/generate":
            json_response(self, {"ok": True, "copy": generate_copy(payload)})
            return
        if path == "/api/choose-app":
            script = 'POSIX path of (choose file with prompt "选择本地编辑软件（如 Photoshop.app）")'
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                # 用户取消选择不算错误
                json_response(self, {"ok": True, "path": "", "cancelled": True})
                return
            app_path = proc.stdout.strip()
            if app_path:
                db_set_setting("editor_app_path", app_path)
            json_response(self, {"ok": True, "path": app_path})
            return
        if path == "/api/choose-folder":
            script = 'POSIX path of (choose folder with prompt "选择素材文件夹")'
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                json_response(self, {"ok": True, "path": "", "cancelled": True})
                return
            folder = proc.stdout.strip().rstrip("/")
            json_response(self, {"ok": True, "path": folder})
            return
        if path == "/api/open-editor":
            paths = [p for p in (payload.get("paths") or []) if p]
            if not paths:
                error_response(self, "没有可打开的图片", 400)
                return
            for p in paths:
                if not os.path.exists(p):
                    error_response(self, f"文件不存在：{p}", 400)
                    return
            app_path = payload.get("app_path") or db_get_setting("editor_app_path", "")
            if app_path:
                cmd = ["open", "-a", app_path] + paths
            else:
                cmd = ["open"] + paths
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                error_response(self, proc.stderr or "打开本地软件失败", 500)
                return
            json_response(self, {"ok": True, "opened": len(paths), "app": app_path})
            return
        if path == "/api/profile":
            max_count = int(payload.get("max_count") or 20)
            result = generate_account_profile(
                max_count=max_count,
                manual_keywords=str(payload.get("manual_keywords") or "").strip(),
                manual_topics=str(payload.get("manual_topics") or "").strip(),
                manual_notes=str(payload.get("manual_notes") or "").strip(),
                expected_profile=str(payload.get("profile_dir") or "") or None,
                source_mode=str(payload.get("source_mode") or "recent"),
            )
            if result.get("ok"):
                json_response(self, result)
            else:
                error_response(self, result.get("error") or "生成画像失败", 500)
            return
        if path == "/api/validate":
            validate_note_image_count(payload.get("images") or [])
            if payload.get("publish_mode") == "scheduled" and not payload.get("schedule_time"):
                try:
                    payload = {
                        **payload,
                        "schedule_time": resolve_planned_schedule_time(
                            str(payload.get("schedule_window") or ""),
                            profile_dir=str(payload.get("profile_dir") or active_profile_dir()),
                        ),
                    }
                except ValueError as exc:
                    json_response(
                        self,
                        {
                            "ok": True,
                            "warnings": [str(exc)],
                            "today_usage_count": today_usage_count(),
                            "daily_limit": int(db_get_setting("daily_limit", 3)),
                        },
                    )
                    return
            warnings = content_warnings(payload)
            json_response(
                self,
                {
                    "ok": True,
                    "warnings": warnings,
                    "schedule_time": payload.get("schedule_time") or "",
                    "today_usage_count": today_usage_count(),
                    "daily_limit": int(db_get_setting("daily_limit", 3)),
                },
            )
            return
        if path == "/api/xhs/check-login":
            with BROWSER_LOCK:
                result = run_xhs_cli(["check-login"], timeout=35)
                identity = refresh_account_identity() if result.get("ok") else None
            json_response(self, {"ok": result["ok"], "result": result, "account": identity})
            return
        if path == "/api/moments/publish":
            text = (payload.get("text") or "").strip()
            images = payload.get("images") or []
            if not text and not images:
                error_response(self, "朋友圈文案和图片不能都为空", 400)
                return
            moments_payload = {
                "images": images[:9],
                "text": text,
                "tag": (payload.get("tag") or db_get_setting("moments_tag", "")).strip(),
                "dryRun": payload.get("dryRun", True),
            }
            result = run_moments_cli(moments_payload, timeout=280)
            data = result.get("json") or {}
            if result["ok"]:
                json_response(self, {"ok": True, "result": data})
            else:
                error_response(self, (data.get("error") or result.get("stderr") or "朋友圈引擎执行失败")[:300], 500)
            return
        if path == "/api/logo/upload":
            if payload.get("profile_dir") and payload["profile_dir"] != active_profile_dir():
                raise PublishError("后台任务已切换浏览器账号，请稍后重新选择账号再上传 LOGO")
            kind = "watermark" if payload.get("kind") == "watermark" else "logo"
            data_b64 = str(payload.get("data_base64") or "")
            if "," in data_b64:  # data:image/png;base64,xxx
                data_b64 = data_b64.split(",", 1)[1]
            if not data_b64:
                error_response(self, "没有收到图片数据", 400)
                return
            import base64

            try:
                raw = base64.b64decode(data_b64)
            except Exception:
                error_response(self, "图片数据解码失败", 400)
                return
            imagefx.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
            account_key = hashlib.sha256(active_profile_dir().encode()).hexdigest()[:12]
            target = imagefx.ASSETS_DIR / f"{account_key}-{kind}.png"
            # 自动裁掉透明边缘：用户 PNG 画布可能远大于实际图形，
            # 不裁剪会导致叠加时可见内容极小（看起来像"没显示"）
            try:
                from PIL import Image as PILImage

                im = PILImage.open(io.BytesIO(raw))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bbox = im.getchannel("A").getbbox()
                    if bbox:
                        im = im.crop(bbox)
                elif im.mode != "RGB":
                    im = im.convert("RGB")
                im.save(target, "PNG")
            except Exception:
                target.write_bytes(raw)  # 解析失败存原始字节
            key = f"{kind}_path"
            db_set_setting(key, str(target))
            json_response(self, {"ok": True, "path": str(target)})
            return
        if path == "/api/logo/settings":
            if payload.get("profile_dir") and payload["profile_dir"] != active_profile_dir():
                raise PublishError("后台任务已切换浏览器账号，请稍后重新选择账号再保存水印")
            for key in FX_KEYS:
                if key in payload:
                    db_set_setting(key, payload[key])
            json_response(self, {"ok": True, "settings": get_fx_params()})
            return
        if path == "/api/collage":
            images = payload.get("images") or []
            if len(images) < 2:
                error_response(self, "拼图至少选择 2 张图片", 400)
                return
            layout = str(payload.get("layout") or "2x2")
            gap = int(payload.get("gap") or 8)
            bg = str(payload.get("bg") or "#ffffff")
            out_name = f"collage-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jpg"
            out_path = PREPARED_DIR / out_name
            try:
                result_path = imagefx.make_collage(images, layout, gap, bg, out_path, params=get_fx_params())
            except Exception as exc:
                error_response(self, str(exc), 500)
                return
            json_response(self, {"ok": True, "path": str(result_path)})
            return
        if path == "/api/save-generated":
            # 保存前端 canvas 合成的拼图/AI封面到指定素材文件夹
            import base64

            raw = str(payload.get("data_base64") or "")
            folder = str(payload.get("folder") or "").strip()
            filename = str(payload.get("filename") or "").strip()
            if not raw or not folder or not filename:
                error_response(self, "缺少图片数据、目录或文件名", 400)
                return
            Path(folder).mkdir(parents=True, exist_ok=True)  # AI封面 等子目录不存在时自动创建
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[1]
            if re.search(r"[/\\:\*\?\"<>\|]", filename):
                error_response(self, "文件名含非法字符", 400)
                return
            out_path = os.path.join(folder, filename)
            try:
                with open(out_path, "wb") as f:
                    f.write(base64.b64decode(raw))
            except Exception as exc:
                error_response(self, f"保存失败：{exc}", 500)
                return
            json_response(self, {"ok": True, "path": out_path})
            return
        if path == "/api/accounts/list":
            json_response(self, account_list())
            return
        if path == "/api/accounts/refresh":
            if not BROWSER_LOCK.acquire(blocking=False):
                error_response(self, "浏览器正在处理任务，请稍后刷新账号资料", 409)
                return
            try:
                result = refresh_account_identity(str(payload.get("profile") or active_profile_dir()))
            except PublishError as exc:
                error_response(self, str(exc), 409)
                return
            finally:
                BROWSER_LOCK.release()
            json_response(self, result)
            return
        if path == "/api/accounts/switch":
            profile_dir = str(payload.get("profile") or "").strip()
            if bool(db_get_setting("queue_enabled", False)) and any(
                row["status"] in {"queued", "submitting", "reviewed_page"} for row in queue_rows()
            ):
                error_response(self, "队列正在提交，请先暂停队列再切换账号", 409)
                return
            try:
                with BROWSER_LOCK:
                    switched = switch_chrome_profile(profile_dir)
                    identity = refresh_account_identity(profile_dir)
            except PublishError as exc:
                error_response(self, str(exc), 502)
                return
            json_response(
                self,
                {
                    "ok": True,
                    "ready": switched["ready"],
                    "profile": profile_dir,
                    "identity": identity["identity"],
                    "login_stdout": switched["login"].get("stdout", "")[-200:],
                },
            )
            return
        if path == "/api/accounts/logout":
            # 退出当前账号：清掉当前 Chrome Profile 的小红书登录 Cookie（账号列表不受影响）
            with BROWSER_LOCK:
                profile_dir = active_profile_dir()
                result = run_xhs_cli(["delete-cookies"], timeout=30)
                if result["ok"]:
                    clear_account_identity(profile_dir)
            if result["ok"]:
                json_response(self, {"ok": True, "result": result})
            else:
                error_response(self, (result.get("stderr") or result.get("stdout") or "退出失败")[:300], 502)
            return
        if path == "/api/cover/config":
            # AI封面：平台/模型清单与本机保存的接入配置
            ensure_ai_defaults()
            base = db_get_setting("apiyi_base") or APIYI_BASE_DEFAULT
            key = db_get_setting("apiyi_key") or ""
            json_response(
                self,
                {
                    "ok": True,
                    "platforms": [{"id": "apiyi", "label": "API易"}],
                    "models": APIYI_MODELS,
                    "base_url": base,
                    "key_masked": mask_key(key),
                    "key_ready": bool(key),
                    "templates": db_get_setting("cover_templates") or COVER_TEMPLATES_DEFAULT,
                    "default_model": APIYI_MODELS[0]["id"],
                },
            )
            return
        if path == "/api/cover/templates":
            templates = [str(t).strip() for t in (payload.get("templates") or []) if str(t).strip()]
            db_set_setting("cover_templates", templates[:50])
            json_response(self, {"ok": True, "templates": templates[:50]})
            return
        if path == "/api/cover/generate":
            ensure_ai_defaults()
            try:
                if payload.get("platform", "apiyi") != "apiyi":
                    raise cover_service.CoverError("当前 AI 封面仅支持 API易，请重新选择平台", 400)
                result = cover_service.generate_cover(
                    payload, db_get_setting("apiyi_key") or "",
                    db_get_setting("apiyi_base") or APIYI_BASE_DEFAULT, _ssl_context(),
                )
            except cover_service.CoverError as exc:
                error_response(self, str(exc), exc.status)
                return
            json_response(self, result)
            return
        if path == "/api/xhs/fill-publish":
            self.fill_publish(payload)
            return
        if path == "/api/xhs/publish-now":
            profile = require_active_profile(payload)
            if manual_pending(profile):
                raise PublishError("当前账号还有待人工确认的发布页面，请先确认已发布或放弃待确认记录，避免覆盖")
            identity = account_identity(profile)
            if not identity.get("user_id"):
                raise PublishError("请先识别当前小红书账号")
            frozen = freeze_payload({**payload, "profile_dir": profile, "publish_mode": "immediate", "schedule_time": "", "strict_fields": True, "expected_user_id": identity["user_id"]})
            job = enqueue_job(frozen, "")
            db_set_setting("queue_enabled", True)
            start_queue_worker()
            QUEUE_WAKE.set()
            json_response(self, {"ok": True, "job": job})
            return
        if path == "/api/xhs/click-publish":
            history_id = int(payload.get("history_id") or 0)
            if history_id <= 0:
                error_response(self, "缺少有效的历史记录 ID", 400)
                return
            result = click_publish(history_id)
            if result["ok"]:
                json_response(self, result)
            else:
                error_response(
                    self,
                    (
                        result.get("message")
                        or result["result"].get("stderr")
                        or result["result"].get("stdout")
                        or "点击发布失败"
                    )[:300],
                    502,
                )
            return
        if path == "/api/history/confirm":
            item_id = int(payload.get("id"))
            with db_connection() as db:
                row = db.execute("SELECT status,profile_dir FROM publish_history WHERE id=?", (item_id,)).fetchone()
            if not row or row[0] not in ("prefilled", "unknown", "scheduled", "accepted", "reviewing", "published"):
                raise ValueError("记录不存在或当前状态不能确认发布")
            receipt = verify_history_published(item_id)
            if receipt["status"] not in {"published", "reviewing", "scheduled"}:
                raise PublishError(f"笔记管理尚未确认发布：{receipt.get('reason') or receipt['status']}")
            clear_manual_pending(item_id, row[1])
            json_response(self, {"ok": True, **receipt})
            return
        if path == "/api/history/discard-pending":
            profile = str(payload.get("profile_dir") or active_profile_dir())
            pending = manual_pending(profile)
            if not pending or int(payload.get("id") or 0) != int(pending["history_id"]):
                raise ValueError("待确认记录已变化，请刷新")
            update_history_status(int(pending["history_id"]), "abandoned")
            clear_manual_pending(int(pending["history_id"]), profile)
            json_response(self, {"ok": True})
            return
        error_response(self, "未知接口", 404)

    def fill_publish(self, payload: dict[str, Any]) -> None:
        try:
            with BROWSER_LOCK:
                profile = str(payload.get("profile_dir") or active_profile_dir())
                if profile != active_profile_dir():
                    raise PublishError("账号已切换，请重新填入")
                result = prepare_publish(payload)
                db_set_setting(f"manual_pending:{profile}", {"history_id": result["history_id"],
                    "title": payload.get("title"), "created_at": now_iso()})
            json_response(self, result)
        except PublishError as exc:
            error_response(self, str(exc), 400)
        return

    def automation_post(self, path, payload):
        engine = automation_engine()
        profile = str(payload.get("profile_dir") or active_profile_dir())
        if path == "/api/automation/save":
            engine.save_plan(profile, payload.get("config") or {})
        elif path == "/api/automation/bind":
            engine.bind_identity(profile)
        elif path == "/api/automation/enable":
            engine.set_enabled(profile, bool(payload.get("enabled")))
        elif path == "/api/automation/build":
            engine.build_batch(profile, payload.get("limit", 10))
        elif path == "/api/automation/approve":
            engine.approve(profile, payload.get("ids") or [])
        elif path == "/api/automation/cancel":
            engine.cancel(profile, payload["id"])
        elif path == "/api/automation/resolve":
            engine.resolve(profile, payload["id"], bool(payload.get("accepted")))
        elif path == "/api/automation/runtime":
            import mac_runtime
            mac_runtime.configure(sys.modules[__name__], bool(payload.get("login_start")), bool(payload.get("prevent_sleep")))
        elif path == "/api/notifications/save":
            mac_notifications.save_settings(sys.modules[__name__], payload)
        elif path == "/api/notifications/ack":
            mac_notifications.acknowledge(sys.modules[__name__], payload["id"])
        elif path == "/api/notifications/preview":
            config = {**mac_notifications.settings(sys.modules[__name__]), **{k: payload[k] for k in ("audio", "sound", "voice", "message", "burst_count") if k in payload}}
            config["burst_count"] = max(1, min(3, int(config["burst_count"])))
            if config["audio"] not in {"sound", "speech", "both", "silent", "custom"} or config["sound"] not in mac_notifications.SOUNDS:
                raise ValueError("提醒方式无效")
            mac_notifications.deliver(sys.modules[__name__], {"message": "这是一条测试提醒。系统通知是否显示取决于你的 macOS 通知设置。", "profile_dir": "提醒试听"}, config)
        elif path == "/api/notifications/import":
            script = 'POSIX path of (choose file with prompt "选择提醒录音（AIFF/WAV/MP3/M4A）")'
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=180)
            if proc.returncode:
                raise ValueError("已取消选择录音")
            mac_notifications.import_audio(sys.modules[__name__], proc.stdout.strip())
        else:
            raise ValueError("未知自动发布操作")
        json_response(self, engine.snapshot(profile))



def automation_engine() -> AutomationEngine:
    global AUTOMATION
    if AUTOMATION is None:
        AUTOMATION = AutomationEngine(sys.modules[__name__])
    return AUTOMATION


def main() -> None:
    global HTTP_SERVER
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5818, type=int)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    HTTP_SERVER = server
    init_db()
    start_queue_worker()
    automation_engine().start()
    import mac_runtime
    mac_runtime.apply_awake(sys.modules[__name__])
    print(f"XHS Assistant running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
