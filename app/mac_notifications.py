"""Durable, acknowledgeable Mac alerts. No remote service receives user data."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


SOUNDS = ("Glass", "Ping", "Pop", "Submarine", "Hero", "Basso", "Funk", "Morse", "Purr", "Sosumi", "Tink")
DEFAULTS = {"enabled": True, "audio": "both", "sound": "Glass", "voice": "Tingting",
            "message": "小红书发布助手有事项需要你处理，请查看软件通知。",
            "repeat": "count", "repeat_count": 3, "interval_minutes": 10, "burst_count": 1, "custom_audio": ""}


def init_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS automation_alerts (
        id INTEGER PRIMARY KEY, profile_dir TEXT NOT NULL, plan_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
        acknowledged_at TEXT, next_at REAL NOT NULL DEFAULT 0, delivered_count INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, delivery_error TEXT NOT NULL DEFAULT '')""")


def settings(app):
    return {**DEFAULTS, **app.db_get_setting("notification_settings", {})}


def save_settings(app, raw):
    config = {**settings(app), **{k: v for k, v in raw.items() if k in DEFAULTS}}
    if config["audio"] not in {"sound", "speech", "both", "silent", "custom"}:
        raise ValueError("请选择提示音或语音方式")
    if config["sound"] not in SOUNDS or config["repeat"] not in {"once", "count", "interval"}:
        raise ValueError("提醒方式无效")
    config["repeat_count"] = max(1, min(20, int(config["repeat_count"])))
    config["burst_count"] = max(1, min(3, int(config["burst_count"])))
    config["interval_minutes"] = max(1, min(1440, int(config["interval_minutes"])))
    config["message"] = str(config["message"]).strip()[:200] or DEFAULTS["message"]
    config["voice"] = str(config["voice"]).strip()[:80]
    # Audio is imported through a dedicated endpoint, never an arbitrary executable/URL.
    config["custom_audio"] = settings(app)["custom_audio"]
    app.db_set_setting("notification_settings", config)
    return config


def add_alert(app, profile, plan_id, kind, message):
    with app.db_connection() as db:
        existing = db.execute("SELECT id FROM automation_alerts WHERE profile_dir=? AND plan_id=? AND kind=? AND acknowledged_at IS NULL", (profile, plan_id, kind)).fetchone()
        if existing:
            return existing[0]
        cur = db.execute("INSERT INTO automation_alerts(profile_dir,plan_id,kind,message,created_at) VALUES(?,?,?,?,?)",
                         (profile, plan_id, kind, str(message)[:1000], app.now_iso()))
        return cur.lastrowid


def alerts(app):
    with app.db_connection() as db:
        db.row_factory = __import__("sqlite3").Row
        return [dict(r) for r in db.execute("SELECT * FROM automation_alerts ORDER BY id DESC LIMIT 100")]


def acknowledge(app, alert_id):
    with app.db_connection() as db:
        db.execute("UPDATE automation_alerts SET acknowledged_at=? WHERE id=?", (app.now_iso(), int(alert_id)))


def voices():
    if sys.platform != "darwin":
        return []
    try:
        result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
        return [line.split("zh_")[0].strip() for line in result.stdout.splitlines() if "zh_" in line]
    except (OSError, subprocess.TimeoutExpired):
        return []


def play_audio(app, config):
    if config["audio"] == "silent":
        return
    if sys.platform != "darwin":
        raise RuntimeError("声音提醒需要在 Mac 上运行")
    if config["audio"] in {"sound", "both"}:
        sound = Path("/System/Library/Sounds") / (config["sound"] + ".aiff")
        subprocess.run(["afplay", str(sound)], check=True, timeout=20, capture_output=True)
    if config["audio"] in {"speech", "both"}:
        target_dir = app.APP_ROOT / "data" / "alerts-audio"
        target_dir.mkdir(parents=True, exist_ok=True)
        import hashlib
        key = hashlib.sha256((config["voice"] + config["message"]).encode()).hexdigest()[:20]
        audio = target_dir / (key + ".aiff")
        if not audio.exists():
            subprocess.run(["say", "-v", config["voice"], "-o", str(audio), "--", config["message"]], check=True, timeout=45, capture_output=True)
        subprocess.run(["afplay", str(audio)], check=True, timeout=60, capture_output=True)
    if config["audio"] == "custom":
        audio = Path(config["custom_audio"])
        if not audio.is_file() or not audio.resolve().is_relative_to((app.APP_ROOT / "data" / "alerts-audio").resolve()):
            raise ValueError("请先导入提醒录音")
        subprocess.run(["afplay", str(audio)], check=True, timeout=60, capture_output=True)


def deliver(app, alert, config):
    if sys.platform != "darwin":
        raise RuntimeError("Mac 系统通知不可用")
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    script = f'display notification {quote(alert["message"][:240])} with title {quote("小红书发布助手 · 需要处理")} subtitle {quote(alert["profile_dir"])}'
    subprocess.run(["osascript", "-e", script], check=True, timeout=12, capture_output=True)
    for count in range(int(config.get("burst_count", 1))):
        if alert.get("id"):
            with app.db_connection() as db:
                row = db.execute("SELECT acknowledged_at FROM automation_alerts WHERE id=?", (alert["id"],)).fetchone()
            if not row or row[0]:
                break
        play_audio(app, config)
        if count < int(config.get("burst_count", 1))-1:
            time.sleep(0.5)


def pump(app, now=None):
    config = settings(app)
    if not config["enabled"]:
        return
    now = time.time() if now is None else now
    limit = 1 if config["repeat"] == "once" else config["repeat_count"] if config["repeat"] == "count" else None
    with app.db_connection() as db:
        db.row_factory = __import__("sqlite3").Row
        due = [dict(a) for a in db.execute("""SELECT * FROM automation_alerts
            WHERE acknowledged_at IS NULL AND next_at<=? AND (? IS NULL OR delivered_count<?)
            ORDER BY next_at,id LIMIT 1""", (now, limit, limit))]
    # One sound per cycle, even when several accounts fail together.
    for alert in due[:1]:
        count = alert["delivered_count"]
        limit = 1 if config["repeat"] == "once" else config["repeat_count"] if config["repeat"] == "count" else None
        if limit is not None and count >= limit:
            continue
        try:
            deliver(app, alert, config)
            with app.db_connection() as db:
                db.execute("UPDATE automation_alerts SET delivered_count=delivered_count+1,attempts=0,delivery_error='',next_at=? WHERE id=? AND acknowledged_at IS NULL",
                           (now + config["interval_minutes"] * 60, alert["id"]))
        except Exception as exc:
            with app.db_connection() as db:
                db.execute("UPDATE automation_alerts SET attempts=attempts+1,delivery_error=?,next_at=? WHERE id=?", (str(exc)[:300], now + min(3600, 60 * 2 ** min(alert["attempts"], 6)), alert["id"]))


def import_audio(app, source):
    source = Path(source).expanduser().resolve()
    if source.suffix.lower() not in {".aiff", ".aif", ".wav", ".mp3", ".m4a"} or not source.is_file() or source.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("请选择小于 20MB 的 AIFF/WAV/MP3/M4A 录音")
    folder = app.APP_ROOT / "data" / "alerts-audio"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("custom" + source.suffix.lower())
    if source != target.resolve():
        shutil.copy2(source, target)
    config = settings(app)
    config["custom_audio"] = str(target)
    app.db_set_setting("notification_settings", config)
    return config
