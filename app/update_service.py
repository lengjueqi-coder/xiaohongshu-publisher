from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


GITHUB_REPOSITORY = "lengjueqi-coder/xiaohongshu-publisher"
RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
PACKAGE_ASSET = "xhs-assistant-mac.zip"
CHECKSUM_ASSET = f"{PACKAGE_ASSET}.sha256"
USER_AGENT = "XHS-Publish-Assistant-Updater"


class UpdateError(Exception):
    """A release lookup or update launch failed."""


def version_key(value: str) -> tuple[int, int, int, int]:
    raw = str(value or "").strip()
    if raw.startswith("v"):
        raw = raw[1:]
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", raw):
        raise ValueError(f"版本号格式不受支持：{value}")
    parts = tuple(int(part) for part in raw.split("."))
    return (parts + (0, 0, 0, 0))[:4]


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("GitHub 上还没有已发布的软件版本") from exc
        raise UpdateError(f"检查更新失败：GitHub 返回 HTTP {exc.code}") from exc
    except Exception as exc:
        raise UpdateError(f"连接 GitHub 检查更新失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("GitHub 返回的版本信息格式无效")
    return payload


def _release_info(release: dict[str, Any]) -> dict[str, Any]:
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError("最新版本不是可用于自动更新的正式版本")
    tag = str(release.get("tag_name") or "").strip()
    try:
        version_key(tag)
    except ValueError as exc:
        raise UpdateError(f"GitHub Release 标签无效：{tag or '空'}") from exc

    assets = {
        str(asset.get("name") or ""): asset
        for asset in release.get("assets", [])
        if isinstance(asset, dict)
    }
    package = assets.get(PACKAGE_ASSET) or {}
    checksum = assets.get(CHECKSUM_ASSET) or {}
    package_url = str(package.get("browser_download_url") or "")
    checksum_url = str(checksum.get("browser_download_url") or "")
    asset_prefix = f"/{GITHUB_REPOSITORY}/releases/download/"

    def valid_asset_url(value: str) -> bool:
        parsed = urlparse(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.path.startswith(asset_prefix)
            and not parsed.username
            and not parsed.password
        )

    package_ready = valid_asset_url(package_url) and valid_asset_url(checksum_url)
    return {
        "version": tag.removeprefix("v"),
        "tag": tag,
        "name": str(release.get("name") or tag),
        "notes": str(release.get("body") or "").strip()[:4000],
        "published_at": str(release.get("published_at") or ""),
        "html_url": str(release.get("html_url") or ""),
        "package_ready": package_ready,
        "asset_url": package_url if package_ready else "",
        "checksum_url": checksum_url if package_ready else "",
    }


def check_for_update(current_version: str, release: dict[str, Any] | None = None) -> dict[str, Any]:
    info = _release_info(release if release is not None else _get_json(RELEASE_API_URL))
    try:
        available = version_key(info["version"]) > version_key(current_version)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc
    return {
        "ok": True,
        "current_version": current_version,
        "latest_version": info["version"],
        "update_available": available,
        "package_ready": info["package_ready"],
        "release": info,
    }


def launch_update(release: dict[str, Any], app_root: Path, parent_pid: int) -> None:
    if not release.get("package_ready"):
        raise UpdateError("该版本缺少更新包或 SHA-256 文件")
    updater = Path(app_root) / "app" / "updater.py"
    if not updater.is_file():
        raise UpdateError("未找到本地更新程序")
    log_path = Path(app_root) / "data" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(updater),
        "--asset-url",
        str(release["asset_url"]),
        "--checksum-url",
        str(release["checksum_url"]),
        "--version",
        str(release["version"]),
        "--parent-pid",
        str(int(parent_pid)),
        "--app-root",
        str(Path(app_root)),
    ]
    try:
        with log_path.open("ab") as log_file:
            subprocess.Popen(
                command,
                cwd=str(app_root),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except Exception as exc:
        raise UpdateError(f"无法启动独立更新程序：{exc}") from exc
