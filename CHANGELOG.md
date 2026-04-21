# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-04-21

### Fixed
- **Credentials no longer leak to the debug log**. `ArcaneClient._request`
  and `MigrationEngine._execute_or_log` previously logged request bodies
  verbatim, echoing registry passwords, user passwords, git tokens, and
  AWS keys into the on-disk log file. Both paths now redact any value
  under a secret-looking key.
- **Checkpoint writes are atomic**. `_save_state` now writes to a sibling
  temp file, fsyncs, and `os.replace`s. The previous non-atomic write
  could be truncated by an interrupt, which combined with the silent
  reset below caused every already-migrated item to be re-created on
  `--resume`.
- **A corrupt checkpoint fails loud** instead of silently falling back
  to fresh state. The old behaviour produced duplicate Arcane resources.
- **Portainer backup streams to disk** with a 600s timeout. The previous
  in-memory `resp.content` at 120s OOM'd or timed out on real-world
  installs.
- **Arcane volume-backup upload timeout scales to file size** (10-minute
  floor, 4-hour cap) instead of a fixed 60s that silently truncated
  multi-GB tarballs.
- **Every Arcane list endpoint now paginates**. Arcane's default page
  size is 20; previously the tool silently stopped at 20 items per
  resource type. Added `_list_paginated` that loops through the
  start/limit response shape and wires it into `list_environments`,
  `list_registries`, `list_git_repos`, `list_projects`, `list_networks`,
  `list_volumes`, `list_containers`, `list_users`, `list_webhooks`,
  `list_templates`.
- **Silent stack compose-file fetch failures are now reported**. Previous
  bare `except Exception` wrote an empty `docker-compose.yml` with no
  warning. Now logs, records a migration failure, and warns the user.
- **Volume-backup upload failures and container-start failures** are now
  recorded as migration failures so the final report surfaces them.
  Previously both were only warnings, producing reports that claimed
  success for resources whose data or runtime state was actually missing.
- **Transient Portainer errors retry with exponential backoff**.
  `PortainerClient._get` retries `ConnectionError` / `Timeout` up to
  three times (1s / 2s / 4s). HTTP 4xx/5xx are not retried.
- **Discovery tolerates per-resource failure**. `MigrationEngine.discover`
  records a "discovery failed" marker for a single flaky endpoint
  instead of aborting the whole phase.
- **403 on Portainer is surfaced as a warning** rather than silently
  degrading to empty like 404 (which only means "EE-only endpoint
  missing on CE"). A 403 usually means the API key is missing permission
  and the data would otherwise be invisibly incomplete.
- **Endpoint/environment pickers no longer clamp invalid input** — they
  loop until a valid row number is entered. Clamping a destructive target
  selection to the first/last row was a foot-gun.
- **Dependency bootstrap no longer hides pip errors**. Dropped
  `--quiet`, caught `EOFError` on non-TTY stdin, and replaced `os.execv`
  with a subprocess spawn (Windows semantics for `os.execv` differ).
- **Reverse-proxy HTML error pages now raise a clear message** instead
  of an opaque `JSONDecodeError`.
- **HTTP sessions close in a `finally` block** so connections don't
  linger through Rich's terminal teardown.
- **Checkpoint cleanup TOCTOU** — `os.remove(cp)` paths guard against
  `FileNotFoundError`.

### Changed
- **`migrate.sh` Python detection** now probes
  `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `/usr/bin/python3`,
  and `py -3` in addition to `PATH` entries. Previously a fresh macOS
  without Homebrew's shims on `PATH` failed with "Python not found".
- **`migrate.sh` pip failures are visible** (no more `--quiet`). Adds a
  PEP 668 ("externally-managed-environment") hint when pip install fails,
  so Ubuntu 24.04 / modern Homebrew users know to reach for a venv.
- **`migrate.sh` Docker probe** distinguishes "Docker CLI missing" from
  "daemon not running" from "socket permission denied" and prints the
  relevant fix for each.
- **Phase headers show a persistent [DRY RUN] or [EXPORT-ONLY] badge**
  so the active mode is visible on every phase, not just at launch.
- **Connection errors print targeted hints** (SSL, DNS, connection
  refused, 401/403, timeout, http:// scheme) via a new
  `WizardUI.diagnose_connection_error` helper.
- **Rollback script instructions** surface in the final report so users
  know to paste their Arcane API key in place of `YOUR_API_KEY_HERE`
  before running `rollback.sh`.

## [0.3.0] - 2026-04-11

### Added
- `--import-dir PATH` feature: import from a previously exported migration directory
  - Skips Portainer connection entirely (no Portainer URL/API key needed)
  - Reads discovery data from exported JSON files and manifest.json
  - Reads compose files from disk instead of calling Portainer API
  - Reads container inspect data from standalone.json instead of Portainer API
  - Reads custom template content from exported data instead of Portainer API
  - Forces strategy to "live" (push to Arcane)
  - Still requires Arcane connection for the target environment
- `config.import_mode` flag on Config dataclass

## [0.2.0] - 2026-04-11

### Added
- `migrate.sh` launcher script
  - `--help` with full usage, options, examples, and prerequisites
  - `--check-only` to validate environment without launching
  - Checks Python 3.8+ availability across `python3` and `python` candidates
  - Checks and installs pip via `ensurepip` if missing
  - Auto-installs `rich` and `requests` from `requirements.txt`
  - Detects Docker availability and reports mode (local vs API-only)
  - Passes all CLI args through to `migrate.py` (strips launcher-only flags)
  - Color output with graceful degradation on non-TTY
  - Platform-specific install guidance (macOS/Linux/Windows)
- `WizardUI`: Rich TUI wizard with phase headers, progress bars, tables, edition panels
- `ReportGenerator`: JSON migration report, rollback shell script generation
- `MigrationEngine`: Full orchestrator with checkpoint/resume, discovery, transforms, export, live migration
  - 13 migration phases (portainer backup, registries, git repos, networks, volumes, stacks, GitOps syncs, containers, templates, users, webhooks, EE RBAC export, EE audit export)
  - Export-to-disk with full directory structure
  - Edition-aware CE/EE branching throughout
  - Pre-flight checks (Arcane health, naming conflicts, disk space)
  - Full-fidelity container transform (capabilities, health checks, devices, DNS, resource limits, mount modes)
- Main entry point wiring with config file loading and error handling
- `.gitignore` for Python cache, runtime artifacts, env files
- `docs/plans/` with complete implementation plan (design, UI/UX wireframes, API mappings)
- `README.md`, `CHANGELOG.md`, `CLAUDE.md` documentation

## [0.1.0] - 2026-04-11

### Added
- Initial project scaffold (`migrate.py`, `requirements.txt`)
- `Config` dataclass with Portainer CE/EE + Arcane connection settings, platform detection
- CLI argument parsing (`--resume`, `--import-dir`, `--dry-run`, `--export-only`, `--skip-backup`, `--config`)
- Dual logging: Rich console (INFO) + file (DEBUG)
- `PortainerClient` with all CE+EE read-only API methods
  - Edition auto-detection via `GET /api/status`
  - `_safe_get()` for graceful 404 handling on EE-only endpoints
  - Docker proxy methods (containers, images, volumes, networks)
  - Portainer backup trigger (`POST /api/backup`)
  - EE-only: webhooks, teams, roles, resource controls, edge stacks
- `ArcaneClient` with all read/write API methods
  - Dual auth: API key (`X-API-Key`) or JWT Bearer token
  - Full CRUD: environments, registries, git repos, projects, GitOps syncs,
    networks, volumes (+ backup upload), containers, users, webhooks,
    notifications, settings, templates
- `DockerLocal` for local Docker socket operations
  - Volume backup via `docker run alpine tar czf`
  - Volume size estimation
  - Compose container listing by project label
