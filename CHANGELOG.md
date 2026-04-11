# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- WizardUI: Rich TUI wizard with phase headers, progress bars, tables, edition panels
- ReportGenerator: JSON migration report, rollback shell script
- MigrationEngine: Full orchestrator with checkpoint/resume, discovery, transforms, export, live migration
- Main entry point wiring with config file loading and error handling
- README documentation

## [0.1.0] - 2026-04-12

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
