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
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
APP_VERSION = "2026.11.5"


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
QUEUE_RESERVING_STATUSES = ("queued", "submitting", "reviewed_page")


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
        if db.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
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
        db.commit()


def db_get_setting(key: str, default: Any = None) -> Any:
    with db_connection() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return row[0]


def db_set_setting(key: str, value: Any) -> None:
    with db_connection() as db:
        db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        db.commit()


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
    "watermark_enabled",
    "watermark_text",
    "watermark_path",
    "watermark_position",
    "watermark_opacity",
    "watermark_scale",
    "output_mode",
)


def get_fx_params(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    stored = {key: db_get_setting(key) for key in FX_KEYS}
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
            if p.stem.endswith("-logo") or p.stem.endswith("-xhs"):
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
        target = PREPARED_DIR / f"{p.stem}-xhs.jpg"
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
        return [candidate] if candidate > now_ref else []

    start_dt = _datetime_from_day_and_minute(day, start)
    end_day = day if end > start else day + timedelta(days=1)
    end_dt = _datetime_from_day_and_minute(end_day, end)
    # Treat 24:00 as the end boundary, not as a second slot on the next day.
    latest = end_dt - timedelta(minutes=1) if end == 24 * 60 else end_dt
    earliest = start_dt
    if day == now_ref.date():
        earliest = max(earliest, now_ref + timedelta(minutes=1))
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
    daily_limit = max(1, int(db_get_setting("daily_limit", 3)))
    parsed = parse_schedule_window(raw_window)
    for offset in range(31):
        day = now_ref.date() + timedelta(days=offset)
        reference = now_ref if offset == 0 else datetime.combine(day, dt_time.min) - timedelta(minutes=1)
        candidates = _planned_candidates_for_day(parsed, day, reference, random.Random(offset * 97 + 17))
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
    if selected <= now_ref:
        raise ValueError("发布时间必须晚于当前时间")
    profile = profile_dir or active_profile_dir()
    reserved_list = reserved_schedule_times(profile) if reserved_times is None else reserved_times
    normalized_reserved = {normalize_schedule_time(value) for value in reserved_list}
    if normalized in normalized_reserved:
        raise ValueError("这个账号的该发布时间已被队列占用，请换一个时间")
    daily_limit = max(1, int(db_get_setting("daily_limit", 3)))
    same_day = sum(1 for value in reserved_list if normalize_schedule_time(value)[:10] == normalized[:10])
    if usage_count_for_day(selected.date(), profile) + same_day >= daily_limit:
        raise ValueError(f"计划日已达到发布上限：{selected.date().isoformat()} {daily_limit}/{daily_limit}")
    return normalized


def resolve_queue_schedule(payload: dict[str, Any], requested_time: str = "") -> str:
    """Resolve a queue slot, preferring a valid requested time and replanning conflicts."""
    profile_dir = str(payload.get("profile_dir") or active_profile_dir())
    reserved = reserved_schedule_times(profile_dir)
    requested = str(requested_time or payload.get("schedule_time") or "").strip()
    auto_schedule = bool(payload.get("schedule_auto", True))
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
            WHERE status = 'published'
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
            WHERE status = 'published'
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
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        current_path = Path(current)
        if current_path != root:
            folders.append(current_path)
    for folder in folders:
        imgs = image_files(folder)
        if not imgs:
            continue
        latest_mtime = max(p.stat().st_mtime for p in imgs)
        signature = material_signature(imgs)
        published_count, last_used_at = get_folder_history(str(folder), signature)
        relative_path, year, month, hierarchy = _calendar_context(root, folder)
        if published_count == 0:
            reason = "未发布素材，优先推荐"
            duplicate_status = "new"
        else:
            reason = f"已预填/发布 {published_count} 次，上次使用 {last_used_at or '未知'}"
            duplicate_status = "used"
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
                published_count=published_count,
                last_used_at=last_used_at,
                score_reason=reason,
                images=[str(p) for p in imgs],
                material_signature=signature,
                duplicate_status=duplicate_status,
                relative_path=relative_path,
                year=year,
                month=month,
                hierarchy=hierarchy,
                display_name=relative_path if relative_path != "." else folder.name,
            )
        )

    all_used = candidates and all(c.published_count > 0 for c in candidates)
    if all_used:
        candidates.sort(key=lambda c: (c.last_used_at or "", -c.latest_mtime))
    else:
        candidates.sort(key=lambda c: (c.published_count > 0, -c.latest_mtime, c.relative_path))
    return candidates


def choose_image_group(images: list[str], max_count: int = 9) -> list[str]:
    selected = images[:max_count]
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
    if model_name:
        base.insert(0, model_name)
    deduped: list[str] = []
    for tag in base:
        clean = tag.strip().lstrip("#")
        if clean and clean not in deduped:
            deduped.append(clean)
    return deduped[:10]


def generate_copy(payload: dict[str, Any]) -> dict[str, Any]:
    engine = payload.get("copy_engine") or db_get_setting("copy_engine", "auto")
    if engine in ("auto", "anthropic"):
        ai_copy = generate_ai_copy(payload)
        if ai_copy:
            return ai_copy

    return generate_template_copy(payload)


def generate_template_copy(payload: dict[str, Any]) -> dict[str, Any]:
    style = payload.get("style") or "professional"
    model_name = (payload.get("model_name") or "这位小姐姐").strip()
    folder_name = payload.get("folder_name") or ""
    notes = (payload.get("notes") or "").strip()
    album = (payload.get("album") or "").strip()
    location = (payload.get("location") or "").strip()

    style_label = COPY_STYLES.get(style, COPY_STYLES["professional"])
    context = f"{folder_name} {notes}".strip()
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
    if context:
        body_map[style] += f"\n\n这组素材关键词：{context}"
    if album or location:
        extra = []
        if album:
            extra.append(f"专辑：{album}")
        if location:
            extra.append(f"地点：{location}")
        body_map[style] += "\n\n" + "；".join(extra)

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
    deepseek = generate_deepseek_copy(payload)
    if deepseek:
        return deepseek

    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    if not token:
        return None


def _ssl_context():
    """本机有代理拦截 HTTPS 时，Python 默认证书库会验证失败，改用 certifi。"""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def call_deepseek_json(system: str, prompt: str, timeout: int = 120) -> dict[str, Any] | None:
    """通用 DeepSeek JSON 调用，返回解析后的 dict，失败返回 None。"""
    token = os.environ.get("DEEPSEEK_API_KEY") or db_get_setting("deepseek_api_key", "")
    if not token:
        return None
    model = db_get_setting("deepseek_model", "deepseek-v4-pro")
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
            "https://api.deepseek.com/chat/completions",
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
        return json.loads(text)
    except Exception as exc:
        print(f"[deepseek] call_deepseek_json failed: {exc!r}")
        return None


def active_profile_dir() -> str:
    return str(db_get_setting("active_chrome_profile", "Default") or "Default")


def _profile_setting_key(profile_dir: str | None = None) -> str:
    profile = profile_dir or active_profile_dir()
    return f"account_profile:{profile}"


def get_account_profile(profile_dir: str | None = None) -> dict[str, Any] | None:
    """Read the saved profile for the active Chrome account."""
    raw = db_get_setting(_profile_setting_key(profile_dir), None)
    if raw is None and profile_dir is None and active_profile_dir() == "Default":
        # Migrate the original single-profile setting without losing it.
        raw = db_get_setting("account_profile", None)
    if isinstance(raw, dict) and raw.get("positioning"):
        return raw
    return None


def generate_account_profile(
    max_count: int = 20,
    manual_keywords: str = "",
    manual_topics: str = "",
    manual_notes: str = "",
) -> dict[str, Any]:
    """抓取创作平台近 N 条笔记 + 首页数据，交给 DeepSeek 总结为账号画像并保存。"""
    manual_present = any((manual_keywords, manual_topics, manual_notes))
    source_error = ""
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
        f"- {n.get('title')}（{n.get('time')}）浏览{n.get('views')} 赞{n.get('likes')} 藏{n.get('collects')} 评{n.get('comments')}"
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
    parsed = call_deepseek_json("你是资深小红书运营分析师，擅长从账号数据中提炼账号定位与内容策略。", prompt, timeout=120)
    if not parsed or not parsed.get("positioning"):
        return {"ok": False, "error": "AI 总结画像失败，请重试"}
    parsed["generated_at"] = now_iso()
    parsed["note_count"] = len(notes)
    parsed["source_profile"] = active_profile_dir()
    parsed["manual_keywords"] = manual_keywords
    parsed["manual_topics"] = manual_topics
    parsed["manual_notes"] = manual_notes
    parsed["source_notes"] = notes[:max_count]
    if source_error:
        parsed["source_error"] = source_error
    db_set_setting(_profile_setting_key(), parsed)
    return {
        "ok": True,
        "profile": parsed,
        "source_notes": notes[:max_count],
        "source_error": source_error,
    }


def profile_prompt_block() -> str:
    """把账号画像转成 prompt 片段，注入文案生成；没有画像时返回空串。"""
    profile = get_account_profile()
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
""".strip()


def generate_deepseek_copy(payload: dict[str, Any]) -> dict[str, Any] | None:
    token = os.environ.get("DEEPSEEK_API_KEY") or db_get_setting("deepseek_api_key", "")
    if not token:
        return None
    model = db_get_setting("deepseek_model", "deepseek-v4-pro")
    style = payload.get("style") or "professional"
    style_label = COPY_STYLES.get(style, COPY_STYLES["professional"])
    model_name = (payload.get("model_name") or "这位出镜人").strip()
    folder_name = payload.get("folder_name") or ""
    notes = payload.get("notes") or ""
    profile_block = profile_prompt_block()
    prompt = f"""
请为小红书账号生成一条图文文案。
{profile_block}

硬性要求：
1. 标题不超过20个中文字符。
2. 正文 120-260 字，真实自然，像摄影工作室日常分享，不要夸大宣传。
3. 话题 5-10 个，不带 #。
4. 输出严格 JSON，不要 Markdown，不要解释。

输入：
- 文案方式：{style_label}
- 出镜人/模特名：{model_name}
- 素材文件夹：{folder_name}
- 拍摄备注：{notes}

JSON 格式：
{{"title":"...","content":"...","tags":["..."]}}
""".strip()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是擅长小红书摄影类账号内容的中文文案编辑，严格遵循账号画像。"},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        parsed = json.loads(text)
        title = str(parsed.get("title", "")).strip()[:20]
        content = str(parsed.get("content", "")).strip()
        tags = [str(t).strip().lstrip("#") for t in parsed.get("tags", []) if str(t).strip()][:10]
        if not title or not content or not tags:
            return None
        return {
            "title": title,
            "content": content,
            "tags": tags,
            "style": style,
            "style_label": style_label,
            "ai_used": True,
            "engine": f"deepseek:{model}",
        }
    except Exception:
        return None

    style = payload.get("style") or "professional"
    style_label = COPY_STYLES.get(style, COPY_STYLES["professional"])
    model_name = (payload.get("model_name") or "这位出镜人").strip()
    folder_name = payload.get("folder_name") or ""
    notes = payload.get("notes") or ""
    prompt = f"""
你是小红书演员资料照账号的文案编辑。请根据输入生成一条低频真实运营的小红书图文文案。

要求：
- 标题不超过20个中文字符。
- 正文 120-260 字，口语自然，不油腻，不夸大。
- 话题 5-10 个，不带 #。
- 风格：{style_label}
- 出镜人/模特名：{model_name}
- 素材文件夹：{folder_name}
- 拍摄备注：{notes}
- 输出严格为 JSON：{{"title":"...","content":"...","tags":["..."]}}
""".strip()
    request_body = {
        "model": model,
        "max_tokens": 900,
        "messages": [{"role": "user", "content": prompt}],
    }
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": token,
        "anthropic-version": "2023-06-01",
        "Authorization": f"Bearer {token}",
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        start = text.find("{")
        end = text.rfind("}") + 1
        parsed = json.loads(text[start:end])
        title = str(parsed.get("title", "")).strip()[:20]
        content = str(parsed.get("content", "")).strip()
        tags = [str(t).strip().lstrip("#") for t in parsed.get("tags", []) if str(t).strip()][:10]
        if not title or not content or not tags:
            return None
        return {
            "title": title,
            "content": content,
            "tags": tags,
            "style": style,
            "style_label": style_label,
            "ai_used": True,
            "engine": "anthropic",
        }
    except Exception:
        return None


def run_xhs_cli(args: list[str], timeout: int = 180) -> dict[str, Any]:
    bundled_python = APP_ROOT / ".venv" / "bin" / "python"
    if bundled_python.exists():
        prefix = [str(bundled_python)]
    elif shutil.which("uv"):
        prefix = ["uv", "run", "python"]
    else:
        prefix = [sys.executable]
    cmd = [*prefix, str(XHS_SKILL_DIR / "scripts" / "cli.py"), *args]
    with BROWSER_LOCK:
        proc = subprocess.run(
            cmd,
            cwd=XHS_SKILL_DIR,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
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
    with db_connection() as db:
        same_title = db.execute(
            "SELECT COUNT(*) FROM publish_history WHERE title = ? AND status IN ('prefilled', 'published') AND profile_dir = ?",
            (title, active_profile_dir()),
        ).fetchone()[0]
        same_content = db.execute(
            "SELECT COUNT(*) FROM publish_history WHERE content = ? AND status IN ('prefilled', 'published') AND profile_dir = ?",
            (content, active_profile_dir()),
        ).fetchone()[0]
    if same_title:
        warnings.append("标题与历史记录重复")
    if same_content:
        warnings.append("正文与历史记录重复")
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
                topic_template, shooting_date, shooting_location, warnings, profile_dir
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
            "SELECT * FROM publish_jobs ORDER BY id ASC LIMIT ?",
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


def enqueue_job(payload: dict[str, Any], schedule_time: str) -> dict[str, Any]:
    profile_dir = str(payload.get("profile_dir") or active_profile_dir())
    schedule_time = normalize_schedule_time(schedule_time)
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
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (key, json.dumps(payload, ensure_ascii=False), schedule_time, profile_dir, created, created),
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
            "SELECT * FROM publish_jobs WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
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


def switch_chrome_profile(profile_dir: str) -> dict[str, Any]:
    """Switch the active Chrome profile and wait for the Bridge connection."""
    profile_dir = str(profile_dir or "Default").strip()
    valid = {p["dir"] for p in chrome_profiles()}
    if profile_dir not in valid:
        raise PublishError(f"未知浏览器账号目录：{profile_dir}")
    with BROWSER_LOCK:
        if profile_dir != active_profile_dir():
            subprocess.run(
                ["osascript", "-e", 'tell application "Google Chrome" to quit'],
                capture_output=True,
                timeout=15,
            )
            time.sleep(1.5)
            subprocess.Popen(["open", "-a", "Google Chrome", "--args", "--profile-directory", profile_dir])
            ready = wait_extension_ready(30)
            if not ready:
                raise PublishError(f"账号「{profile_dir}」的 XHS Bridge 未连接")
            db_set_setting("active_chrome_profile", profile_dir)
        login = run_xhs_cli(["check-login"], timeout=30)
    data = login.get("json") or {}
    logged_in = data.get("logged_in")
    if not login.get("ok") or logged_in is False:
        raise PublishError(f"账号「{profile_dir}」未登录小红书，请先在该 Chrome Profile 登录")
    return {"ready": True, "profile": profile_dir, "login": login}


def prepare_publish(payload: dict[str, Any], reservation_job_id: int | None = None) -> dict[str, Any]:
    """Fill the XHS page and persist a prefilled history row, without clicking submit."""
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    images = payload.get("images") or []
    tags = [str(t).strip().lstrip("#") for t in payload.get("tags") or [] if str(t).strip()]
    if not title or not content or not images:
        raise PublishError("标题、正文和图片不能为空")

    schedule_time = str(payload.get("schedule_time") or "")
    if payload.get("publish_mode") == "scheduled" and not schedule_time:
        try:
            schedule_time = resolve_planned_schedule_time(
                str(payload.get("schedule_window") or ""),
                profile_dir=str(payload.get("profile_dir") or active_profile_dir()),
            )
        except ValueError as exc:
            raise PublishError(str(exc)) from exc
        payload = {**payload, "schedule_time": schedule_time}
    elif schedule_time:
        try:
            schedule_time = normalize_schedule_time(schedule_time)
        except ValueError as exc:
            raise PublishError(str(exc)) from exc
        payload = {**payload, "schedule_time": schedule_time}
    elif payload.get("publish_mode") == "immediate":
        payload = {**payload, "schedule_time": ""}

    daily_limit = int(db_get_setting("daily_limit", 3))
    try:
        scheduled_day = date.fromisoformat((schedule_time or date.today().isoformat())[:10])
    except ValueError:
        scheduled_day = date.today()
    profile_dir = str(payload.get("profile_dir") or active_profile_dir())
    usage = usage_count_for_day(scheduled_day, profile_dir)
    reserved = reserved_schedule_times(profile_dir, exclude_job_id=reservation_job_id)
    reserved_for_day = sum(1 for value in reserved if value[:10] == scheduled_day.isoformat())
    if usage + reserved_for_day >= daily_limit:
        raise PublishError(f"计划日已达到发布上限：{scheduled_day.isoformat()} {usage}/{daily_limit}")

    fx_params = get_fx_params()
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
            update_history_status(history_id, "published")
        return {"ok": True, "result": result, "confirmation": confirmation}
    if result["ok"]:
        message = "发布命令返回成功，但没有收到平台确认，状态不明，请人工检查"
        if history_id:
            update_history_status(history_id, "unknown")
        return {"ok": False, "uncertain": True, "message": message, "result": result}
    if history_id:
        update_history_status(history_id, "unknown")
    return {"ok": False, "result": result}


def update_history_status(history_id: int, status: str) -> None:
    with db_connection() as db:
        db.execute(
            "UPDATE publish_history SET status = ?, confirmed_at = ? WHERE id = ?",
            (status, now_iso() if status == "published" else None, int(history_id)),
        )
        db.commit()


def process_queue_job(job: dict[str, Any]) -> bool:
    payload = job["payload"]
    job_id = int(job["id"])
    try:
        switch_chrome_profile(str(job.get("profile_dir") or active_profile_dir()))
        prepared = prepare_publish(payload, reservation_job_id=job_id)
        history_id = int(prepared["history_id"])
        update_job(job_id, history_id=history_id, status="reviewed_page")
        clicked = click_publish(history_id)
        if not clicked["ok"]:
            message = (
                clicked.get("message")
                or clicked["result"].get("stderr")
                or clicked["result"].get("stdout")
                or "点击发布失败"
            )[:500]
            update_job(job_id, status="unknown", last_error=message)
            db_set_setting("queue_enabled", False)
            return False
        update_job(job_id, status="submitted", completed_at=now_iso(), last_error="")
        return True
    except PublishError as exc:
        update_job(job_id, status="unknown", last_error=str(exc))
        db_set_setting("queue_enabled", False)
        return False
    except Exception as exc:
        update_job(job_id, status="unknown", last_error=f"{type(exc).__name__}: {exc}")
        db_set_setting("queue_enabled", False)
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


def update_install_block_reason() -> str:
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
        if path == "/api/config":
            root_path = db_get_setting("root_path", str(DEFAULT_TEST_ROOT))
            json_response(
                self,
                {
                    "ok": True,
                    "config": {
                        "root_path": root_path,
                        "default_album": db_get_setting("default_album", ""),
                        "default_location": db_get_setting("default_location", ""),
                        "default_schedule_time": db_get_setting("default_schedule_time", ""),
                        "schedule_windows": db_get_setting("schedule_windows", ["早上 10:00", "晚上 22:00-24:00"]),
                        "topic_templates": db_get_setting("topic_templates", ["演员资料", "艺考录制"]),
                        "daily_limit": db_get_setting("daily_limit", 3),
                        "visibility": db_get_setting("visibility", "仅自己可见"),
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
            json_response(
                self,
                {"ok": True, "profile": get_account_profile(), "profile_dir": active_profile_dir()},
            )
            return
        if path == "/api/logo/settings":
            json_response(self, {"ok": True, "settings": get_fx_params()})
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
            params = get_fx_params(overrides)
            try:
                data = imagefx.render_preview(p, params)
            except Exception:
                data = p.read_bytes()
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
        except subprocess.TimeoutExpired:
            error_response(self, "命令执行超时，发布页可能卡住了", 504)
        except Exception as exc:
            error_response(self, str(exc), 500)

    def _post(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        payload = read_json(self)
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
            for key in (
                "root_path",
                "default_album",
                "default_location",
                "default_schedule_time",
                "schedule_windows",
                "topic_templates",
                "daily_limit",
                "visibility",
                "copy_engine",
                "deepseek_model",
                "moments_tag",
            ):
                if key in payload:
                    db_set_setting(key, payload[key])
            if payload.get("deepseek_api_key"):
                db_set_setting("deepseek_api_key", payload["deepseek_api_key"])
            json_response(self, {"ok": True})
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
            queued_payload = {**queued_payload, "schedule_time": schedule_time}
            job = enqueue_job(queued_payload, schedule_time)
            start_queue_worker()
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
        if path == "/api/recommend":
            root = payload.get("root_path") or db_get_setting("root_path", "")
            candidates = scan_folders(root)
            if not candidates:
                error_response(self, "没有找到包含图片的素材文件夹", 404)
                return
            candidate = candidates[0]
            data = asdict(candidate)
            data["selected_images"] = choose_image_group(candidate.images, int(payload.get("max_images") or 9))
            json_response(self, {"ok": True, "item": data})
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
            )
            if result.get("ok"):
                json_response(self, result)
            else:
                error_response(self, result.get("error") or "生成画像失败", 500)
            return
        if path == "/api/validate":
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
            result = run_xhs_cli(["check-login"], timeout=35)
            json_response(self, {"ok": result["ok"], "result": result})
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
            target = imagefx.ASSETS_DIR / f"{kind}.png"
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
            profiles = chrome_profiles()
            active = db_get_setting("active_chrome_profile") or (profiles[0]["dir"] if profiles else "Default")
            json_response(self, {"ok": True, "profiles": profiles, "active": active})
            return
        if path == "/api/accounts/switch":
            profile_dir = str(payload.get("profile") or "").strip()
            if bool(db_get_setting("queue_enabled", False)) and any(
                row["status"] in {"queued", "submitting", "reviewed_page"} for row in queue_rows()
            ):
                error_response(self, "队列正在提交，请先暂停队列再切换账号", 409)
                return
            try:
                switched = switch_chrome_profile(profile_dir)
            except PublishError as exc:
                error_response(self, str(exc), 502)
                return
            json_response(
                self,
                {
                    "ok": True,
                    "ready": switched["ready"],
                    "profile": profile_dir,
                    "login_stdout": switched["login"].get("stdout", "")[-200:],
                },
            )
            return
        if path == "/api/accounts/logout":
            # 退出当前账号：清掉当前 Chrome Profile 的小红书登录 Cookie（账号列表不受影响）
            result = run_xhs_cli(["delete-cookies"], timeout=30)
            if result["ok"]:
                json_response(self, {"ok": True, "result": result})
            else:
                error_response(self, (result.get("stderr") or result.get("stdout") or "退出失败")[:300], 502)
            return
        if path == "/api/cover/config":
            # AI封面：平台/模型清单 + 已预填的接入配置（从样片工厂迁移，本地保存）
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
            # API易图生图：Gemini 原生 generateContent 通道（流程照搬样片工厂已验证实现）
            import base64 as _b64
            from io import BytesIO

            from PIL import Image as PILImage

            ensure_ai_defaults()
            api_key = str(payload.get("api_key") or db_get_setting("apiyi_key") or "").strip()
            base_url = str(payload.get("base_url") or db_get_setting("apiyi_base") or APIYI_BASE_DEFAULT).strip().rstrip("/")
            model = str(payload.get("model") or "gemini-3.1-flash-image-preview").strip()
            resolution = str(payload.get("resolution") or "2k").strip().lower()
            aspect_ratio = str(payload.get("aspect_ratio") or "3:4").strip()
            prompt = str(payload.get("prompt") or "").strip()
            image_path = str(payload.get("image_path") or "").strip()
            if not api_key:
                error_response(self, "API易 Key 未配置（后台设置 → AI 接入）", 400)
                return
            if not prompt:
                error_response(self, "提示词不能为空", 400)
                return
            src = Path(image_path)
            if not src.exists() or not src.is_file():
                error_response(self, f"参考图不存在：{image_path}", 400)
                return
            # 参考图压缩：长边压到 2048，转 JPEG（与样片工厂 APIYI_REFERENCE_MAX_LONG_EDGE 一致）
            with PILImage.open(src) as im:
                im = im.convert("RGB")
                if max(im.size) > 2048:
                    im.thumbnail((2048, 2048))
                buf = BytesIO()
                im.save(buf, "JPEG", quality=90)
            ref_b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
            payload_body = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt},
                            {"inlineData": {"mimeType": "image/jpeg", "data": ref_b64}},
                        ]
                    }
                ],
                "generationConfig": {
                    "imageConfig": {
                        "imageSize": resolution.upper(),
                        "aspectRatio": aspect_ratio,
                    }
                },
            }
            url = f"{base_url}/v1beta/models/{model}:generateContent"
            req = urllib.request.Request(
                url,
                data=json.dumps(payload_body).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            started = time.time()
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = str(exc.read().decode("utf-8"))[:300]
                except Exception:
                    pass
                error_response(self, f"API易返回 HTTP {exc.code}：{detail}", 502)
                return
            except Exception as exc:
                error_response(self, f"API易请求失败：{exc}", 502)
                return
            # 解析：parts 里筛 inlineData，取最后一张（复杂任务最后一张是终稿）
            images = []
            for part in ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data") or {}
                if inline.get("data"):
                    images.append((inline["data"], inline.get("mimeType") or "image/png"))
            if not images:
                finish = str(((data.get("candidates") or [{}])[0].get("finishReason")) or "").upper()
                error_response(self, f"API易未返回图片（finishReason={finish or 'UNKNOWN'}，可能被安全审核拦截，可改写提示词重试）", 502)
                return
            b64, mime = images[-1]
            json_response(
                self,
                {
                    "ok": True,
                    "b64": b64,
                    "mime": mime,
                    "model": model,
                    "resolution": resolution,
                    "elapsed_sec": round(time.time() - started, 2),
                },
            )
            return
        if path == "/api/xhs/fill-publish":
            self.fill_publish(payload)
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
                db.execute(
                    "UPDATE publish_history SET status = 'published', confirmed_at = ? WHERE id = ? AND status IN ('prefilled', 'unknown')",
                    (now_iso(), item_id),
                )
                db.commit()
            json_response(self, {"ok": True})
            return
        error_response(self, "未知接口", 404)

    def fill_publish(self, payload: dict[str, Any]) -> None:
        try:
            json_response(self, prepare_publish(payload))
        except PublishError as exc:
            error_response(self, str(exc), 400)
        return



def main() -> None:
    global HTTP_SERVER
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5818, type=int)
    args = parser.parse_args()
    init_db()
    start_queue_worker()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    HTTP_SERVER = server
    print(f"XHS Assistant running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
