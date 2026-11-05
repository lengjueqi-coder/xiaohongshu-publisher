"""Optional per-user login/crash recovery; no administrator permission needed."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.xhsassistant.publisher"


def agent_path():
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def status(app):
    path = agent_path()
    return {"supported": sys.platform == "darwin", "login_start": path.is_file(),
            "prevent_sleep": bool(app.db_get_setting("prevent_sleep", False))}


def configure(app, enabled, prevent_sleep):
    if sys.platform != "darwin":
        raise ValueError("后台启动只支持 macOS")
    path = agent_path()
    domain = f"gui/{os.getuid()}"
    if path.exists():
        subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True, timeout=10)
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        app_dir = app.APP_ROOT.resolve()
        logs = app_dir / "data"
        logs.mkdir(exist_ok=True)
        with path.open("wb") as stream:
            plistlib.dump({"Label": LABEL, "ProgramArguments": [str(app_dir / ".venv/bin/python"), str(app_dir / "app/background_service.py")],
                          "WorkingDirectory": str(app_dir), "RunAtLoad": True, "KeepAlive": True,
                          "AbandonProcessGroup": True,
                          "ThrottleInterval": 30, "StandardOutPath": str(logs / "background.log"),
                          "StandardErrorPath": str(logs / "background.log")}, stream)
        result = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True, timeout=15)
        if result.returncode:
            path.unlink(missing_ok=True)
            raise ValueError("注册登录启动失败：" + result.stderr[:200])
    else:
        path.unlink(missing_ok=True)
    app.db_set_setting("prevent_sleep", bool(prevent_sleep))
    apply_awake(app)
    return status(app)


_AWAKE = None


def apply_awake(app):
    global _AWAKE
    if _AWAKE and _AWAKE.poll() is None:
        _AWAKE.terminate()
    _AWAKE = None
    if sys.platform == "darwin" and app.db_get_setting("prevent_sleep", False):
        _AWAKE = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
