from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def git_apply_check(vendor_dir: Path, patch_file: Path, reverse: bool = False) -> subprocess.CompletedProcess[str]:
    # Upstream contains harmless whitespace-only changes between snapshots;
    # keep the integration patch focused on behavior rather than indentation.
    command = ["git", "-C", str(vendor_dir), "apply", "--ignore-space-change"]
    if reverse:
        command.append("--reverse")
    command.extend(["--check", str(patch_file)])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def prepare(vendor_dir: Path, patch_file: Path) -> bool:
    if not (vendor_dir / ".git").exists():
        raise RuntimeError(f"vendor 目录不是已初始化的 Git 子模块：{vendor_dir}")
    if not patch_file.is_file():
        raise RuntimeError(f"未找到 vendor 集成补丁：{patch_file}")

    already_applied = git_apply_check(vendor_dir, patch_file, reverse=True)
    if already_applied.returncode == 0:
        return False

    applicable = git_apply_check(vendor_dir, patch_file)
    if applicable.returncode != 0:
        detail = (applicable.stderr or applicable.stdout).strip()
        raise RuntimeError(f"vendor 补丁无法应用，可能需要人工解决上游冲突：\n{detail}")

    result = subprocess.run(
        ["git", "-C", str(vendor_dir), "apply", "--ignore-space-change", str(patch_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the app's maintained integration patch to its vendor submodule.")
    parser.add_argument("--vendor-dir", type=Path, default=ROOT / "app/vendor/xiaohongshu-skills")
    parser.add_argument("--patch", type=Path, default=ROOT / "patches/vendor-xiaohongshu-skills.patch")
    args = parser.parse_args()

    try:
        changed = prepare(args.vendor_dir.resolve(), args.patch.resolve())
    except Exception as exc:
        print(f"vendor 准备失败：{exc}", file=sys.stderr)
        return 1
    print("已应用本地 vendor 补丁" if changed else "vendor 补丁已存在，无需重复应用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
