# GitHub Release Updates Implementation Plan

> **For Codex:** Execute each task in order, keep local user data out of the release, and run the listed checks before publishing.

**Goal:** Let the assistant notify friends of a newer public GitHub Release, verify its package, install it while preserving local state, and restart automatically.

**Architecture:** The Python server checks GitHub's latest-release API. A separate helper downloads a ZIP and checksum, waits for the server to stop, safely replaces only application-managed files with rollback support, and restarts the local service. GitHub Actions creates a ZIP and SHA-256 sidecar for each version tag.

**Tech Stack:** Python standard library, SQLite-backed existing app, Git submodule, GitHub Releases and Actions.

---

### Task 1: Define repository boundaries

Files: `.gitignore`, `.gitmodules`, `patches/`, `tools/prepare_vendor.py`, this plan, `docs/adr/0001-github-release-updates.md`.

Track app source and docs only. Keep `.venv`, all `data/`, `config/`, user calibration, real test logs, generated images, and secrets local. Preserve the vendored upstream Git history as a submodule and record local vendor changes as a patch plus its untracked integration file.

Verification: inspect the staged path list and scan it for API keys, database files, images, and local absolute paths.

### Task 2: Add update discovery and installation

Files: `app/update_service.py`, `app/updater.py`, `app/server.py`, `app/tests.py`.

Parse numeric release tags, require the expected ZIP and checksum assets, and reject an install while publishing is active. The helper accepts only GitHub Release URLs, verifies SHA-256, rejects unsafe ZIP paths and symlinks, preserves local state, and rolls back if installation or restart fails.

Verification: unit tests for version ordering, missing assets, checksum mismatch, path traversal, preservation of local files, rollback, and blocked installs during an active queue.

### Task 3: Add the in-app update flow

Files: `app/templates/index.html`, `app/static/app.js`, `app/static/styles.css`, `app/server.py`.

Check quietly at startup and provide a manual check control. Show a compact update banner with version and release notes, confirm the restart, then wait for the new server version and reload the page.

Verification: JavaScript syntax check, API smoke tests, and manual browser check of both current and newer version states.

### Task 4: Package and publish releases

Files: `tools/package_release.py`, `.github/workflows/release.yml`, `README.md`, `安装教程-Mac-M芯片.md`, `start.command`.

Build a deterministic package from a tag, verify tag and app versions match, exclude local data, and create the release ZIP and checksum. Publish under the public repository `lengjueqi-coder/xiaohongshu-publisher`.

Verification: package inspection, checksum verification, full unit and syntax checks, GitHub Release asset validation, and updater API check against the published release.
