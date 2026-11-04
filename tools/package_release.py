from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import stat
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "xhs-assistant-mac.zip"
ROOT_FILES = (
    "README.md",
    "安装教程-Mac-M芯片.md",
    "requirements-mac.txt",
    "install.command",
    "start.command",
    "stop.command",
)
SOURCE_DIRS = ("app", "moments")
EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "logs"}
EXCLUDED_PARTS = {"data", "config"}
MAX_PACKAGE_BYTES = 200 * 1024 * 1024


def app_version() -> str:
    source = (ROOT / "app/server.py").read_text("utf-8")
    module = ast.parse(source)
    for node in module.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in targets):
            return str(ast.literal_eval(node.value))
    raise RuntimeError("app/server.py 中未定义静态 APP_VERSION")


def release_version(tag: str) -> str:
    if not re.fullmatch(r"v\d+(?:\.\d+){1,3}", tag):
        raise RuntimeError("Release 标签必须使用 v2026.11.4 这样的数字版本格式")
    return tag[1:]


def collect_files() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for relative in ROOT_FILES:
        path = ROOT / relative
        if not path.is_file():
            raise RuntimeError(f"发布包缺少必要文件：{relative}")
        files.append((path, relative))

    for directory in SOURCE_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            raise RuntimeError(f"发布包缺少必要目录：{directory}")
        for current, dirs, names in os.walk(base, followlinks=False):
            current_path = Path(current)
            kept_dirs = []
            for name in sorted(dirs):
                child = current_path / name
                if name in EXCLUDED_DIRS:
                    continue
                if child.is_symlink():
                    raise RuntimeError(f"发布源代码不能包含符号链接：{child.relative_to(ROOT)}")
                kept_dirs.append(name)
            dirs[:] = kept_dirs

            for name in sorted(names):
                path = current_path / name
                relative = path.relative_to(ROOT).as_posix()
                parts = Path(relative).parts
                if path.is_symlink():
                    raise RuntimeError(f"发布源代码不能包含符号链接：{relative}")
                if any(part in EXCLUDED_PARTS for part in parts):
                    continue
                if name == ".git" or name == ".DS_Store" or name.endswith((".pyc", ".pyo", ".log", ".sqlite3", ".db")):
                    continue
                if name == "calibration.json" and relative == "moments/calibration.json":
                    continue
                if name.startswith(".env"):
                    continue
                if path.is_file():
                    files.append((path, relative))

    unique = {relative: path for path, relative in files}
    if len(unique) != len(files):
        raise RuntimeError("发布包发现重复路径")
    for required in ("app/server.py", "app/updater.py", "app/vendor/xiaohongshu-skills/scripts/xhs/creator.py"):
        if required not in unique:
            raise RuntimeError(f"发布包缺少必要应用文件：{required}")
    return [(unique[name], name) for name in sorted(unique)]


def build_release(tag: str, output_dir: Path) -> tuple[Path, Path]:
    expected = release_version(tag)
    current = app_version()
    if expected != current:
        raise RuntimeError(f"Release 标签版本 {expected} 与 app/server.py 中的 {current} 不一致")

    output_dir.mkdir(parents=True, exist_ok=True)
    package_path = output_dir / PACKAGE_NAME
    checksum_path = output_dir / f"{PACKAGE_NAME}.sha256"
    files = collect_files()
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, relative in files:
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = stat.S_IMODE(path.stat().st_mode)
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    size = package_path.stat().st_size
    if size > MAX_PACKAGE_BYTES:
        package_path.unlink(missing_ok=True)
        raise RuntimeError(f"发布包超过更新器允许的 200 MiB：{size} bytes")
    digest_builder = hashlib.sha256()
    with package_path.open("rb") as package:
        for chunk in iter(lambda: package.read(1024 * 1024), b""):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    checksum_path.write_text(f"{digest}  {PACKAGE_NAME}\n", encoding="ascii")
    return package_path, checksum_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a data-free macOS app update package and SHA-256 sidecar.")
    parser.add_argument("--tag", required=True, help="Version tag, for example v2026.11.4")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    try:
        package, checksum = build_release(args.tag, args.output_dir.resolve())
    except Exception as exc:
        print(f"Release 打包失败：{exc}", file=sys.stderr)
        return 1
    print(f"已生成 {package} 和 {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
