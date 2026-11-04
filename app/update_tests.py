from __future__ import annotations

import hashlib
import stat
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ROOT))

import server
import updater
from update_service import UpdateError, check_for_update, version_key
from updater import (
    UpdateInstallError,
    install_archive,
    install_changed_requirements,
    read_expected_sha256,
    rollback_archive,
    validate_archive,
    validate_asset_url,
    verify_sha256,
)
from tools import package_release


REPOSITORY = "lengjueqi-coder/xiaohongshu-publisher"
ASSET_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/assets/123"
CHECKSUM_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/assets/124"


def release_payload(version: str = "v2026.11.5", include_assets: bool = True) -> dict:
    assets = []
    if include_assets:
        assets = [
            {"name": "xhs-assistant-mac.zip", "url": ASSET_URL},
            {"name": "xhs-assistant-mac.zip.sha256", "url": CHECKSUM_URL},
        ]
    return {
        "tag_name": version,
        "name": f"小红书发布软件 {version}",
        "body": "更新测试",
        "published_at": "2026-11-04T00:00:00Z",
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{version}",
        "draft": False,
        "prerelease": False,
        "assets": assets,
    }


def write_package(path: Path, extra: dict[str, bytes] | None = None) -> None:
    entries = {
        "app/server.py": b"new server",
        "app/updater.py": b"updater",
        "start.command": b"#!/bin/zsh\n",
        "requirements-mac.txt": b"requests>=2\n",
    }
    entries.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


class UpdateServiceTests(unittest.TestCase):
    def test_numeric_version_ordering(self) -> None:
        self.assertGreater(version_key("v2026.11.5"), version_key("2026.11.4"))
        self.assertEqual(version_key("2026.11"), version_key("2026.11.0"))
        with self.assertRaises(ValueError):
            version_key("latest")

    def test_release_requires_expected_checksum_asset(self) -> None:
        result = check_for_update("2026.11.4", release_payload())
        self.assertTrue(result["update_available"])
        self.assertTrue(result["package_ready"])
        missing = check_for_update("2026.11.4", release_payload(include_assets=False))
        self.assertFalse(missing["package_ready"])

    def test_prerelease_is_not_installable(self) -> None:
        release = release_payload()
        release["prerelease"] = True
        with self.assertRaises(UpdateError):
            check_for_update("2026.11.4", release)

    def test_asset_urls_are_pinned_to_the_public_release_path(self) -> None:
        validate_asset_url(ASSET_URL)
        with self.assertRaises(UpdateInstallError):
            validate_asset_url("https://example.com/xhs-assistant-mac.zip")
        with self.assertRaises(UpdateInstallError):
            validate_asset_url("https://api.github.com/repos/other/repo/releases/assets/123")


class UpdaterArchiveTests(unittest.TestCase):
    def test_download_error_leaves_the_current_server_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            updater, "download", side_effect=UpdateInstallError("network unavailable")
        ), patch.object(updater, "wait_for_process_exit") as stop_server:
            with self.assertRaises(UpdateInstallError):
                updater.run_update(ASSET_URL, CHECKSUM_URL, "2026.11.5", 1234, Path(tmp))
            stop_server.assert_not_called()

    def test_install_error_attempts_to_restore_the_old_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            (app_root / "data").mkdir()

            def fake_download(_url: str, target: Path, **_kwargs: object) -> None:
                target.write_bytes(b"test")

            with patch.object(updater, "download", side_effect=fake_download), patch.object(
                updater, "read_expected_sha256", return_value="0" * 64
            ), patch.object(updater, "verify_sha256"), patch.object(
                updater,
                "validate_archive",
                return_value=["app/server.py", "app/updater.py", "start.command", "requirements-mac.txt"],
            ), patch.object(updater, "wait_for_process_exit"), patch.object(
                updater, "install_archive", side_effect=UpdateInstallError("replace failed")
            ), patch.object(updater, "start_server") as restart:
                with self.assertRaises(UpdateInstallError):
                    updater.run_update(ASSET_URL, CHECKSUM_URL, "2026.11.5", 1234, app_root)
                restart.assert_called_once()

    def test_rejects_traversal_and_local_state_paths(self) -> None:
        for path in ("../outside.txt", "data/assistant.sqlite3", "config/settings.json", ".venv/bin/python"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                package = Path(tmp) / "package.zip"
                write_package(package, {path: b"not allowed"})
                with self.assertRaises(UpdateInstallError):
                    validate_archive(package)

    def test_rejects_symlinks_and_duplicate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "symlink.zip"
            write_package(package)
            with zipfile.ZipFile(package, "a") as archive:
                info = zipfile.ZipInfo("app/link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, "../../outside")
            with self.assertRaises(UpdateInstallError):
                validate_archive(package)

            duplicate = Path(tmp) / "duplicate.zip"
            write_package(duplicate)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "a") as archive:
                    archive.writestr("app/server.py", b"second copy")
            with self.assertRaises(UpdateInstallError):
                validate_archive(duplicate)

    def test_install_preserves_local_files_and_can_roll_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app-root"
            backup = Path(tmp) / "transaction"
            package = Path(tmp) / "package.zip"
            (root / "app").mkdir(parents=True)
            (root / "data").mkdir()
            (root / "config").mkdir()
            (root / "moments").mkdir()
            (root / ".venv").mkdir()
            (root / "app/server.py").write_bytes(b"old server")
            (root / "data/assistant.sqlite3").write_bytes(b"private database")
            (root / "config/settings.json").write_bytes(b"private settings")
            (root / "moments/calibration.json").write_bytes(b"private calibration")
            (root / ".venv/marker").write_bytes(b"private environment")
            write_package(package, {"moments/engine.js": b"new moments"})

            installed = install_archive(package, root, backup)
            self.assertEqual((root / "app/server.py").read_bytes(), b"new server")
            self.assertEqual((root / "data/assistant.sqlite3").read_bytes(), b"private database")
            self.assertEqual((root / "config/settings.json").read_bytes(), b"private settings")
            self.assertEqual((root / "moments/calibration.json").read_bytes(), b"private calibration")
            self.assertEqual((root / ".venv/marker").read_bytes(), b"private environment")

            backed_up = {
                relative
                for relative in installed
                if (backup / "original" / Path(relative)).is_file()
            }
            rollback_archive(root, backup, installed, backed_up)
            self.assertEqual((root / "app/server.py").read_bytes(), b"old server")
            self.assertFalse((root / "moments/engine.js").exists())

    def test_sha256_sidecar_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "package.zip"
            checksum = Path(tmp) / "package.zip.sha256"
            package.write_bytes(b"release bytes")
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            checksum.write_text(f"{digest}  xhs-assistant-mac.zip\n", encoding="ascii")
            verify_sha256(package, read_expected_sha256(checksum))
            with self.assertRaises(UpdateInstallError):
                verify_sha256(package, "0" * 64)

    def test_changed_dependencies_are_installed_but_unchanged_ones_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "transaction/original/requirements-mac.txt"
            new = root / "app/requirements-mac.txt"
            (root / "transaction/original").mkdir(parents=True)
            (root / "app/data").mkdir(parents=True)
            old.write_text("requests==1\n", encoding="utf-8")
            new.write_text("requests==2\n", encoding="utf-8")
            with patch("updater.subprocess.run") as run:
                run.return_value.returncode = 0
                install_changed_requirements(root / "app", root / "transaction")
                run.assert_called_once()
                old.write_text("requests==2\n", encoding="utf-8")
                run.reset_mock()
                install_changed_requirements(root / "app", root / "transaction")
                run.assert_not_called()


class UpdateQueueTests(unittest.TestCase):
    def test_updates_are_blocked_while_queue_is_enabled_or_submitting(self) -> None:
        with patch.object(server, "db_get_setting", return_value=True):
            self.assertIn("暂停", server.update_install_block_reason())

        with patch.object(server, "db_get_setting", return_value=False), patch.object(
            server, "queue_rows", return_value=[{"status": "submitting"}]
        ):
            self.assertIn("暂停", server.update_install_block_reason())

    def test_updates_can_proceed_when_queue_is_paused_and_idle(self) -> None:
        with patch.object(server, "db_get_setting", return_value=False), patch.object(
            server, "queue_rows", return_value=[{"status": "queued"}]
        ):
            self.assertEqual(server.update_install_block_reason(), "")


class ReleasePackageTests(unittest.TestCase):
    def test_release_archive_is_versioned_and_excludes_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package, checksum = package_release.build_release(
                f"v{server.APP_VERSION}", Path(tmp)
            )
            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())
            self.assertIn("app/server.py", names)
            self.assertIn("app/updater.py", names)
            self.assertIn("app/vendor/xiaohongshu-skills/scripts/xhs/creator.py", names)
            self.assertNotIn("moments/calibration.json", names)
            self.assertFalse(any(name.startswith(("data/", "config/", ".venv/")) for name in names))
            self.assertFalse(any("/data/" in name or "/config/" in name for name in names))
            verify_sha256(package, read_expected_sha256(checksum))

    def test_release_tag_must_match_application_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                package_release.build_release("v1.0.0", Path(tmp))


if __name__ == "__main__":
    unittest.main()
