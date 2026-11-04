# ADR-0001: Use GitHub Releases for In-App Updates

## Status
Accepted

## Context

The assistant is a local Python web app distributed to friends on Apple Silicon Macs. Copies must be able to discover and install updates without Git or a GitHub account. Each computer has private state in `data/`, `config/`, `.venv/`, and `moments/calibration.json`; an update must not replace it. An interrupted or invalid package must not leave the app half updated.

## Decision

Publish versioned source packages as GitHub Release assets. The app checks the latest public release, downloads the named ZIP and its SHA-256 sidecar, verifies the bytes, then starts a separate updater process. The helper waits for the server to exit, installs only managed application paths with rollback backups, and starts the server again. Releases are public so friends do not need GitHub credentials.

The repo keeps the upstream automation project as a Git submodule and tracks local integration changes as a patch. The release workflow applies the patch and packages the fully populated dependency so normal installation does not need Git.

## Consequences

### Positive
- Friends can check and install updates from inside the app without GitHub accounts.
- SHA-256 validation catches corrupted or substituted downloads.
- User databases, API keys, assets, virtual environments, and local calibration remain outside the update package.
- A failed file replacement can restore the previous application files and restart the prior version.

### Negative
- The public repository exposes source code and release history.
- Public GitHub availability is required to check and download releases.
- The initial update-enabled version must be installed manually once on older copies.
- Upstream dependency patches need to be carried forward when refreshing the submodule.

## Alternatives Considered

**`git pull` from the app:** rejected because friends would need Git and repository access, and local data or edits could conflict.

**Download and replace from the browser:** rejected because the browser cannot safely coordinate server shutdown, path validation, rollback, and restart.

**Private GitHub repository:** rejected because friend installations would need authenticated release access and credential distribution.

## References

- GitHub REST API: latest release endpoint
- `docs/plans/2026-11-04-github-release-updates.md`
