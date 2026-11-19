"""Read the connected account's identity without navigating or publishing."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class IdentityUnavailable(Exception):
    pass


def safe_avatar_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    if not any(host == domain or host.endswith("." + domain) for domain in ("xhscdn.com", "xiaohongshu.com", "xhsimg.com")):
        return ""
    return value[:2048]


def normalize_identity(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise IdentityUnavailable("小红书未返回账号资料")
    if raw.get("logged_in") is False:
        return {"logged_in": False, "nickname": "", "user_id": "", "avatar": "", "status": "logged_out"}
    nickname = str(raw.get("nickname") or "").strip()[:120]
    if raw.get("logged_in") is not True or not nickname:
        raise IdentityUnavailable(str(raw.get("error") or "未识别到当前登录的小红书账号")[:300])
    return {
        "logged_in": True,
        "nickname": nickname,
        "user_id": str(raw.get("user_id") or "").strip()[:128],
        "avatar": safe_avatar_url(raw.get("avatar")),
        "status": "verified",
    }


def _bridge_call(method: str, params: dict[str, Any] | None = None, timeout: float = 10) -> Any:
    from websockets.sync.client import connect

    with connect("ws://localhost:9333", open_timeout=2, close_timeout=1, proxy=None, max_size=1024 * 1024) as ws:
        ws.send(json.dumps({"role": "cli", "method": method, "params": params or {}}))
        response = json.loads(ws.recv(timeout=timeout))
    if response.get("error"):
        raise IdentityUnavailable(str(response["error"])[:300])
    return response.get("result")


def _read_from_consumer_site(expression: str, fallback: dict[str, Any]) -> dict[str, Any]:
    """后台打开一个消费者页读取真实 user_id，读完立即关闭。

    创作者发布页只暴露昵称、不暴露 user_id，而且昵称可能被截断（例如末尾的“—”），
    直接用它会导致账号显示对不上、身份校验失效。
    """
    tab = None
    try:
        tab = _bridge_call("open_tab", {"url": "https://www.xiaohongshu.com/explore", "active": False}, timeout=15)
        tab_id = tab.get("id") if isinstance(tab, dict) else tab
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(1)
            raw = _bridge_call("evaluate", {"expression": expression,"tabId":tab_id}, timeout=12)
            try:
                identity = normalize_identity(raw)
            except IdentityUnavailable:
                continue
            if identity.get("user_id"):
                return identity
    except Exception:
        pass
    finally:
        if tab is not None:
            try:
                _bridge_call("close_tab", {"tabId": tab.get("id") if isinstance(tab, dict) else tab}, timeout=10)
            except Exception:
                pass
    return fallback


def read_connected_identity(skill_dir: Path) -> dict[str, Any]:
    """Start only the local bridge if needed; never restart Chrome to read a label."""
    try:
        status = _bridge_call("ping_server", timeout=2)
    except OSError:
        subprocess.Popen(
            [sys.executable, str(skill_dir / "scripts" / "bridge_server.py")],
            cwd=skill_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        status = {}
    deadline = time.monotonic() + 5
    while not (status or {}).get("extension_connected"):
        if time.monotonic() >= deadline:
            raise IdentityUnavailable("Chrome 扩展未连接；请打开对应用户资料并启用 XHS Bridge 后刷新")
        time.sleep(0.25)
        try:
            status = _bridge_call("ping_server", timeout=2)
        except OSError:
            status = {}
    expression = (Path(__file__).parent / "static" / "account-identity.js").read_text("utf-8")
    identity = normalize_identity(_bridge_call("evaluate", {"expression": expression}, timeout=12))
    if identity.get("logged_in") and not identity.get("user_id"):
        identity = _read_from_consumer_site(expression, identity)
    return identity
