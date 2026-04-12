# Portainer to Arcane Migration Tool

A single-file Python CLI wizard that performs a complete migration from
Portainer CE/EE to [Arcane](https://getarcane.app). Auto-detects your Portainer
edition (CE or EE) and adjusts available features accordingly.

## What Gets Migrated

| Resource | Portainer | Arcane | Edition |
|----------|-----------|--------|---------|
| Portainer Backup | Full `.tar.gz` backup | Local safety-net file | CE + EE |
| Container Registries | Registries | Container Registries | CE + EE |
| Git Repositories | Git-based stack configs | Git Repositories | CE + EE |
| Networks | User-created networks | Networks | CE + EE |
| Volumes + Data | Docker volumes | Volumes + backup upload | CE + EE |
| Compose Stacks | Stacks + `.env` | Projects (compose + envContent) | CE + EE |
| Git-based Stacks | Git stacks | GitOps Syncs | CE + EE |
| Standalone Containers | Full inspect | Containers (full fidelity) | CE + EE |
| Custom Templates | Templates + compose | Arcane Templates | CE + EE |
| Users | User accounts | Users | CE + EE |
| Webhooks | Webhooks | Arcane Webhooks | CE + EE (best-effort) |
| Teams / Roles / ACLs | RBAC config | Exported as reference JSON | EE |
| Settings | Full settings | Exported as reference JSON | CE + EE |

## Quick Start

```bash
pip install rich requests
python migrate.py
```

## Requirements

- Python 3.8+
- `rich` and `requests` (auto-installed on first run if missing)
- Portainer API key (generate in Portainer > My Account > Access Tokens)
- Arcane API key or admin credentials
- Optional: Docker CLI on the host (enables volume data backup)

## Usage

```bash
# Interactive wizard (recommended)
python migrate.py

# Export Portainer data only (no import to Arcane)
python migrate.py --export-only

# Simulate without making any changes
python migrate.py --dry-run

# Resume an interrupted migration
python migrate.py --resume

# Import from a previous export (skips Portainer connection, pushes to Arcane)
python migrate.py --import-dir ./migration_export

# Load connection config from file
python migrate.py --config my-config.json

# Skip the Portainer backup step
python migrate.py --skip-backup
```

## Modes

### Export-Only

Saves all Portainer data to `./migration_export/` without touching Arcane.
Useful for auditing, planning, or creating a portable backup of your
Portainer configuration.

### Live Migration

Exports data first, then imports everything into Arcane. Compose stacks are
recreated as Arcane projects, standalone containers are rebuilt with full
config fidelity, and volume data is backed up and restored.

### Dry Run

Simulates the entire migration end-to-end. All reads happen, all transforms
run, but no writes are made to Arcane. Shows exactly what would happen.

## Features

- Auto-detects Portainer CE vs EE and adjusts features
- Interactive TUI wizard with progress bars and colored output
- Checkpoint/resume for interrupted migrations
- Full container fidelity (capabilities, health checks, devices, DNS, resource limits)
- Automatic Docker socket detection (local mode vs API-only remote mode)
- Volume data backup via Docker CLI (when running on the Docker host)
- Portainer backup triggered before any changes (safety net)
- Rollback script generation
- Detailed JSON migration report
- Debug log file for troubleshooting
- Cross-platform: Windows, Linux, macOS

## Config File Format

```json
{
    "portainer_url": "https://portainer.example.com:9443",
    "portainer_api_key": "ptr_...",
    "portainer_endpoint_id": 1,
    "arcane_url": "https://arcane.example.com:3552",
    "arcane_api_key": "...",
    "arcane_environment_id": "0",
    "strategy": "live",
    "dry_run": false,
    "backup_dir": "./migration_export",
    "log_file": "./migration.log"
}
```

## Export Directory Structure

```
migration_export/
├── manifest.json
├── portainer_backup/
│   └── portainer_backup.tar.gz
├── registries/
│   └── registries.json
├── stacks/
│   ├── my-app/
│   │   ├── docker-compose.yml
│   │   ├── .env
│   │   └── metadata.json
│   └── monitoring/
│       ├── docker-compose.yml
│       ├── .env
│       └── metadata.json
├── containers/
│   └── standalone.json
├── networks/
│   └── networks.json
├── volumes/
│   ├── volumes.json
│   └── backups/
│       ├── postgres_data.tar.gz
│       └── redis_data.tar.gz
├── templates/
│   └── custom_templates.json
├── users/
│   └── users.json
├── webhooks/
│   └── webhooks.json
├── settings/
│   └── portainer_settings.json
└── ee_reference/              (EE only)
    ├── teams.json
    ├── team_memberships.json
    ├── roles.json
    └── resource_controls.json
```

## Versioning

This project uses [Semantic Versioning](https://semver.org/).

## License

MIT
