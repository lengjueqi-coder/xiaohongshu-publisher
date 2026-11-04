from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlparse

import certifi


GITHUB_REPOSITORY = "lengjueqi-coder/xiaohongshu-publisher"
PACKAGE_ASSET = "xhs-assistant-mac.zip"
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
ALLOWED_ROOTS = {"app", "moments"}
ALLOWED_ROOT_FILES = {
    "README.md",
    "安装教程-Mac-M芯片.md",
    "requirements-mac.txt",
    "install.command",
    "start.command",
    "stop.command",
}
FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "data",
    "config",
    "logs",
}


class UpdateInstallError(Exception):
    """The downloaded update is unsafe or could not be installed."""


def log(message: str) -> None:
    print(message, flush=True)


def validate_asset_url(url: str) -> None:
    parsed = urlparse(str(url))
    expected_path = f"/{GITHUB_REPOSITORY}/releases/download/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or not parsed.path.startswith(expected_path)
        or parsed.username
        or parsed.password
    ):
        raise UpdateInstallError("更新下载地址不是本软件的 GitHub Release 资源")


def download(url: str, target: Path, max_bytes: int = MAX_PACKAGE_BYTES) -> None:
    validate_asset_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "XHS-Publish-Assistant-Updater"})
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=45, context=context) as response, target.open("wb") as out:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UpdateInstallError("更新包超过允许大小")
                out.write(chunk)
    except UpdateInstallError:
        raise
    except urllib.error.HTTPError as exc:
        raise UpdateInstallError(f"下载更新失败：HTTP {exc.code}") from exc
    except Exception as exc:
        raise UpdateInstallError(f"下载更新失败：{exc}") from exc


def read_expected_sha256(checksum_path: Path) -> str:
    raw = checksum_path.read_text("utf-8", errors="replace")
    match = re.search(r"(?m)^\s*([0-9a-fA-F]{64})(?:\s+\*?xhs-assistant-mac\.zip)?\s*$", raw)
    if not match:
        raise UpdateInstallError("SHA-256 文件格式无效")
    return match.group(1).lower()


def verify_sha256(package_path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with package_path.open("rb") as package:
        for chunk in iter(lambda: package.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        raise UpdateInstallError("更新包 SHA-256 校验失败，已取消安装")


def _member_path(name: str) -> PurePosixPath | None:
    if not name or "\\" in name:
        raise UpdateInstallError("更新包包含无效路径")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UpdateInstallError("更新包包含越界路径")
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        raise UpdateInstallError("更新包试图覆盖本地数据或运行环境")
    if path.parts[0] not in ALLOWED_ROOTS and not (
        len(path.parts) == 1 and path.parts[0] in ALLOWED_ROOT_FILES
    ):
        raise UpdateInstallError(f"更新包包含非应用文件：{name}")
    if path.as_posix() == "moments/calibration.json":
        raise UpdateInstallError("更新包不能覆盖本机坐标校准")
    return path


def validate_archive(package_path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(package_path) as archive:
            names: list[str] = []
            seen: set[str] = set()
            total_size = 0
            for info in archive.infolist():
                name = info.filename.rstrip("/") if info.is_dir() else info.filename
                if not name:
                    continue
                path = _member_path(name)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise UpdateInstallError("更新包不能包含符号链接")
                if info.is_dir():
                    continue
                if path is None:
                    continue
                relative = path.as_posix()
                if relative in seen:
                    raise UpdateInstallError(f"更新包包含重复文件：{relative}")
                seen.add(relative)
                total_size += info.file_size
                if total_size > MAX_UNPACKED_BYTES:
                    raise UpdateInstallError("更新包解压后的总大小超过允许范围")
                names.append(relative)
            required = {"app/server.py", "app/updater.py", "start.command", "requirements-mac.txt"}
            if not required.issubset(names):
                missing = ", ".join(sorted(required - set(names)))
                raise UpdateInstallError(f"更新包缺少必要文件：{missing}")
            return names
    except zipfile.BadZipFile as exc:
        raise UpdateInstallError("下载的更新文件不是有效 ZIP") from exc


def wait_for_process_exit(pid: int, timeout: float = 45) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            raise UpdateInstallError("没有权限等待旧版服务退出")
        time.sleep(0.25)
    raise UpdateInstallError("旧版服务没有及时退出，已取消替换")


def _safe_target(app_root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    target = app_root.joinpath(*parts)
    current = app_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise UpdateInstallError(f"安装目标包含符号链接：{relative}")
    if not target.resolve(strict=False).is_relative_to(app_root.resolve()):
        raise UpdateInstallError(f"安装目标越出软件目录：{relative}")
    return target


def install_archive(package_path: Path, app_root: Path, backup_root: Path) -> list[str]:
    names = validate_archive(package_path)
    app_root = app_root.resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    backed_up: set[str] = set()
    try:
        with zipfile.ZipFile(package_path) as archive:
            for relative in sorted(names):
                path = _member_path(relative)
                if path is None:
                    continue
                target = _safe_target(app_root, path.as_posix())
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = backup_root / "staged" / path
                staged.parent.mkdir(parents=True, exist_ok=True)
                info = archive.getinfo(relative)
                staged.write_bytes(archive.read(info))
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    staged.chmod(mode)
                elif target.exists():
                    staged.chmod(stat.S_IMODE(target.stat().st_mode))

                if target.exists():
                    backup = backup_root / "original" / path
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                    backed_up.add(path.as_posix())
                os.replace(staged, target)
                installed.append(path.as_posix())
        return installed
    except Exception:
        rollback_archive(app_root, backup_root, installed, backed_up)
        raise


def rollback_archive(
    app_root: Path,
    backup_root: Path,
    installed: Iterable[str],
    backed_up: set[str],
) -> None:
    app_root = app_root.resolve()
    for relative in reversed(list(installed)):
        target = _safe_target(app_root, relative)
        backup = backup_root / "original" / PurePosixPath(relative)
        if relative in backed_up and backup.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, target)
        else:
            target.unlink(missing_ok=True)


def install_changed_requirements(app_root: Path, backup_root: Path) -> None:
    old_requirements = backup_root / "original" / "requirements-mac.txt"
    new_requirements = app_root / "requirements-mac.txt"
    if not old_requirements.is_file() or not new_requirements.is_file():
        return
    if old_requirements.read_bytes() == new_requirements.read_bytes():
        return

    log_path = app_root / "data" / "update.log"
    with log_path.open("ab") as log_file:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(new_requirements)],
                cwd=str(app_root),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise UpdateInstallError("更新 Python 依赖超时；旧版程序文件将恢复") from exc
    if result.returncode != 0:
        raise UpdateInstallError("更新 Python 依赖失败；旧版程序文件将恢复，请查看 data/update.log")


def server_version() -> str | None:
    request = urllib.request.Request(
        "http://127.0.0.1:5818/api/version",
        headers={"Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            import json

            return str(json.loads(response.read().decode("utf-8")).get("version") or "")
    except Exception:
        return None


def wait_for_version(expected: str, timeout: float = 35) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_version() == expected:
            return True
        time.sleep(0.5)
    return False


def start_server(app_root: Path, log_path: Path) -> subprocess.Popen[bytes]:
    python = Path(sys.executable)
    server_script = app_root / "app" / "server.py"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        return subprocess.Popen(
            [str(python), str(server_script), "--port", "5818"],
            cwd=str(app_root),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )


def run_update(
    asset_url: str,
    checksum_url: str,
    version: str,
    parent_pid: int,
    app_root: Path,
) -> None:
    app_root = app_root.resolve()
    log_path = app_root / "data" / "update.log"

    with tempfile.TemporaryDirectory(prefix=".xhs-update-", dir=app_root) as temp_name:
        workspace = Path(temp_name)
        package_path = workspace / PACKAGE_ASSET
        checksum_path = workspace / f"{PACKAGE_ASSET}.sha256"
        backup_root = workspace / "transaction"
        download(asset_url, package_path)
        download(checksum_url, checksum_path, max_bytes=4096)
        verify_sha256(package_path, read_expected_sha256(checksum_path))
        validate_archive(package_path)
        log("SHA-256 校验通过，正在安全检查更新包")

        wait_for_process_exit(parent_pid)
        log(f"开始安装版本 {version}")

        installed: list[str] = []
        backed_up: set[str] = set()
        process: subprocess.Popen[bytes] | None = None
        try:
            installed = install_archive(package_path, app_root, backup_root)
            backed_up = {
                relative
                for relative in installed
                if (backup_root / "original" / PurePosixPath(relative)).is_file()
            }
            install_changed_requirements(app_root, backup_root)
            process = start_server(app_root, log_path)
            if not wait_for_version(version):
                raise UpdateInstallError("新版本启动失败，正在恢复旧版本")
        except Exception:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            if installed:
                try:
                    rollback_archive(app_root, backup_root, installed, backed_up)
                except Exception as rollback_error:
                    log(f"恢复旧版文件失败：{rollback_error}")
            try:
                start_server(app_root, log_path)
            except Exception as restart_error:
                log(f"恢复旧版本启动失败：{restart_error}")
            raise

    log(f"更新完成：{version}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset-url", required=True)
    parser.add_argument("--checksum-url", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--app-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        run_update(args.asset_url, args.checksum_url, args.version, args.parent_pid, args.app_root)
        return 0
    except Exception as exc:
        log(f"更新失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
