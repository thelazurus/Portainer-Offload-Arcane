# Portainer-to-Arcane Migration Tool

## Project Overview

Single-file Python CLI wizard (`migrate.py`) that migrates all Docker resources
from Portainer CE/EE to Arcane. Built with `rich` for the TUI and `requests`
for HTTP.

## Architecture

One file, seven classes:

```
migrate.py
├── Config              Dataclass: connection info, flags, edition, state
├── PortainerClient     Read-only Portainer API (CE+EE with graceful fallback)
├── ArcaneClient        Read/write Arcane API (all CRUD operations)
├── DockerLocal         Docker socket ops (volume backup, size estimation)
├── WizardUI            Rich TUI (banners, tables, prompts, progress, edition panels)
├── ReportGenerator     JSON report + rollback script + console summary
├── MigrationEngine     Orchestrator (phases, checkpoints, transforms, CE/EE branching)
└── main()              Argparse, config init, engine.run()
```

## Key Design Decisions

- **Single file**: Easy to distribute. Run anywhere with `pip install rich requests && python migrate.py`.
- **CE/EE auto-detect**: `GET /api/status` returns edition. EE-only endpoints use `_safe_get()` which returns empty on 404/403 instead of crashing. CE users never see EE features.
- **Checkpoint/resume**: State saved to `migration_state.json` after each sub-phase. Each item tracked individually. Resume skips completed phases.
- **Dry-run**: Every write wraps in `_execute_or_log()`. All reads happen normally; only writes are suppressed.
- **Export-first**: Even in live migration mode, data is exported to disk before any Arcane writes. The export serves as a backup.

## Portainer API Patterns

- Base URL: `{portainer_url}/api`
- Auth: `X-API-Key` header
- Docker resources proxied via: `/api/endpoints/{endpoint_id}/docker/...`
- Stacks: `GET /api/stacks` (Type 2 = compose), `GET /api/stacks/{id}/file` for compose YAML
- Stack env vars: `stack.Env[]` array of `{"name": "KEY", "value": "VAL"}`
- EE-only endpoints (404 on CE): `/api/webhooks`, `/api/teams`, `/api/roles`, `/api/resource_controls`, `/api/edge_stacks`

## Arcane API Patterns

- Base URL: `{arcane_url}/api`
- Auth: `X-API-Key` header OR `Authorization: Bearer {jwt}`
- Environment ID "0" = local environment
- Projects (stacks): `POST /environments/{id}/projects` with `composeContent` + `envContent`
- Env vars are a raw `.env` string in `envContent` field, not an array

## Common Operations

```bash
# Run the wizard
python migrate.py

# Run with all checks but no actual changes
python migrate.py --dry-run

# Export only (no Arcane writes)
python migrate.py --export-only

# Resume after interruption
python migrate.py --resume
```

## Versioning

Uses [Semantic Versioning](https://semver.org/). Version constant is `__version__` in `migrate.py`.
Changelog is in `CHANGELOG.md`.

## Testing

No test suite -- this is a migration tool meant to run once. Verify with:
```bash
python migrate.py --help
python migrate.py --version
python -c "from migrate import Config, PortainerClient, ArcaneClient, DockerLocal, WizardUI, ReportGenerator, MigrationEngine; print('OK')"
```

## Files

```
migrate.py              The migration tool (single file, all classes)
requirements.txt        Python dependencies (rich, requests)
README.md               User-facing documentation
CHANGELOG.md            Version history (semver)
CLAUDE.md               This file (dev context for Claude)
docs/plans/             Design + implementation plan
```
