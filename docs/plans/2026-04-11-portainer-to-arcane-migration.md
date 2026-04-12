# Portainer to Arcane -- Complete Migration Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

```
    ____            __        _                      __          ___
   / __ \____  ____/ /_____ _(_)___  ___  _____     / /_____    /   |  _____________  ____  ___
  / /_/ / __ \/ __  / __/ _` / / __ \/ _ \/ ___/   / __/ __ \  / /| | / ___/ ___/ _ `/ __ \/ _ \
 / ____/ /_/ / /_/ / /_/ (_| / / / / /  __/ /      / /_/ /_/ / / ___ |/ /  / /__/ (_| / / / /  __/
/_/    \____/\__,_/\__/\__,_/_/_/ /_/\___/_/       \__/\____/ /_/  |_/_/   \___/\__,_/_/ /_/\___/

                         Migration Wizard v1.0.0
```

---

## Table of Contents

1. [Goal & Architecture](#1-goal--architecture)
2. [What Gets Migrated](#2-what-gets-migrated)
3. [User Interface & Experience](#3-user-interface--experience)
4. [Wizard Flow -- Screen by Screen](#4-wizard-flow----screen-by-screen)
5. [API Mapping & Data Transforms](#5-api-mapping--data-transforms)
6. [Internal Architecture](#6-internal-architecture)
7. [Implementation Tasks](#7-implementation-tasks)

---

## 1. Goal & Architecture

**Goal:** A single-file Python CLI wizard (`migrate.py`) that performs a complete
migration from Portainer CE/EE to Arcane. Guided, visual, recoverable.

**Architecture:** Single `migrate.py` (~2500 lines) with 7 internal classes.
Uses `rich` for an interactive TUI. Auto-detects local Docker socket vs API-only
remote mode.

**Tech Stack:**
```
Python 3.8+
rich >= 13.0    # TUI: panels, tables, progress, prompts, trees
requests >= 2.28 # HTTP for both APIs
```

**Key Capabilities:**
- Auto-detects Portainer CE vs EE and adjusts available features
- Interactive wizard with step indicators and progress bars
- Export-only mode (safe audit) or live migration (full cutover)
- Dry-run simulation mode
- Checkpoint/resume for interrupted migrations
- Portainer backup triggered before any changes
- Rollback script generation
- Cross-platform: Windows, Linux, macOS

**CLI:**
```
python migrate.py                  # Interactive wizard
python migrate.py --resume         # Resume from checkpoint
python migrate.py --import-dir ./  # Import from prior export
python migrate.py --dry-run        # Simulate only
python migrate.py --export-only    # Export, don't import
python migrate.py --skip-backup    # Skip Portainer backup
python migrate.py --config f.json  # Load saved config
```

---

## 2. What Gets Migrated

```
 SOURCE (Portainer)                    TARGET (Arcane)            EDITION
 ==================                    ===============            =======

 +-- Portainer Backup -.tar.gz ------> Local safety-net file     CE + EE
 |
 +-- Container Registries -----------> Container Registries      CE + EE
 +-- Git Repo configs (from stacks) -> Git Repositories          CE + EE
 +-- User-created Networks ----------> Networks                  CE + EE
 +-- Docker Volumes + data ----------> Volumes + backup upload   CE + EE
 +-- Compose Stacks + .env ----------> Projects (compose + env)  CE + EE
 +-- Git-based Stacks ---------------> GitOps Syncs              CE + EE
 +-- Standalone Containers ----------> Containers (full fidelity)CE + EE
 +-- Custom Templates ---------------> Arcane Templates          CE + EE
 +-- Users --------------------------> Users                     CE + EE
 +-- Webhooks -----------------------> Webhooks                  EE only *
 +-- RBAC Teams ---------------------> Exported as reference     EE only
 +-- RBAC Roles ---------------------> Exported as reference     EE only
 +-- Activity Logs ------------------> Exported as reference     EE only
 +-- Settings -----------------------> Exported as reference JSON CE + EE

 * Webhooks: EE-only in Portainer < 2.19. CE 2.19+ has partial support.
   The wizard auto-detects and skips gracefully if unavailable.
```

### CE vs EE -- What the Wizard Does Differently

The wizard detects edition via `GET /api/status` (returns `Edition` field) and
adjusts its behavior:

| Feature | CE Behavior | EE Behavior |
|---------|-------------|-------------|
| **Webhooks** | Try endpoint; skip with info if 404 | Full migration |
| **Teams / RBAC** | Skip (not available) | Export teams, roles, memberships as reference JSON |
| **Activity Logs** | Skip (not available) | Offer to export audit log as reference |
| **Registry mgmt config** | Basic fields only | Include `ManagementConfiguration` in export |
| **Edge Stacks** | Skip (not available) | Detect and warn -- export for reference, no Arcane equivalent |
| **Custom template ACLs** | Not present | Strip ACL metadata, migrate content only |
| **Resource controls** | Not present | Export per-resource ACLs as reference JSON |

**NOT migrated** (no Arcane equivalent -- documented in wizard):
```
 CE + EE:
   Endpoint Groups      Arcane uses flat environment list
   SSL Certificates     Portainer UI TLS, not workload

 EE only (exported as reference, not imported):
   Teams / Memberships  Arcane has user-role RBAC, not team-based
   Granular Roles       Arcane has admin/user only
   Resource ACLs        Different permission model
   Edge Groups/Jobs     Edge agent architecture differs
   Edge Stacks          No Arcane equivalent
   Activity/Audit Logs  Operational history, not config
```

---

## 3. User Interface & Experience

### 3.1 Design Principles (Terminal TUI)

| Principle | Implementation |
|-----------|---------------|
| **Always show progress** | Phase indicator `[2/7]` in every header. Overall progress bar during execution. |
| **Immediate feedback** | Spinners for API calls. Checkmarks/crosses appear as each item completes. |
| **Clear error recovery** | Every error shows what failed + what to do next. Never a dead end. |
| **Confirm before destructive** | Live migration and backup deletion require explicit `y/N` confirmation. |
| **Scan before you commit** | Discovery + dry-run happen before any writes. User sees exactly what will happen. |
| **Respect the terminal** | Max 80-col layouts. No horizontal scroll. Colors degrade gracefully. |

### 3.2 Color Language

```
 cyan    Informational, headers, phase titles
 green   Success, completed items, checkmarks
 yellow  Warnings, dry-run labels, skipped items
 red     Errors, failures, critical warnings
 dim     Secondary text, paths, metadata
 bold    Emphasis, counts, important values
 blue    Prompts, interactive elements
```

### 3.3 Iconography (Unicode)

```
 OK / success .... checkmark
 Error / fail .... cross mark
 Warning ......... triangle warning
 Info ............ info circle
 Arrow ........... right arrow
 Bullet .......... circle bullet
 Spinner ......... rich spinner (dots/line)
```

### 3.4 Component Library

**Phase Header** -- Used at the start of every phase:
```
 ──────────────────── Phase 2 of 7: Discovery & Audit ────────────────────
```

**Status Line** -- Inline feedback after each operation:
```
   [checkmark] Registry 'ghcr.io' migrated (0.3s)
   [cross] Registry 'ecr-prod' failed: 403 Forbidden
   [triangle] Volume 'redis-data' skipped: already exists
   [DRY RUN] Would create project 'my-app'
```

**Confirmation Box** -- Before irreversible actions:
```
 +---------------------------------------------------------------------+
 |                                                                     |
 |  You are about to start a LIVE MIGRATION.                           |
 |                                                                     |
 |  This will create resources in Arcane and may briefly                |
 |  affect running containers during stack re-deployment.              |
 |                                                                     |
 |  A Portainer backup has been saved to:                              |
 |  ./migration_export/portainer_backup/portainer_backup.tar.gz        |
 |                                                                     |
 +---------------------------------------------------------------------+
 Proceed with live migration? [y/N]:
```

**Progress Bar** -- During execution phases:
```
 Migrating resources  [################............]  58%  5e: Stacks
```

**Resource Table** -- For discovery summary:
```
 +---------------------+-------+-----------------------------------------+
 | Resource            | Count | Details                                 |
 +---------------------+-------+-----------------------------------------+
 | Stacks              |    12 | 8 file-based, 4 git-based               |
 | Standalone Cont.    |     3 | (15 compose-managed excluded)           |
 | Images              |    47 | 12.3 GB total                           |
 | Volumes             |    18 |                                         |
 | Networks            |     6 | (3 default excluded)                    |
 | Registries          |     2 | ghcr.io, docker.io                      |
 | Custom Templates    |     5 | nginx-proxy, postgres, redis            |
 | Users               |     4 | admin, deploy-bot, dev1, dev2           |
 | Webhooks            |     1 |                                         |
 | Settings            |     1 | Exported for reference                  |
 +---------------------+-------+-----------------------------------------+
```

---

## 4. Wizard Flow -- Screen by Screen

### Phase 0: Welcome & Prerequisites

```
 +=====================================================================+
 |                                                                     |
 |       Portainer to Arcane Migration Tool  v1.0.0                    |
 |       2026-04-11 14:30                                              |
 |                                                                     |
 +=====================================================================+

  Platform ....... macOS (Darwin)
  Docker ......... Available (/var/run/docker.sock)
  Python ......... 3.11.4
  Log file ....... migration_20260411_143022.log


```

**What happens:**
1. Display banner with version and timestamp
2. Detect platform (Windows/Linux/macOS)
3. Check Python >= 3.8
4. Check `rich` and `requests` are installed; offer `pip install` if not
5. Detect Docker socket; set `has_docker` flag
6. Initialize log file

**Error state -- missing deps:**
```
  [cross] Missing dependencies: rich>=13.0.0, requests>=2.28.0
  Install now with pip? [Y/n]: y
  [checkmark] Dependencies installed. Restarting...
```

---

### Phase 1: Connection Setup

```
 ──────────────── Phase 1 of 7: Connection Setup ─────────────────


 Portainer Connection
 ────────────────────
  Portainer URL [https://portainer.example.com:9443]: https://port.myhost.com:9443
  Portainer API Key: ****************************
  Verify SSL? [Y/n]: y

  [spinner] Testing connection...
  [checkmark] Connected to Portainer EE v2.39.1

 +---------------------------------------------------------------------+
 |  EDITION DETECTED: Portainer Business (EE)                          |
 |                                                                     |
 |  EE features available for migration:                               |
 |    [checkmark] Webhooks                                             |
 |    [checkmark] RBAC Teams & Roles (exported as reference)           |
 |    [checkmark] Activity Logs (exported as reference)                |
 |    [checkmark] Edge Stacks (detected + warned if present)           |
 +---------------------------------------------------------------------+


 Portainer Environments
 +---+----+------------------+------------------------------+--------+
 | # | ID | Name             | URL                          | Status |
 +---+----+------------------+------------------------------+--------+
 | 1 |  1 | local            | /var/run/docker.sock         | Up     |
 | 2 |  5 | staging-server   | tcp://192.168.1.50:2376      | Up     |
 +---+----+------------------+------------------------------+--------+
  Select environment # [1]: 1
  [checkmark] Selected: local (endpoint ID 1)


 Arcane Connection
 ─────────────────
  Arcane URL [https://arcane.example.com:3552]: https://arcane.myhost.com:3552
  Auth method (apikey/login) [apikey]: login
  Username [admin]: admin
  Password: ************

  [spinner] Authenticating...
  [checkmark] Connected to Arcane v1.17.0

  [info] Using Arcane environment: Local (ID: 0)


```

**CE variant of the edition panel:**
```
 +---------------------------------------------------------------------+
 |  EDITION DETECTED: Portainer Community (CE)                         |
 |                                                                     |
 |  CE limitations:                                                    |
 |    [triangle] Webhooks may not be available (CE < 2.19)             |
 |    [triangle] Teams, Roles, Activity Logs -- not available in CE    |
 |    [info] All core resources (stacks, containers, volumes,          |
 |           networks, registries, users) fully supported              |
 +---------------------------------------------------------------------+
```

**What happens:**
1. Prompt for Portainer URL, API key, SSL toggle
2. Test Portainer connection via `GET /api/status`
3. **Detect edition** from `status.Edition` field (`"CE"` or `"EE"`)
4. Store `config.portainer_edition` (`"CE"` or `"EE"`)
5. Display edition panel showing what's available / limited
6. List Portainer endpoints in a table
7. User selects which endpoint to migrate
8. Prompt for Arcane URL, auth method (API key or login)
9. If login: prompt username/password, call `POST /auth/login`
10. Test Arcane connection -- show version on success
11. Auto-select Arcane environment (or let user pick if multiple)

**Error state -- bad credentials:**
```
  [spinner] Testing connection...
  [cross] Cannot connect to Portainer: 401 Unauthorized

  Check your API key and try again.
  Portainer API Key: ****************************
  [spinner] Testing connection...
  [checkmark] Connected to Portainer EE v2.39.1
```

**Error state -- unreachable:**
```
  [cross] Cannot connect to Portainer: Connection refused
         Is the URL correct? Is Portainer running?

  Portainer URL [https://portainer.example.com:9443]:
```

**Error state -- edition detection fails:**
```
  [triangle] Could not detect Portainer edition (older API version?)
         Defaulting to CE mode. EE features will be attempted but may fail gracefully.
```

---

### Phase 1.5: Portainer Backup

```
 ──────────────── Phase 1.5: Portainer Backup ────────────────────


 +---------------------------------------------------------------------+
 |  SAFETY NET                                                         |
 |                                                                     |
 |  Before migrating, we recommend creating a full Portainer backup.   |
 |  If anything goes wrong, you can restore Portainer to its current   |
 |  state from this backup file.                                       |
 +---------------------------------------------------------------------+

  Create Portainer backup? [Y/n]: y
  Encryption password (empty for none): ********

  [spinner] Creating Portainer backup...
  [checkmark] Backup saved: ./migration_export/portainer_backup/portainer_backup.tar.gz (24.7 MB)


```

**What happens:**
1. Display safety-net explanation panel
2. Ask user to confirm backup
3. Optionally set encryption password
4. Call `POST /api/backup`, stream response to file
5. Verify file size > 0 and valid gzip header
6. Report file path and size

**Error state -- not admin:**
```
  [triangle] Portainer backup requires admin access -- skipping
         You can create a backup manually in Portainer > Settings > Backup
```

**Skip path (`--skip-backup`):**
```
  [info] Portainer backup skipped (--skip-backup flag)
```

---

### Phase 2: Discovery & Audit

```
 ──────────────── Phase 2 of 7: Discovery & Audit ────────────────

  [spinner] Discovering resources...

  Discovering resources  [############################]  100%

 Portainer Resource Discovery (EE detected)
 +---------------------+-------+-----------------------------------------+---------+
 | Resource            | Count | Details                                 | Edition |
 +---------------------+-------+-----------------------------------------+---------+
 | Stacks              |    12 | 8 file-based, 4 git-based               | CE + EE |
 | Standalone Cont.    |     3 | (15 compose-managed excluded)           | CE + EE |
 | Images              |    47 | 12.3 GB total                           | CE + EE |
 | Volumes             |    18 |                                         | CE + EE |
 | Networks            |     6 | (3 default excluded)                    | CE + EE |
 | Registries          |     2 | ghcr.io, docker.io                      | CE + EE |
 | Custom Templates    |     5 | nginx-proxy, postgres, redis...         | CE + EE |
 | Users               |     4 | admin, deploy-bot, dev1, dev2           | CE + EE |
 | Webhooks            |     1 |                                         | EE      |
 | Teams               |     2 | dev-team, ops-team                      | EE      |
 | Roles               |     3 | admin, helpdesk, readonly               | EE      |
 | Edge Stacks         |     0 | (none detected)                         | EE      |
 | Settings            |     1 | Exported for reference                  | CE + EE |
 +---------------------+-------+-----------------------------------------+---------+

 Resources NOT migrated (no Arcane equivalent):
 +-------------------------+-------------------------------------------+---------+
 | Portainer Resource      | Reason                                    | Edition |
 +-------------------------+-------------------------------------------+---------+
 | Endpoint Groups         | Arcane uses flat environment list          | CE + EE |
 | Teams & Memberships     | Exported as reference; Arcane uses roles   | EE      |
 | Granular Roles          | Exported as reference; Arcane: admin/user  | EE      |
 | Resource Access Controls| Exported as reference; different model     | EE      |
 | Edge Groups/Jobs/Stacks | Edge agent architecture differs            | EE      |
 | Activity / Audit Logs   | Exported as reference if desired           | EE      |
 | SSL Certificates        | Portainer UI TLS, not workload related     | CE + EE |
 +-------------------------+-------------------------------------------+---------+

  Migrate ALL discovered resources? [Y/n]: n

  Core resources:
    Include Stacks? [Y/n]: y
    Include Standalone Containers? [Y/n]: y
    Include Volumes? [Y/n]: y
    Include Networks? [Y/n]: y
    Include Registries? [Y/n]: y
    Include Custom Templates? [Y/n]: n
    Include Users? [Y/n]: y

  EE-only resources:                              <-- only shown if EE detected
    Include Webhooks? [Y/n]: y
    Export Teams & Roles as reference? [Y/n]: y
    Export Activity Logs? [Y/n]: n

  [checkmark] Scope confirmed: 8 resource types selected + 1 EE reference export


```

**What happens:**
1. Call all Portainer list endpoints in sequence with a progress bar
2. Classify stacks (file-based vs git-based)
3. Separate standalone containers from compose-managed ones
4. Filter networks (exclude bridge, host, none, ingress)
5. Display summary table
6. Show "not migrated" table for transparency
7. Let user confirm all or cherry-pick resource types

**UX Detail -- why show "NOT migrated":**
Users switching from Portainer need to know what won't carry over so they can
plan manual steps. Showing this proactively builds trust.

---

### Phase 3: Strategy Selection

```
 ──────────────── Phase 3 of 7: Strategy Selection ───────────────


 Migration Strategy
 ──────────────────
  Strategy (export/live) [export]: live

  [info] Live migration: data will be exported AND imported to Arcane


  Enable dry-run (simulate only)? [y/N]: n
  Export/backup directory [./migration_export]: ./migration_export

 +---------------------------------------------------------------------+
 |  MIGRATION PLAN                                                     |
 |                                                                     |
 |  Strategy ......... Live Migration                                  |
 |  Dry Run .......... No                                              |
 |  Backup Dir ....... ./migration_export/                             |
 |  Docker Local ..... Yes (volume backups enabled)                    |
 |                                                                     |
 |  Execution Order:                                                   |
 |    1. Export all Portainer data to disk                              |
 |    2. Create registries in Arcane                                   |
 |    3. Create git repositories in Arcane                             |
 |    4. Create networks in Arcane                                     |
 |    5. Create volumes + upload backups                               |
 |    6. Create projects from stacks (compose + .env)                  |
 |    7. Create GitOps syncs for git-based stacks                      |
 |    8. Recreate standalone containers                                |
 |    9. Create users                                                  |
 |   10. Migrate webhooks                                              |
 +---------------------------------------------------------------------+


```

**What happens:**
1. User chooses export-only vs live
2. If not already set via CLI, ask about dry-run
3. Set backup directory path
4. Display final plan summary panel showing exact execution order

---

### Phase 4: Pre-Flight Checks (Live Migration Only)

```
 ──────────────── Phase 4 of 7: Pre-Flight Checks ───────────────

 Pre-Flight Checks
 +---------------------------+--------+-------------------------------+
 | Check                     | Result | Details                       |
 +---------------------------+--------+-------------------------------+
 | Arcane API health         | PASS   | ok                            |
 | Arcane environment        | PASS   | Status: connected             |
 | Naming conflicts          | WARN   | Conflicting: my-app           |
 | Disk space                | PASS   | 48.2 GB free                  |
 +---------------------------+--------+-------------------------------+

  [triangle] Warning: Project 'my-app' already exists in Arcane.
         It will be skipped during migration unless renamed.

  All checks passed. Proceed with migration? [Y/n]: y


```

**What happens:**
1. Health-check Arcane API
2. Verify target environment is accessible
3. Compare Portainer stack names against existing Arcane projects
4. Check available disk space for export/backups
5. Display results table with PASS/WARN/FAIL status
6. If any FAIL: ask user to continue or abort
7. If WARN only: inform and proceed with confirmation

**Error state -- critical failure:**
```
 | Arcane API health         | FAIL   | Connection refused            |

  [cross] Cannot proceed: Arcane is unreachable.
         Fix the connection and run with --resume.
```

---

### Phase 5: Execution

```
 ──────────────── Phase 5 of 7: Execution ────────────────────────

  [info] Exporting Portainer data to ./migration_export/...

  [checkmark] Exported 2 registries
  [checkmark] Exported 12 stacks (8 file-based, 4 git-based)
  [checkmark] Exported 3 standalone containers
  [checkmark] Exported 6 networks
  [checkmark] Exported 18 volumes
  [info] Backing up volume data (this may take a while)...

  Backing up volumes  [################............]  55%  vol: postgres_data

  [checkmark] Exported 18 volumes (4.2 GB backup data)
  [checkmark] Exported 4 users
  [checkmark] Exported 1 webhook (EE)
  [checkmark] Exported 2 teams, 3 roles, 8 resource controls (EE reference)
  [checkmark] Exported Portainer settings (reference)
  [checkmark] Export manifest saved (edition: EE, 12 resource types)


  [info] Starting live migration to Arcane...

  Migration progress  [##..............................] 12%  5a: Registries

   [checkmark] Registry 'ghcr.io' migrated
   [checkmark] Registry 'docker.io' migrated

  Migration progress  [######..........................] 24%  5b: Git Repos

   [checkmark] Git repo for 'webapp-frontend' created
   [checkmark] Git repo for 'api-backend' created

  Migration progress  [##########......................] 36%  5c: Networks

   [checkmark] Network 'app-network' migrated
   [checkmark] Network 'monitoring' migrated
   [triangle] Network 'legacy-net' skipped: already exists

  Migration progress  [##############..................] 48%  5d: Volumes

   [checkmark] Volume 'postgres_data' created
   [info] Uploading backup for 'postgres_data' (1.2 GB)...
   [checkmark] Volume 'postgres_data' backup restored
   [checkmark] Volume 'redis_data' created + restored
   ...

  Migration progress  [##################............] 60%  5e: Stacks

   [checkmark] Stack 'my-app' -> Arcane project (3 services, 5 env vars)
   [checkmark] Stack 'monitoring' -> Arcane project (2 services)
   [cross] Stack 'legacy-v1' failed: invalid compose syntax
   ...

  Migration progress  [######################........] 72%  5f: GitOps Syncs

   [checkmark] GitOps sync 'webapp-frontend' created (auto-sync: on)
   ...

  Migration progress  [##########################....] 84%  5g: Containers

   [checkmark] Container 'watchtower' recreated + started
   [checkmark] Container 'cloudflared' recreated + started
   [triangle] Container 'temp-debug' skipped: exited state
   ...

  Migration progress  [############################..] 94%  5h-5j

   [checkmark] 3 users migrated (password: ChangeMe123!)
   [triangle] 1 webhook exported (needs manual target mapping)

  Migration progress  [##############################] 100%  Complete


```

**What happens (per sub-phase):**

| Sub-Phase | Source | Target | Edition | Notes |
|-----------|--------|--------|---------|-------|
| 5a | `GET /api/registries` | `POST /container-registries` | CE+EE | Map registry types |
| 5b | Git config from stacks | `POST /customize/git-repositories` | CE+EE | Extract URL, auth, branch |
| 5c | `GET /docker/networks` | `POST /environments/{id}/networks` | CE+EE | Skip defaults |
| 5d | `GET /docker/volumes` | `POST /environments/{id}/volumes` | CE+EE | + backup upload if local |
| 5e | `GET /api/stacks/{id}/file` | `POST /environments/{id}/projects` | CE+EE | compose + envContent |
| 5f | Stack GitConfig | `POST /environments/{id}/gitops-syncs` | CE+EE | Uses repo IDs from 5b |
| 5g | `inspect_container` | `POST /environments/{id}/containers` | CE+EE | Full fidelity |
| 5h | `GET /api/custom_templates` | `POST /templates` | CE+EE | compose + description |
| 5i | `GET /api/users` | `POST /users` | CE+EE | Skip dupes, default pw |
| 5j | `GET /api/webhooks` | `POST .../webhooks` | EE* | *CE skips gracefully if 404 |
| 5k | `GET /api/teams` + roles | Reference JSON | EE | Teams, memberships, roles, ACLs |
| 5l | `GET /api/audit` | Reference JSON | EE | Optional, can be large |

**Checkpoint behavior:**
- State saved to `migration_state.json` after each sub-phase
- Each item tracked individually (migrated/pending/failed)
- On resume: skip completed phases, retry failed items

**Error handling during execution:**
```
   [cross] Stack 'legacy-v1' failed: 422 Unprocessable Entity
         Response: "invalid compose file: unknown service field 'extends'"

  Continue with remaining stacks? [Y/n]: y
```

**Dry-run output:**
```
   [DRY RUN] Would create registry 'ghcr.io' (custom, ghcr.io)
   [DRY RUN] Would create registry 'docker.io' (dockerhub, hub.docker.com)
   [DRY RUN] Would create network 'app-network' (bridge, 172.18.0.0/16)
   ...
```

---

### Phase 6: Verification & Report

```
 ──────────────── Phase 6 of 7: Verification ─────────────────────


 Migration Complete
 ══════════════════

 Migration Summary (Portainer EE -> Arcane v1.17.0)
 +---------------------+----------+--------+---------+---------+
 | Resource            | Migrated | Failed | Skipped | Edition |
 +---------------------+----------+--------+---------+---------+
 | Portainer Backup    |        1 |      0 |       0 | CE + EE |
 | Registries          |        2 |      0 |       0 | CE + EE |
 | Git Repositories    |        4 |      0 |       0 | CE + EE |
 | Networks            |        5 |      0 |       1 | CE + EE |
 | Volumes             |       18 |      0 |       0 | CE + EE |
 | Stacks (Projects)   |        7 |      1 |       0 | CE + EE |
 | GitOps Syncs        |        4 |      0 |       0 | CE + EE |
 | Containers          |        2 |      0 |       1 | CE + EE |
 | Templates           |        0 |      0 |       5 | CE + EE |
 | Users               |        3 |      0 |       1 | CE + EE |
 | Webhooks            |        1 |      0 |       0 | EE      |
 +---------------------+----------+--------+---------+---------+
 | TOTAL               |       47 |      1 |       8 |         |
 +---------------------+----------+--------+---------+---------+

 EE Reference Exports (saved for manual Arcane setup):
 +---------------------+-------+----------------------------------------------+
 | Resource            | Count | File                                         |
 +---------------------+-------+----------------------------------------------+
 | Teams               |     2 | ee_reference/teams.json                      |
 | Team Memberships    |     6 | ee_reference/team_memberships.json           |
 | Roles               |     3 | ee_reference/roles.json                      |
 | Resource Controls   |     8 | ee_reference/resource_controls.json          |
 | Edge Stacks         |     0 | (none detected)                              |
 | Activity Logs       |    -- | (skipped by user)                            |
 +---------------------+-------+----------------------------------------------+

 Errors:
   [cross] [Stacks] legacy-v1: 422 invalid compose file

 Action Items:
   [triangle] 3 users created with default password 'ChangeMe123!' -- reset ASAP
   [triangle] 1 webhook needs manual target mapping in Arcane
   [triangle] 5 custom templates not migrated (deselected by user)
   [triangle] Review ee_reference/ to manually configure Arcane RBAC
          Portainer had 2 teams and 3 custom roles -- Arcane uses admin/user roles
   [triangle] Portainer settings exported to settings/portainer_settings.json
          Review for LDAP/OAuth config to replicate in Arcane

 Files:
   Full report .... ./migration_export/migration_report.json
   Rollback script  ./migration_export/rollback.sh
   Log file ....... migration_20260411_143022.log
   Portainer backup ./migration_export/portainer_backup/portainer_backup.tar.gz
   EE reference ... ./migration_export/ee_reference/

 [checkmark] Migration completed successfully!


```

**What happens:**
1. Count migrated/failed/skipped per resource type
2. Display summary table
3. List any errors with details
4. Show action items the user needs to handle manually
5. Save JSON report, rollback script, reference paths
6. Clean up checkpoint file if fully successful

**Rollback script** (`rollback.sh`):
```bash
#!/usr/bin/env bash
# Rollback -- undo Arcane resources created during migration
ARCANE_URL="https://arcane.myhost.com:3552/api"
API_KEY="YOUR_API_KEY_HERE"

# Delete project 'my-app'
curl -X DELETE "$ARCANE_URL/environments/0/projects/abc123/destroy" -H "X-API-Key: $API_KEY"

# Delete network 'app-network'
curl -X DELETE "$ARCANE_URL/environments/0/networks/def456" -H "X-API-Key: $API_KEY"

# ...one entry per created resource, in reverse order
```

---

## 5. API Mapping & Data Transforms

### 5.1 Full API Mapping

**CE + EE (core -- always available):**

| # | Portainer Source | Portainer API | Arcane Target | Arcane API |
|---|---|---|---|---|
| 0 | Portainer Backup | `POST /api/backup` | Local file | `portainer_backup.tar.gz` |
| 1 | Edition detect | `GET /api/status` | Config flag | `config.portainer_edition` |
| 2 | Container Registries | `GET /api/registries` | Container Registries | `POST /container-registries` |
| 3 | Git repo configs | `GET /api/stacks` (git type) | Git Repositories | `POST /customize/git-repositories` |
| 4 | Networks | `GET /endpoints/{id}/docker/networks` | Networks | `POST /environments/{id}/networks` |
| 5 | Volumes | `GET /endpoints/{id}/docker/volumes` | Volumes | `POST /environments/{id}/volumes` |
| 6 | Volume data | Docker CLI `tar czf` | Volume restore | `POST .../volumes/{name}/backups/upload` |
| 7 | Compose stacks | `GET /api/stacks/{id}/file` + env | Projects | `POST /environments/{id}/projects` |
| 8 | Git stacks | Stack GitConfig | GitOps Syncs | `POST /environments/{id}/gitops-syncs` |
| 9 | Containers | `GET .../docker/containers/{id}/json` | Containers | `POST /environments/{id}/containers` |
| 10 | Custom Templates | `GET /api/custom_templates` + `/file` | Templates | `POST /templates` |
| 11 | Users | `GET /api/users` | Users | `POST /users` |
| 12 | Settings | `GET /api/settings` | Reference export | `settings/portainer_settings.json` |

**EE-only (attempted only when `config.portainer_edition == "EE"`):**

| # | Portainer Source | Portainer API | Target | Notes |
|---|---|---|---|---|
| 13 | Webhooks | `GET /api/webhooks` | Arcane webhooks | CE < 2.19 returns 404 -- graceful skip |
| 14 | Teams | `GET /api/teams` | Reference export | `teams/teams.json` |
| 15 | Team Memberships | `GET /api/team_memberships` | Reference export | `teams/memberships.json` |
| 16 | Roles | `GET /api/roles` | Reference export | `roles/roles.json` |
| 17 | Resource Controls | `GET /api/resource_controls` | Reference export | `rbac/resource_controls.json` |
| 18 | Edge Stacks | `GET /api/edge_stacks` | Detect + warn | No Arcane equiv; warn if count > 0 |
| 19 | Activity Logs | `GET /api/audit` | Reference export | `audit/activity_logs.json` (optional) |

### 5.2 Registry Type Mapping

```python
REGISTRY_TYPE_MAP = {
    1: "custom",     # Quay
    2: "custom",     # Azure
    3: "custom",     # Custom
    4: "custom",     # GitLab
    5: "custom",     # ProGet
    6: "dockerhub",  # DockerHub
    7: "ecr",        # ECR
    8: "custom",     # GitHub (ghcr.io)
}
```

### 5.3 Stack to Project Transform

```python
# Portainer stack.Env = [{"name": "DB_HOST", "value": "postgres"}, ...]
# Arcane project.envContent = "DB_HOST=postgres\nDB_PORT=5432"

def _transform_stack_to_project(stack, compose_content):
    env_vars = stack.get("Env", [])
    return {
        "name": stack["Name"],
        "composeContent": compose_content,
        "envContent": "\n".join(f"{e['name']}={e['value']}" for e in env_vars),
    }
```

### 5.4 Container Transform (Full Fidelity)

Every field that can cause silent breakage if omitted:

```python
def _transform_container(inspect_data):
    config = inspect_data["Config"]
    host_config = inspect_data["HostConfig"]

    # Volume mounts with mode preservation (ro/rw)
    volumes = []
    for m in inspect_data.get("Mounts", []):
        src = m.get("Source", m.get("Name", ""))
        dst = m.get("Destination", "")
        mode = m.get("Mode", "")
        entry = f"{src}:{dst}" + (f":{mode}" if mode else "")
        volumes.append(entry)

    return {
        "name": inspect_data["Name"].lstrip("/"),
        "image": config["Image"],
        "env": config.get("Env", []),
        "cmd": config.get("Cmd"),
        "entrypoint": config.get("Entrypoint"),
        "labels": config.get("Labels", {}),
        "hostname": config.get("Hostname"),
        "domainname": config.get("Domainname", ""),
        "user": config.get("User"),
        "workingDir": config.get("WorkingDir"),
        "tty": config.get("Tty", False),
        "openStdin": config.get("OpenStdin", False),
        "volumes": volumes,
        "restartPolicy": host_config.get("RestartPolicy", {}).get("Name", "no"),
        "privileged": host_config.get("Privileged", False),
        "healthcheck": config.get("Healthcheck"),
        "dns": host_config.get("Dns") or [],
        "dnsSearch": host_config.get("DnsSearch") or [],
        "dnsOptions": host_config.get("DnsOptions") or [],
        "hostConfig": {
            "networkMode": host_config.get("NetworkMode"),
            "portBindings": host_config.get("PortBindings", {}),
            "binds": host_config.get("Binds", []),
            # Resources
            "memory": host_config.get("Memory", 0),
            "memorySwap": host_config.get("MemorySwap", 0),
            "nanoCpus": host_config.get("NanoCpus", 0),
            "cpuShares": host_config.get("CpuShares", 0),
            # Security
            "privileged": host_config.get("Privileged", False),
            "capAdd": host_config.get("CapAdd") or [],
            "capDrop": host_config.get("CapDrop") or [],
            "securityOpt": host_config.get("SecurityOpt") or [],
            "readonlyRootfs": host_config.get("ReadonlyRootfs", False),
            # Devices
            "devices": host_config.get("Devices") or [],
            # Limits
            "pidsLimit": host_config.get("PidsLimit", 0),
            "autoRemove": host_config.get("AutoRemove", False),
        },
    }
```

| Field | Risk if Missing |
|-------|-----------------|
| `capAdd/capDrop` | Containers needing NET_ADMIN etc. fail silently |
| `securityOpt` | AppArmor/SELinux profiles lost |
| `healthcheck` | Health monitoring breaks |
| `dns/dnsSearch` | Custom DNS resolution fails |
| `devices` | GPU/serial device access lost |
| `memorySwap` | OOM behavior changes |
| `pidsLimit` | Fork bomb protection lost |
| `readonlyRootfs` | Security posture weakened |
| Mount `mode` | Read-only mounts become read-write |

---

## 6. Internal Architecture

### 6.1 Class Diagram

```
migrate.py (~2800 lines)
|
+-- Config                   Dataclass: URLs, keys, flags, paths, edition, state
+-- PortainerClient          Read-only Portainer API (GET + backup POST + EE endpoints)
+-- ArcaneClient             Read+write Arcane API (all CRUD)
+-- DockerLocal              Docker socket: volume backup, size estimation
+-- WizardUI                 Rich TUI: banners, tables, prompts, progress, edition panels
+-- ReportGenerator          JSON report + rollback script + console summary
+-- MigrationEngine          Orchestrator: phases, checkpoints, transforms, CE/EE branching
|
+-- main()                   Argparse, config init, engine.run()
```

### 6.2 Checkpoint/Resume State

```json
{
    "version": "1.0",
    "config_hash": "a1b2c3d4",
    "portainer_edition": "EE",
    "started_at": "2026-04-11T14:30:00Z",
    "phases": {
        "portainer_backup": { "status": "completed", "migrated": ["backup"] },
        "registries":       { "status": "completed", "migrated": ["1", "2"] },
        "git_repos":        { "status": "completed", "migrated": ["3", "4"] },
        "networks":         { "status": "in_progress", "migrated": ["net-1"], "errors": [] },
        "volumes":          { "status": "pending" },
        "stacks":           { "status": "pending" },
        "gitops_syncs":     { "status": "pending" },
        "containers":       { "status": "pending" },
        "templates":        { "status": "pending" },
        "users":            { "status": "pending" },
        "webhooks":         { "status": "pending" },
        "ee_rbac_export":   { "status": "pending" },
        "ee_audit_export":  { "status": "pending" }
    }
}
```

Phases `ee_rbac_export` and `ee_audit_export` are auto-skipped when edition is CE.

Resume logic: skip `completed`, retry `in_progress` from last good item, run `pending`.

### 6.3 Export Directory Structure

```
migration_export/
+-- manifest.json                        # Inventory with edition, counts, timestamps
+-- portainer_backup/
|   +-- portainer_backup.tar.gz
+-- registries/
|   +-- registries.json
+-- stacks/
|   +-- my-app/
|   |   +-- docker-compose.yml
|   |   +-- .env
|   +-- monitoring/
|       +-- docker-compose.yml
|       +-- .env
+-- containers/
|   +-- standalone.json
+-- networks/
|   +-- networks.json
+-- volumes/
|   +-- volumes.json
|   +-- backups/
|       +-- postgres_data.tar.gz
|       +-- redis_data.tar.gz
+-- templates/
|   +-- custom_templates.json
+-- users/
|   +-- users.json
+-- webhooks/
|   +-- webhooks.json                    # EE: full webhook data. CE: may be empty.
+-- settings/
|   +-- portainer_settings.json
+-- notifications/
|   +-- settings.json
+-- ee_reference/                        # EE-only (directory omitted entirely for CE)
    +-- teams.json                       # Team names, IDs, leader IDs
    +-- team_memberships.json            # User-to-team assignments
    +-- roles.json                       # Custom role definitions
    +-- resource_controls.json           # Per-resource ACL rules
    +-- edge_stacks.json                 # Edge stack configs (if any detected)
    +-- activity_logs.json               # Audit trail (optional, can be large)
```

### 6.4 Dry-Run Wrapper

Every write operation passes through:

```python
def _execute_or_log(self, action, api_call, *args, **kwargs):
    if self.config.dry_run:
        self.ui.dry_run_msg(action)
        return {"dry_run": True, "action": action}
    return api_call(*args, **kwargs)
```

### 6.5 Logging

| Output | Level | Format | Purpose |
|--------|-------|--------|---------|
| Console | INFO+ | Rich formatted, colors, spinners | User-facing |
| File | DEBUG | `YYYY-MM-DD HH:MM:SS [LEVEL] message` | Debugging, API responses |

### 6.6 Platform Compatibility

| Concern | Solution |
|---------|----------|
| Paths | `pathlib.Path` everywhere |
| Docker socket | `/var/run/docker.sock` (Unix) / `//./pipe/docker_engine` (Win) |
| Volume backup | Docker CLI `docker run alpine tar czf` |
| Terminal colors | `rich` auto-detects; degrades gracefully |
| Encoding | UTF-8 enforced on all file I/O |

---

## 7. Implementation Tasks

### Task 1: Scaffold -- Config, CLI, dependency check

**Files:** Create `migrate.py`, `requirements.txt`

**Delivers:**
- Shebang, docstring, `__version__ = "1.0.0"`
- `check_dependencies()` -- tries `import rich, requests`, offers pip install
- `Config` dataclass with all fields including:
  - `portainer_edition: str = ""` -- auto-detected: `"CE"` or `"EE"`
  - `portainer_version: str = ""` -- e.g. `"2.39.1"`
- `parse_args()` with argparse (all CLI flags)
- `setup_logging()` -- file + rich console handlers
- `main()` entry point: parse args, init config, detect platform, show banner
- `requirements.txt` with `rich>=13.0.0` and `requests>=2.28.0`

**Verify:** `python migrate.py --help` shows all options. `python migrate.py` shows banner.

**Commit:** `feat: scaffold migrate.py with Config, CLI args, dependency check`

---

### Task 2: PortainerClient -- all API methods (CE + EE)

**Files:** Modify `migrate.py`

**Delivers:**
- `PortainerClient` class with `requests.Session`, API key header, SSL config
- `_get()` helper with timeout, error handling, debug logging
- `_safe_get()` helper -- same as `_get()` but returns `[]` or `{}` on 404/403 (for EE-only endpoints called from CE)
- **CE + EE methods:** `test_connection` (returns edition + version), `list_endpoints`, `get_endpoint`, `list_stacks`, `get_stack`, `get_stack_file`, `list_registries`, `list_users`, `get_settings`, `list_custom_templates`, `get_custom_template_file`, `trigger_backup`
- **EE-only methods** (use `_safe_get`, graceful on CE):
  - `list_webhooks()` -- returns `[]` if 404
  - `list_teams()` -- `GET /api/teams`
  - `list_team_memberships()` -- `GET /api/team_memberships`
  - `list_roles()` -- `GET /api/roles`
  - `list_resource_controls()` -- `GET /api/resource_controls`
  - `list_edge_stacks()` -- `GET /api/edge_stacks`
- Docker proxy methods: `list_containers`, `inspect_container`, `list_images`, `list_volumes`, `list_networks`, `get_docker_info`, `get_system_df`
- `detect_edition()` -- parses `test_connection()` response, sets `config.portainer_edition` and `config.portainer_version`

**Verify:** `python -c "from migrate import PortainerClient; print('OK')"`

**Commit:** `feat: add PortainerClient with CE+EE API methods including backup`

---

### Task 3: ArcaneClient -- all read/write API methods

**Files:** Modify `migrate.py`

**Delivers:**
- `ArcaneClient` class with dual auth (API key or Bearer token)
- `_request()` helper with method dispatch, JSON + file upload support
- Auth: `login`
- Health: `health_check`, `get_version`
- CRUD for: environments, registries, git repos, projects, gitops syncs, containers, networks, volumes (+ backup upload), users, webhooks, notifications, settings, templates

**Verify:** `python -c "from migrate import ArcaneClient; print('OK')"`

**Commit:** `feat: add ArcaneClient with all read/write API methods`

---

### Task 4: DockerLocal -- volume backup via Docker socket

**Files:** Modify `migrate.py`

**Delivers:**
- `DockerLocal` class
- `is_available()` -- checks Docker CLI reachability
- `backup_volume(name, dir)` -- `docker run alpine tar czf` with timeout
- `get_volume_size(name)` -- `docker run alpine du -sb`
- `list_compose_containers(project)` -- filter by compose label

**Verify:** `python -c "from migrate import DockerLocal; print('OK')"`

**Commit:** `feat: add DockerLocal for volume backup via Docker socket`

---

### Task 5: WizardUI -- rich TUI components (edition-aware)

**Files:** Modify `migrate.py`

**Delivers:**
- `WizardUI` class -- all user-facing terminal output
- `banner()` -- ASCII-art title + platform info table
- `phase_header(num, total, title)` -- `Phase N of M: Title` rule
- Status methods: `success()`, `warning()`, `error()`, `info()`, `dry_run_msg()`
- `ask_portainer_connection()` -- URL, API key, SSL prompts with retry on failure
- `show_edition_panel(edition, version)` -- displays CE or EE panel with available/limited features (see Phase 1 wireframe). Uses green checkmarks for available, yellow triangles for limited.
- `ask_arcane_connection()` -- URL, auth method choice, credentials
- `select_endpoint(endpoints)` -- numbered table, auto-select if only one
- `select_arcane_environment(envs)` -- same pattern
- `show_discovery_summary(discovery, edition)` -- resource table with Edition column. EE-only rows only shown when edition is EE. Includes "not migrated" table adjusted per edition.
- `ask_strategy()` -- export/live choice, dry-run, backup dir
- `ask_scope_confirmation(discovery, edition)` -- grouped into "Core resources" (always) and "EE-only resources" (only when EE). See Phase 2 wireframe.
- `show_preflight_results(results)` -- PASS/WARN/FAIL table
- `show_migration_plan(config)` -- confirmation panel with execution order
- `create_progress()` -- returns configured Progress context manager
- `show_final_report(report, edition)` -- summary table with Edition column + EE reference exports table (only when EE) + action items including RBAC review note

**Verify:** `python -c "from migrate import WizardUI; print('OK')"`

**Commit:** `feat: add WizardUI with edition-aware rich TUI components`

---

### Task 6: ReportGenerator -- reports and rollback

**Files:** Modify `migrate.py`

**Delivers:**
- `ReportGenerator` class
- `record_success(type, name, source_id, target_id)`
- `record_failure(type, name, error)`
- `record_skip(type, name, reason)`
- `add_rollback(method, url, description)`
- `save_report()` -- JSON to `migration_export/migration_report.json`
- `save_rollback_script()` -- shell script with curl DELETE commands

**Verify:** `python -c "from migrate import ReportGenerator; print('OK')"`

**Commit:** `feat: add ReportGenerator for JSON reports and rollback scripts`

---

### Task 7: MigrationEngine -- checkpoint + state management

**Files:** Modify `migrate.py`

**Delivers:**
- `MigrationEngine` class with PHASES list
- `_load_state()` / `_save_state()` -- JSON checkpoint file
- `_phase_status()` / `_mark_phase()` / `_is_migrated()` / `_record_migrated()`
- `_should_include()` -- checks user scope selection
- `_execute_or_log()` -- dry-run wrapper

**Commit:** `feat: add MigrationEngine with checkpoint/resume state management`

---

### Task 8: MigrationEngine -- discovery phase (edition-aware)

**Files:** Modify `migrate.py`

**Delivers:**
- `discover()` method -- calls all Portainer list endpoints
- Progress bar while scanning (more items when EE)
- Classifies stacks (file vs git), containers (standalone vs compose), networks (user vs default)
- Counts images, sizes, templates, settings
- **EE-only discovery** (when `config.portainer_edition == "EE"`):
  - `list_webhooks()` -- attempted, graceful skip on 404
  - `list_teams()` -- team count and names
  - `list_roles()` -- role count
  - `list_resource_controls()` -- ACL count
  - `list_edge_stacks()` -- detect and warn if count > 0
- **CE behavior:** EE endpoints skipped entirely (not attempted), progress bar has fewer items
- Returns discovery dict with `edition` key consumed by UI and execution phases

**Commit:** `feat: add edition-aware discovery phase`

---

### Task 9: MigrationEngine -- data transformation helpers

**Files:** Modify `migrate.py`

**Delivers:**
- `_transform_registry()` -- registry type map, ECR special handling
- `_transform_stack_to_project()` -- compose content + env array to envContent string
- `_transform_git_stack_to_gitops()` -- branch, compose path, auto-sync config
- `_transform_git_config_to_repo()` -- URL, auth type, credentials
- `_transform_network()` -- driver, IPAM, labels, options
- `_transform_container()` -- full fidelity (caps, health, DNS, devices, limits, mount modes)
- `_transform_custom_template()` -- title, description, compose content
- `_transform_user()` -- username, role mapping, default password

**Commit:** `feat: add data transformation helpers for all resource types`

---

### Task 10: MigrationEngine -- export to disk (edition-aware)

**Files:** Modify `migrate.py`

**Delivers:**
- `_export_to_disk()` -- saves all discovery data to export directory
- **CE + EE exports:** registries, stacks (compose + .env per stack), containers, networks, volumes (metadata + tar.gz backups if local), templates (with compose content), users, settings (masked)
- **EE-only exports** (when edition is EE and user opted in):
  - `ee_reference/teams.json` -- team names, IDs, leader IDs
  - `ee_reference/team_memberships.json` -- user-to-team assignments
  - `ee_reference/roles.json` -- custom role definitions with permissions
  - `ee_reference/resource_controls.json` -- per-resource ACL rules
  - `ee_reference/edge_stacks.json` -- edge stack configs (if any)
  - `ee_reference/activity_logs.json` -- audit trail (optional, warned about size)
  - `webhooks/webhooks.json` -- full webhook data
- **CE behavior:** `ee_reference/` directory not created. Webhooks attempted but empty file if 404.
- Manifest file with edition, counts, and timestamp
- Volume backup with progress bar

**Commit:** `feat: add edition-aware export-to-disk for all resource types`

---

### Task 11: MigrationEngine -- live migration phases (edition-aware)

**Files:** Modify `migrate.py`

**Delivers:**

**CE + EE phases (always run):**
- `_portainer_backup()` -- POST /api/backup, save to file, verify
- `_migrate_registries()` -- transform + create, with checkpoint
- `_migrate_git_repos()` -- extract from git stacks, create repos, build ID map
- `_migrate_networks()` -- create user networks, skip duplicates
- `_migrate_volumes()` -- create + upload backup if available
- `_migrate_stacks()` -- file-based stacks to Arcane projects
- `_migrate_gitops_syncs()` -- git stacks to syncs using repo ID map
- `_migrate_containers()` -- full inspect + transform + create + start if was running
- `_migrate_templates()` -- custom templates to Arcane templates
- `_migrate_users()` -- skip duplicates, default password

**EE-only phases (skipped automatically on CE):**
- `_migrate_webhooks()` -- EE: full webhook migration. CE: attempt, skip gracefully on 404.
- `_export_ee_rbac()` -- saves teams, memberships, roles, resource controls to `ee_reference/`. Not imported to Arcane (no equivalent) -- reference for manual RBAC setup.
- `_export_ee_audit()` -- optional: saves activity logs. Warns about potential size. Skipped if user declined in scope selection.

**Edition branching pattern:**
```python
if self.config.portainer_edition == "EE":
    self._migrate_webhooks()
    if self._should_include("Teams"):
        self._export_ee_rbac()
    if self._should_include("Activity Logs"):
        self._export_ee_audit()
else:
    # CE: attempt webhooks but expect possible 404
    self._migrate_webhooks()  # _safe_get handles 404
```

Each phase: checkpoint per item, error recovery (skip + continue), rollback recording.

**Commit:** `feat: add edition-aware live migration phases`

---

### Task 12: MigrationEngine -- run() orchestrator (edition-aware)

**Files:** Modify `migrate.py`

**Delivers:**
- `run(args)` -- main orchestrator method
- Phase 0: banner + prereqs
- Phase 1: connection setup with retry loops
  - After Portainer connection: call `detect_edition()`
  - Display edition panel (CE or EE) via `ui.show_edition_panel()`
  - Store edition in config for all downstream decisions
- Phase 1.5: Portainer backup (unless --skip-backup)
- Phase 2: discovery + scope confirmation
  - Discovery calls EE endpoints only when edition is EE
  - Scope prompt groups "Core" vs "EE-only" resources
- Phase 3: strategy + options
- Phase 4: pre-flight checks (live only)
- Phase 5: export first, then live migration with overall progress bar
  - Phase list dynamically built based on edition:
    ```python
    phases = [core_phases]  # always present
    if config.portainer_edition == "EE":
        phases += [ee_phases]  # webhooks, rbac export, audit export
    ```
- Phase 6: report generation + final summary
  - EE: includes EE reference exports table + RBAC review action item
  - CE: clean summary without EE sections
- Resume detection on startup (checkpoint stores edition)
- KeyboardInterrupt handling (save checkpoint, clean exit)

**Commit:** `feat: add edition-aware run() orchestrator with full wizard flow`

---

### Task 13: Wire up main() entry point

**Files:** Modify `migrate.py`

**Delivers:**
- Update `if __name__ == "__main__"` block
- Load config from JSON file if `--config`
- Pass CLI flags to config (dry-run, export-only, skip-backup)
- Instantiate engine and call `engine.run(args)`
- Exception handling: KeyboardInterrupt -> save + exit, unhandled -> log + exit

**Verify:**
- `python migrate.py --help` -- shows all options
- `python migrate.py --version` -- shows `1.0.0`
- `python migrate.py --dry-run` -- starts wizard, shows banner

**Commit:** `feat: wire up main() entry point with config loading`

---

### Task 14: README

**Files:** Create `README.md`

**Delivers:**
- Project description
- Migration resource table (what gets migrated)
- Quick start (3 lines)
- Requirements
- Usage examples (all CLI modes)
- Mode descriptions (export, live, dry-run)
- Feature list
- Config file format example
- License

**Commit:** `docs: add README with usage and migration reference`

---

### Task 15: Integration verification

**Verify:**
- `python migrate.py --version` -- outputs version
- `python migrate.py --help` -- shows complete help
- `python -c "import migrate; print('All classes:', all(hasattr(migrate, c) for c in ['Config', 'PortainerClient', 'ArcaneClient', 'DockerLocal', 'WizardUI', 'ReportGenerator', 'MigrationEngine']))"` -- `True`

**Commit:** `chore: final integration verification`
