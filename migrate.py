#!/usr/bin/env python3
"""
Portainer-to-Arcane Migration Tool

Migrates Docker resources (stacks, containers, volumes, networks, registries,
users, settings) from Portainer CE/EE to Arcane.

Usage:
    python migrate.py [options]

See --help for full option list.
"""

__version__ = "0.2.0"

# ---------------------------------------------------------------------------
# Dependency bootstrap -- runs before any third-party imports
# ---------------------------------------------------------------------------

import importlib
import subprocess
import sys
import os


def check_dependencies():
    """Verify that required third-party packages are installed.

    If any are missing, offer to install them via pip and restart the process.
    """
    missing = []
    for pkg in ("rich", "requests"):
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return

    print(f"Missing required packages: {', '.join(missing)}")
    answer = input("Install them now with pip? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing
        )
        print("Dependencies installed. Restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        print("Cannot continue without required packages. Exiting.")
        sys.exit(1)


check_dependencies()

# ---------------------------------------------------------------------------
# Third-party imports (guaranteed present after check_dependencies)
# ---------------------------------------------------------------------------

import requests as http_requests  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.progress import (  # noqa: E402
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
)
from rich.prompt import Prompt, Confirm, IntPrompt  # noqa: E402
from rich.logging import RichHandler  # noqa: E402
from rich.text import Text  # noqa: E402
from rich.tree import Tree  # noqa: E402
from rich import box  # noqa: E402

# ---------------------------------------------------------------------------
# Standard-library imports
# ---------------------------------------------------------------------------

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import platform  # noqa: E402
import shutil  # noqa: E402
from dataclasses import dataclass, field, asdict  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional, List, Dict, Any, Callable  # noqa: E402

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

console = Console()

DISPLAY_NAMES = {
    "stacks": "Stacks",
    "standalone_containers": "Standalone Containers",
    "images": "Images",
    "volumes": "Volumes",
    "networks": "Networks",
    "registries": "Registries",
    "custom_templates": "Custom Templates",
    "users": "Users",
    "webhooks": "Webhooks",
    "teams": "Teams",
    "roles": "Roles",
    "edge_stacks": "Edge Stacks",
    "settings": "Settings",
    "team_memberships": "Team Memberships",
    "resource_controls": "Resource Controls",
    "activity_logs": "Activity Logs",
}

# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """Central configuration for the migration run."""

    # -- Portainer ---------------------------------------------------------
    portainer_url: str = ""
    portainer_api_key: str = ""
    portainer_endpoint_id: int = 1
    portainer_ssl_verify: bool = True
    portainer_edition: str = ""
    portainer_version: str = ""

    # -- Arcane ------------------------------------------------------------
    arcane_url: str = ""
    arcane_api_key: str = ""
    arcane_username: str = ""
    arcane_password: str = ""
    arcane_token: str = ""
    arcane_refresh_token: str = ""
    arcane_environment_id: str = "0"
    arcane_ssl_verify: bool = True

    # -- Options -----------------------------------------------------------
    strategy: str = "export"
    dry_run: bool = False
    backup_dir: str = "./migration_export"
    log_file: str = ""

    # -- Runtime -----------------------------------------------------------
    docker_socket: str = ""
    has_docker: bool = False
    platform_name: str = ""

    # -- State -------------------------------------------------------------
    checkpoint_file: str = "./migration_state.json"
    selected_items: Dict[str, List[str]] = field(default_factory=dict)

    # -- Methods -----------------------------------------------------------

    def detect_platform(self):
        """Detect OS, set the Docker socket path, and check docker availability."""
        self.platform_name = platform.system().lower()
        if self.platform_name == "windows":
            self.docker_socket = "//./pipe/docker_engine"
        else:
            self.docker_socket = "/var/run/docker.sock"
        self.has_docker = shutil.which("docker") is not None

    def to_dict(self) -> Dict[str, Any]:
        """Return a dict representation with secrets masked."""
        d = asdict(self)
        for key in (
            "portainer_api_key",
            "arcane_api_key",
            "arcane_password",
            "arcane_token",
            "arcane_refresh_token",
        ):
            if d.get(key):
                d[key] = d[key][:4] + "****"
        return d

    def config_hash(self) -> str:
        """SHA-256 fingerprint of the key connection identifiers."""
        payload = (
            f"{self.portainer_url}|{self.portainer_endpoint_id}"
            f"|{self.arcane_url}|{self.arcane_environment_id}"
        )
        return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# PortainerClient
# ---------------------------------------------------------------------------


class PortainerClient:
    """Read-only wrapper around the Portainer CE / EE API."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.session = http_requests.Session()
        self.session.headers.update({"X-API-Key": config.portainer_api_key})
        self.session.verify = config.portainer_ssl_verify
        self.base_url = config.portainer_url.rstrip("/")

    # -- low-level helpers -------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET *path* (relative to base_url) and return parsed JSON."""
        url = f"{self.base_url}{path}"
        self.logger.debug("GET %s params=%s", url, params)
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _safe_get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Like _get but returns [] on 404/403 (EE-only endpoints on CE)."""
        try:
            return self._get(path, params)
        except http_requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (403, 404):
                self.logger.debug(
                    "Endpoint %s returned %s -- treating as empty",
                    path,
                    exc.response.status_code,
                )
                return []
            raise

    def _docker(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Proxied Docker API call via Portainer endpoint."""
        eid = self.config.portainer_endpoint_id
        return self._get(f"/api/endpoints/{eid}/docker{path}", params)

    def _safe_docker(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Proxied Docker API call that returns [] on 404/403."""
        eid = self.config.portainer_endpoint_id
        return self._safe_get(f"/api/endpoints/{eid}/docker{path}", params)

    # -- CE + EE methods ---------------------------------------------------

    def test_connection(self) -> Dict[str, Any]:
        """GET /api/status -- returns version + edition info."""
        return self._get("/api/status")

    def detect_edition(self) -> Dict[str, Any]:
        """Call test_connection and populate config edition/version fields."""
        status = self.test_connection()
        self.config.portainer_version = status.get("Version", "")
        # Edition field present in EE; absent or "CE" in Community
        edition = status.get("Edition", "CE")
        if isinstance(edition, int):
            edition = "EE" if edition >= 2 else "CE"
        self.config.portainer_edition = edition
        self.logger.info(
            "Portainer %s %s detected",
            self.config.portainer_edition,
            self.config.portainer_version,
        )
        return status

    def list_endpoints(self) -> List[Dict[str, Any]]:
        return self._get("/api/endpoints")

    def get_endpoint(self, endpoint_id: int) -> Dict[str, Any]:
        return self._get(f"/api/endpoints/{endpoint_id}")

    def list_stacks(self) -> List[Dict[str, Any]]:
        return self._get("/api/stacks")

    def get_stack(self, stack_id: int) -> Dict[str, Any]:
        return self._get(f"/api/stacks/{stack_id}")

    def get_stack_file(self, stack_id: int) -> Dict[str, Any]:
        return self._get(f"/api/stacks/{stack_id}/file")

    def list_registries(self) -> List[Dict[str, Any]]:
        return self._get("/api/registries")

    def list_users(self) -> List[Dict[str, Any]]:
        return self._get("/api/users")

    def get_settings(self) -> Dict[str, Any]:
        return self._get("/api/settings")

    def list_custom_templates(self) -> Any:
        return self._safe_get("/api/custom_templates")

    def get_custom_template_file(self, template_id: int) -> Dict[str, Any]:
        return self._get(f"/api/custom_templates/{template_id}/file")

    def trigger_backup(self, password: str = "") -> bytes:
        """POST /api/backup -- returns raw tar.gz bytes."""
        url = f"{self.base_url}/api/backup"
        self.logger.debug("POST %s (backup)", url)
        resp = self.session.post(url, json={"password": password}, timeout=120)
        resp.raise_for_status()
        return resp.content

    # -- EE-only methods ---------------------------------------------------

    def list_webhooks(self) -> Any:
        return self._safe_get("/api/webhooks")

    def list_teams(self) -> Any:
        return self._safe_get("/api/teams")

    def list_team_memberships(self) -> Any:
        return self._safe_get("/api/team_memberships")

    def list_roles(self) -> Any:
        return self._safe_get("/api/roles")

    def list_resource_controls(self) -> Any:
        return self._safe_get("/api/resource_controls")

    def list_edge_stacks(self) -> Any:
        return self._safe_get("/api/edge_stacks")

    # -- Docker proxy methods ----------------------------------------------

    def list_containers(self, all: bool = True) -> List[Dict[str, Any]]:
        params = {"all": "true"} if all else {}
        return self._docker("/containers/json", params)

    def inspect_container(self, container_id: str) -> Dict[str, Any]:
        return self._docker(f"/containers/{container_id}/json")

    def list_images(self) -> List[Dict[str, Any]]:
        return self._docker("/images/json")

    def list_volumes(self) -> Dict[str, Any]:
        return self._docker("/volumes")

    def list_networks(self) -> List[Dict[str, Any]]:
        return self._docker("/networks")

    def get_docker_info(self) -> Dict[str, Any]:
        return self._docker("/info")

    def get_system_df(self) -> Any:
        return self._safe_docker("/system/df")


# ---------------------------------------------------------------------------
# ArcaneClient
# ---------------------------------------------------------------------------


class ArcaneClient:
    """Read+write wrapper around the Arcane API."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.session = http_requests.Session()
        self.session.verify = config.arcane_ssl_verify
        self.base_url = config.arcane_url.rstrip("/") + "/api"

    # -- auth helpers ------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        """Return the appropriate authorization header."""
        if self.config.arcane_api_key:
            return {"X-API-Key": self.config.arcane_api_key}
        if self.config.arcane_token:
            return {"Authorization": f"Bearer {self.config.arcane_token}"}
        return {}

    # -- low-level helpers -------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        files: Optional[Any] = None,
    ) -> Any:
        """Generic request with auth, timeout, and error handling."""
        url = f"{self.base_url}{path}"
        headers = self._auth_headers()
        log_data = "[REDACTED]" if "/auth/login" in path else json_data
        self.logger.debug("%s %s json=%s params=%s", method, url, log_data, params)
        resp = self.session.request(
            method,
            url,
            headers=headers,
            json=json_data,
            params=params,
            files=files,
            timeout=60,
        )
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(
        self,
        path: str,
        json_data: Optional[Any] = None,
        files: Optional[Any] = None,
    ) -> Any:
        return self._request("POST", path, json_data=json_data, files=files)

    def _put(self, path: str, json_data: Optional[Any] = None) -> Any:
        return self._request("PUT", path, json_data=json_data)

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    # -- Auth --------------------------------------------------------------

    def login(self, username: str, password: str) -> Dict[str, Any]:
        """POST /auth/login -- store token in config."""
        data = self._request(
            "POST", "/auth/login", json_data={"username": username, "password": password}
        )
        self.config.arcane_token = data.get("token", "")
        self.config.arcane_refresh_token = data.get("refreshToken", data.get("refresh_token", ""))
        self.logger.info("Arcane login successful")
        return data

    # -- Health ------------------------------------------------------------

    def health_check(self) -> Any:
        return self._get("/health")

    def get_version(self) -> Any:
        return self._get("/app-version")

    # -- Environments ------------------------------------------------------

    def list_environments(self) -> List[Dict[str, Any]]:
        return self._get("/environments")

    def get_environment(self, eid: str) -> Dict[str, Any]:
        return self._get(f"/environments/{eid}")

    # -- Registries --------------------------------------------------------

    def list_registries(self) -> List[Dict[str, Any]]:
        return self._get("/container-registries")

    def create_registry(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/container-registries", json_data=data)

    # -- Git repos ---------------------------------------------------------

    def list_git_repos(self) -> List[Dict[str, Any]]:
        return self._get("/customize/git-repositories")

    def create_git_repo(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/customize/git-repositories", json_data=data)

    # -- Projects ----------------------------------------------------------

    def list_projects(self, eid: str) -> List[Dict[str, Any]]:
        return self._get(f"/environments/{eid}/projects")

    def create_project(self, eid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"/environments/{eid}/projects", json_data=data)

    def deploy_project(
        self, eid: str, pid: str, options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._post(f"/environments/{eid}/projects/{pid}/deploy", json_data=options)

    # -- GitOps ------------------------------------------------------------

    def create_gitops_sync(self, eid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"/environments/{eid}/gitops-syncs", json_data=data)

    # -- Networks ----------------------------------------------------------

    def list_networks(self, eid: str) -> List[Dict[str, Any]]:
        return self._get(f"/environments/{eid}/networks")

    def create_network(self, eid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"/environments/{eid}/networks", json_data=data)

    # -- Volumes -----------------------------------------------------------

    def list_volumes(self, eid: str) -> List[Dict[str, Any]]:
        return self._get(f"/environments/{eid}/volumes")

    def create_volume(self, eid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"/environments/{eid}/volumes", json_data=data)

    def upload_volume_backup(
        self, eid: str, name: str, filepath: str
    ) -> Dict[str, Any]:
        with open(filepath, "rb") as fh:
            return self._post(
                f"/environments/{eid}/volumes/{name}/backups/upload",
                files={"file": (os.path.basename(filepath), fh, "application/gzip")},
            )

    # -- Containers --------------------------------------------------------

    def list_containers(self, eid: str) -> List[Dict[str, Any]]:
        return self._get(f"/environments/{eid}/containers")

    def create_container(self, eid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"/environments/{eid}/containers", json_data=data)

    def start_container(self, eid: str, cid: str) -> Dict[str, Any]:
        return self._post(f"/environments/{eid}/containers/{cid}/start")

    # -- Users -------------------------------------------------------------

    def list_users(self) -> List[Dict[str, Any]]:
        return self._get("/users")

    def create_user(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/users", json_data=data)

    # -- Webhooks ----------------------------------------------------------

    def list_webhooks(self, eid: str) -> List[Dict[str, Any]]:
        return self._get(f"/environments/{eid}/webhooks")

    def create_webhook(self, eid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(f"/environments/{eid}/webhooks", json_data=data)

    # -- Notifications -----------------------------------------------------

    def get_notification_settings(self, eid: str) -> Dict[str, Any]:
        return self._get(f"/environments/{eid}/notification-settings")

    def create_notification_settings(
        self, eid: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self._post(f"/environments/{eid}/notification-settings", json_data=data)

    # -- Settings ----------------------------------------------------------

    def get_settings(self, eid: str) -> Dict[str, Any]:
        return self._get(f"/environments/{eid}/settings")

    def update_settings(self, eid: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._put(f"/environments/{eid}/settings", json_data=data)

    # -- Templates ---------------------------------------------------------

    def list_templates(self) -> List[Dict[str, Any]]:
        return self._get("/templates")

    def create_template(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/templates", json_data=data)


# ---------------------------------------------------------------------------
# DockerLocal
# ---------------------------------------------------------------------------


class DockerLocal:
    """Local Docker socket / CLI operations for volume backup, etc."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger

    def is_available(self) -> bool:
        """Return True if the docker CLI is functional."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def backup_volume(self, name: str, backup_dir: str) -> Optional[str]:
        """Create a tar.gz backup of a Docker volume. Returns filepath or None."""
        os.makedirs(backup_dir, exist_ok=True)
        archive_name = f"{name}.tar.gz"
        self.logger.info("Backing up volume %s to %s/%s", name, backup_dir, archive_name)
        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{name}:/source:ro",
                    "-v", f"{os.path.abspath(backup_dir)}:/backup",
                    "alpine",
                    "tar", "czf", f"/backup/{archive_name}", "-C", "/source", ".",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                self.logger.error(
                    "Volume backup failed for %s: %s", name, result.stderr.strip()
                )
                return None
            filepath = os.path.join(backup_dir, archive_name)
            self.logger.info("Volume %s backed up to %s", name, filepath)
            return filepath
        except subprocess.TimeoutExpired:
            self.logger.error("Volume backup timed out for %s", name)
            return None

    def get_volume_size(self, name: str) -> Optional[int]:
        """Return the size in bytes of a volume, or None on failure."""
        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{name}:/source:ro",
                    "alpine",
                    "du", "-sb", "/source",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return None
            # Output: "<bytes>\t/source\n"
            return int(result.stdout.strip().split()[0])
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            return None

    def list_compose_containers(self, project: str) -> List[str]:
        """Return container IDs belonging to a Docker Compose project."""
        try:
            result = subprocess.run(
                [
                    "docker", "ps", "-a",
                    "--filter", f"label=com.docker.compose.project={project}",
                    "--format", "{{.ID}}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return []
            return [cid.strip() for cid in result.stdout.strip().splitlines() if cid.strip()]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []


# ---------------------------------------------------------------------------
# WizardUI
# ---------------------------------------------------------------------------


class WizardUI:
    """Terminal-based wizard interface using rich for all console output."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.console = console  # global Console instance

    # -- Banner & phase helpers --------------------------------------------

    def banner(self):
        """Display welcome banner with version, platform info."""
        banner_text = Text()
        banner_text.append("Portainer ", style="bold cyan")
        banner_text.append("-> ", style="bold white")
        banner_text.append("Arcane ", style="bold green")
        banner_text.append("Migration Tool", style="bold white")
        banner_text.append(f"\nv{__version__}", style="dim")
        banner_text.append(
            f"  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", style="dim"
        )
        banner_text.append(f"  |  Platform: {self.config.platform_name}", style="dim")
        banner_text.append(
            f"  |  Docker: {'[green]available[/green]' if self.config.has_docker else '[yellow]not found[/yellow]'}",
            style="dim",
        )
        if self.config.log_file:
            banner_text.append(f"\nLog file: {self.config.log_file}", style="dim")
        if self.config.dry_run:
            banner_text.append("\n[DRY RUN MODE]", style="bold yellow")

        self.console.print(
            Panel(
                banner_text,
                title="[cyan]migrate.py[/cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    def phase_header(self, phase_num, total: int, title: str):
        """Display phase separator. phase_num can be int or string like '1.5'."""
        self.console.print()
        self.console.rule(
            f"[bold blue]Phase {phase_num} of {total}: {title}[/bold blue]"
        )
        self.console.print()

    # -- Message helpers ---------------------------------------------------

    def success(self, msg: str):
        """Green checkmark prefix."""
        self.console.print(f"  [green]\u2714[/green] {msg}")

    def warning(self, msg: str):
        """Yellow triangle prefix."""
        self.console.print(f"  [yellow]\u26a0[/yellow] {msg}")

    def error(self, msg: str):
        """Red cross prefix."""
        self.console.print(f"  [red]\u2718[/red] {msg}")

    def info(self, msg: str):
        """Blue info prefix."""
        self.console.print(f"  [blue]\u2139[/blue] {msg}")

    def dry_run_msg(self, msg: str):
        """Display a dry-run notice."""
        self.console.print(f"  [yellow][DRY RUN][/yellow] Would {msg}")

    # -- Edition panel -----------------------------------------------------

    def show_edition_panel(self, edition: str, version: str):
        """Show CE or EE panel after connection."""
        if edition == "EE":
            lines = [
                f"[bold]Portainer Business Edition {version}[/bold]\n",
                "[green]\u2714[/green] Webhooks available for migration",
                "[green]\u2714[/green] RBAC export available",
                "[green]\u2714[/green] Activity logs can be exported",
                "[green]\u2714[/green] Edge device detection enabled",
            ]
            self.console.print(
                Panel(
                    "\n".join(lines),
                    title="[green]Enterprise Edition Detected[/green]",
                    border_style="green",
                )
            )
        else:
            lines = [
                f"[bold]Portainer Community Edition {version}[/bold]\n",
                "[yellow]\u26a0[/yellow] Webhooks may not be available",
                "[yellow]\u26a0[/yellow] Teams and roles are not available",
                "[yellow]\u26a0[/yellow] Activity logs are not available",
                "",
                "[blue]\u2139[/blue] All core resources (stacks, containers, volumes, networks, registries, users) fully supported",
            ]
            self.console.print(
                Panel(
                    "\n".join(lines),
                    title="[cyan]Community Edition Detected[/cyan]",
                    border_style="cyan",
                )
            )

    # -- Connection prompts ------------------------------------------------

    def ask_portainer_connection(self):
        """Prompt for Portainer URL, API key, SSL verify. Store in config."""
        self.console.print("\n[bold cyan]Portainer Connection[/bold cyan]")
        self.config.portainer_url = Prompt.ask(
            "  [blue]Portainer URL[/blue]",
            default="https://portainer.example.com:9443",
        )
        self.config.portainer_api_key = Prompt.ask(
            "  [blue]API Key[/blue]", password=True
        )
        self.config.portainer_ssl_verify = Confirm.ask(
            "  [blue]Verify SSL certificate?[/blue]", default=True
        )

    def ask_arcane_connection(self):
        """Prompt for Arcane URL, auth method, credentials. Store in config."""
        self.console.print("\n[bold cyan]Arcane Connection[/bold cyan]")
        self.config.arcane_url = Prompt.ask(
            "  [blue]Arcane URL[/blue]",
            default="https://arcane.example.com",
        )
        auth_method = Prompt.ask(
            "  [blue]Auth method[/blue]",
            choices=["apikey", "login"],
            default="apikey",
        )
        if auth_method == "apikey":
            self.config.arcane_api_key = Prompt.ask(
                "  [blue]API Key[/blue]", password=True
            )
        else:
            self.config.arcane_username = Prompt.ask(
                "  [blue]Username[/blue]", default="admin"
            )
            self.config.arcane_password = Prompt.ask(
                "  [blue]Password[/blue]", password=True
            )

    # -- Selection prompts -------------------------------------------------

    def select_endpoint(self, endpoints: list) -> int:
        """Show numbered table of Portainer endpoints. Return endpoint ID."""
        if len(endpoints) == 1:
            ep = endpoints[0]
            self.info(
                f"Auto-selected endpoint: [bold]{ep.get('Name', 'unknown')}[/bold] (ID {ep['Id']})"
            )
            return ep["Id"]

        table = Table(title="Portainer Endpoints", box=box.ROUNDED)
        table.add_column("#", style="dim", width=4)
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="bold")
        table.add_column("URL", style="dim")
        table.add_column("Status", style="green")

        for idx, ep in enumerate(endpoints, 1):
            status = "[green]Up[/green]" if ep.get("Status") == 1 else "[red]Down[/red]"
            table.add_row(
                str(idx),
                str(ep.get("Id", "")),
                ep.get("Name", ""),
                ep.get("URL", ""),
                status,
            )

        self.console.print(table)
        choice = IntPrompt.ask(
            "  [blue]Select endpoint #[/blue]", default=1
        )
        selected = endpoints[max(0, min(choice - 1, len(endpoints) - 1))]
        return selected["Id"]

    def select_arcane_environment(self, environments: list) -> str:
        """Show numbered table of Arcane environments. Return environment ID."""
        if len(environments) == 1:
            env = environments[0]
            env_id = str(env.get("id", env.get("Id", "")))
            self.info(
                f"Auto-selected environment: [bold]{env.get('name', env.get('Name', 'unknown'))}[/bold] (ID {env_id})"
            )
            return env_id

        table = Table(title="Arcane Environments", box=box.ROUNDED)
        table.add_column("#", style="dim", width=4)
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="bold")
        table.add_column("Status", style="green")

        for idx, env in enumerate(environments, 1):
            env_id = str(env.get("id", env.get("Id", "")))
            env_name = env.get("name", env.get("Name", ""))
            env_status = env.get("status", env.get("Status", "unknown"))
            table.add_row(str(idx), env_id, env_name, str(env_status))

        self.console.print(table)
        choice = IntPrompt.ask(
            "  [blue]Select environment #[/blue]", default=1
        )
        selected = environments[max(0, min(choice - 1, len(environments) - 1))]
        return str(selected.get("id", selected.get("Id", "")))

    # -- Discovery summary -------------------------------------------------

    def show_discovery_summary(self, discovery: dict, edition: str):
        """Show resource table and NOT migrated table."""
        # -- Discovered resources table --
        table = Table(title="Discovered Resources", box=box.ROUNDED)
        table.add_column("Resource", style="bold")
        table.add_column("Count", justify="right", style="cyan")
        table.add_column("Details", style="dim")
        table.add_column("Edition", style="dim")

        # Core resources (always shown)
        core_types = [
            "stacks", "containers", "volumes", "networks",
            "registries", "users", "custom_templates", "settings",
        ]
        for rtype in core_types:
            data = discovery.get(rtype, {})
            count = data.get("count", 0) if isinstance(data, dict) else len(data) if isinstance(data, list) else 0
            details = data.get("details", "") if isinstance(data, dict) else ""
            table.add_row(DISPLAY_NAMES.get(rtype, rtype), str(count), str(details), "CE+EE")

        # EE-only resources
        if edition == "EE":
            ee_types = [
                "webhooks", "teams", "team_memberships", "roles",
                "resource_controls", "edge_stacks", "activity_logs",
            ]
            for rtype in ee_types:
                data = discovery.get(rtype, {})
                count = data.get("count", 0) if isinstance(data, dict) else len(data) if isinstance(data, list) else 0
                details = data.get("details", "") if isinstance(data, dict) else ""
                table.add_row(DISPLAY_NAMES.get(rtype, rtype), str(count), str(details), "EE only")

        self.console.print(table)

        # -- NOT migrated table --
        not_migrated = Table(title="NOT Migrated (reference only)", box=box.ROUNDED)
        not_migrated.add_column("Resource", style="yellow")
        not_migrated.add_column("Reason", style="dim")
        not_migrated.add_column("Edition", style="dim")

        not_migrated.add_row("Endpoint Groups", "No equivalent in Arcane", "CE+EE")
        not_migrated.add_row("SSL Certificates", "Managed differently in Arcane", "CE+EE")

        if edition == "EE":
            ee_not_migrated = [
                ("Teams & Memberships", "EE RBAC -- exported for reference"),
                ("Granular Roles", "EE RBAC -- exported for reference"),
                ("Resource ACLs", "EE RBAC -- exported for reference"),
                ("Edge Groups/Jobs/Stacks", "Edge compute -- exported for reference"),
                ("Activity Logs", "Audit data -- exported for reference"),
            ]
            for name, reason in ee_not_migrated:
                not_migrated.add_row(name, reason, "EE only")

        self.console.print(not_migrated)

    # -- Strategy & scope --------------------------------------------------

    def ask_strategy(self):
        """Prompt: export/live, dry-run toggle, backup dir. Store in config."""
        self.console.print("\n[bold cyan]Migration Strategy[/bold cyan]")
        self.config.strategy = Prompt.ask(
            "  [blue]Strategy[/blue] (export = files only, live = export + import)",
            choices=["export", "live"],
            default=self.config.strategy,
        )
        self.config.dry_run = Confirm.ask(
            "  [blue]Enable dry-run mode?[/blue] (simulate without changes)",
            default=self.config.dry_run,
        )
        self.config.backup_dir = Prompt.ask(
            "  [blue]Backup / export directory[/blue]",
            default=self.config.backup_dir,
        )

    def ask_scope_confirmation(self, discovery: dict, edition: str) -> bool:
        """Ask 'Migrate ALL?' If no, show per-type selection. Return True if any selected."""
        migrate_all = Confirm.ask(
            "\n  [blue]Migrate ALL discovered resources?[/blue]", default=True
        )

        core_types = [
            "stacks", "containers", "volumes", "networks",
            "registries", "users", "custom_templates", "settings",
            "webhooks",
        ]
        ee_types = [
            "teams", "team_memberships", "roles",
            "resource_controls", "edge_stacks", "activity_logs",
        ]

        if migrate_all:
            self.config.selected_items = {
                rt: [] for rt in core_types
            }
            if edition == "EE":
                for rt in ee_types:
                    self.config.selected_items[rt] = []
            return True

        # Per-type selection
        self.config.selected_items = {}

        self.console.print("\n  [bold]Core resources:[/bold]")
        for rt in core_types:
            data = discovery.get(rt, {})
            count = data.get("count", 0) if isinstance(data, dict) else len(data) if isinstance(data, list) else 0
            display = DISPLAY_NAMES.get(rt, rt)
            if count > 0 and Confirm.ask(
                f"    [blue]Migrate {display}?[/blue] ({count} found)",
                default=True,
            ):
                self.config.selected_items[rt] = []

        if edition == "EE":
            self.console.print("\n  [bold]EE-only resources:[/bold]")
            for rt in ee_types:
                data = discovery.get(rt, {})
                count = data.get("count", 0) if isinstance(data, dict) else len(data) if isinstance(data, list) else 0
                display = DISPLAY_NAMES.get(rt, rt)
                if count > 0 and Confirm.ask(
                    f"    [blue]Migrate {display}?[/blue] ({count} found)",
                    default=True,
                ):
                    self.config.selected_items[rt] = []

        return bool(self.config.selected_items)

    # -- Preflight ---------------------------------------------------------

    def show_preflight_results(self, results: list) -> bool:
        """Show preflight table. If any FAIL, ask continue? Return bool."""
        table = Table(title="Preflight Checks", box=box.ROUNDED)
        table.add_column("Check", style="bold")
        table.add_column("Result", justify="center")
        table.add_column("Details", style="dim")

        has_fail = False
        for r in results:
            status = r.get("status", "pass")
            if status == "pass":
                styled_status = "[green]PASS[/green]"
            elif status == "warn":
                styled_status = "[yellow]WARN[/yellow]"
            else:
                styled_status = "[red]FAIL[/red]"
                has_fail = True
            table.add_row(r.get("name", ""), styled_status, r.get("details", ""))

        self.console.print(table)

        if has_fail:
            return Confirm.ask(
                "  [yellow]Some checks failed. Continue anyway?[/yellow]",
                default=False,
            )
        return True

    # -- Migration plan ----------------------------------------------------

    def show_migration_plan(self, config: Config):
        """Show Panel with migration plan details."""
        lines = [
            f"[bold]Strategy:[/bold]       {config.strategy}",
            f"[bold]Dry run:[/bold]        {'[yellow]Yes[/yellow]' if config.dry_run else '[green]No[/green]'}",
            f"[bold]Backup dir:[/bold]     [dim]{config.backup_dir}[/dim]",
            f"[bold]Docker local:[/bold]   {'[green]available[/green]' if config.has_docker else '[yellow]not available[/yellow]'}",
            "",
            "[bold]Execution order:[/bold]",
        ]

        execution_order = [
            "1. Registries",
            "2. Networks",
            "3. Volumes (with backup if docker available)",
            "4. Stacks / Compose projects",
            "5. Standalone containers",
            "6. Custom templates",
            "7. Users",
            "8. Webhooks (EE or if available)",
            "9. Settings",
        ]
        for step in execution_order:
            lines.append(f"  {step}")

        self.console.print(
            Panel(
                "\n".join(lines),
                title="[cyan]Migration Plan[/cyan]",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    # -- Progress ----------------------------------------------------------

    def create_progress(self) -> Progress:
        """Return configured Progress with spinner + text + bar + percentage."""
        return Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=self.console,
        )

    # -- Final report ------------------------------------------------------

    def show_final_report(self, report: dict, edition: str):
        """Show final migration report with summary, errors, and action items."""
        self.console.print()
        self.console.rule("[bold cyan]Migration Report[/bold cyan]")
        self.console.print()

        # 1. Summary table
        summary = Table(title="Migration Summary", box=box.ROUNDED)
        summary.add_column("Resource", style="bold")
        summary.add_column("Migrated", justify="right", style="green")
        summary.add_column("Failed", justify="right", style="red")
        summary.add_column("Skipped", justify="right", style="yellow")
        summary.add_column("Edition", style="dim")

        resources = report.get("resources", {})
        for rtype, data in resources.items():
            ed = "EE only" if rtype in (
                "webhooks", "teams", "team_memberships", "roles",
                "resource_controls", "edge_stacks",
            ) else "CE+EE"
            summary.add_row(
                rtype.replace("_", " ").title(),
                str(data.get("migrated", 0)),
                str(data.get("failed", 0)),
                str(data.get("skipped", 0)),
                ed,
            )

        self.console.print(summary)

        # 2. EE Reference Exports table
        ee_ref = report.get("ee_reference", {})
        if edition == "EE" and ee_ref:
            self.console.print()
            ee_table = Table(title="EE Reference Exports", box=box.ROUNDED)
            ee_table.add_column("Resource", style="bold")
            ee_table.add_column("Count", justify="right", style="cyan")
            ee_table.add_column("File", style="dim")

            for rtype, data in ee_ref.items():
                ee_table.add_row(
                    rtype.replace("_", " ").title(),
                    str(data.get("count", 0)),
                    data.get("file_path", ""),
                )

            self.console.print(ee_table)

        # 3. Errors list
        errors = report.get("errors", [])
        if errors:
            self.console.print()
            self.console.print("[bold red]Errors:[/bold red]")
            for err in errors:
                self.console.print(
                    f"  [red]\u2718[/red] [{err.get('resource_type', '')}] "
                    f"{err.get('name', '')}: {err.get('error', '')}"
                )

        # 4. Action items
        action_items = report.get("action_items", [])
        if not action_items:
            # Provide sensible defaults
            action_items = []
        # Always suggest common post-migration steps
        default_actions = [
            "Reset user passwords in Arcane (passwords cannot be migrated)",
            "Manually map webhooks if URLs differ between systems",
            "Review RBAC / role assignments in Arcane",
            "Review and adjust environment settings in Arcane",
        ]
        all_actions = list(action_items) + [
            a for a in default_actions if a not in action_items
        ]

        if all_actions:
            self.console.print()
            self.console.print("[bold yellow]Action Items:[/bold yellow]")
            for item in all_actions:
                self.console.print(f"  [yellow]\u26a0[/yellow] {item}")

        # 5. File paths
        config = report.get("config", {})
        backup_dir = config.get("backup_dir", "./migration_export")
        self.console.print()
        self.console.print("[bold cyan]Output Files:[/bold cyan]")
        file_paths = [
            ("Report", f"{backup_dir}/migration_report.json"),
            ("Rollback script", f"{backup_dir}/rollback.sh"),
            ("Log", config.get("log_file", "")),
            ("Backup directory", backup_dir),
        ]
        if edition == "EE":
            file_paths.append(("EE reference", f"{backup_dir}/ee_reference/"))

        for label, path in file_paths:
            if path:
                self.console.print(f"  [dim]{label}:[/dim] {path}")

        self.console.print()


# ---------------------------------------------------------------------------
# ReportGenerator
# ---------------------------------------------------------------------------


class ReportGenerator:
    """Tracks migration results and generates output files."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.report: Dict[str, Any] = {
            "version": __version__,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "portainer_edition": config.portainer_edition,
            "config": config.to_dict(),
            "resources": {},
            "ee_reference": {},
            "errors": [],
            "action_items": [],
            "completed_at": None,
        }
        self.rollback_commands: List[Dict[str, str]] = []

    def _ensure_resource(self, resource_type: str):
        """Ensure the resource_type key exists in the report resources dict."""
        if resource_type not in self.report["resources"]:
            self.report["resources"][resource_type] = {
                "migrated": 0,
                "failed": 0,
                "skipped": 0,
                "items": [],
            }

    def record_success(self, resource_type: str, name: str, source_id: Any, target_id: Any):
        """Track a successful migration."""
        self._ensure_resource(resource_type)
        self.report["resources"][resource_type]["migrated"] += 1
        self.report["resources"][resource_type]["items"].append({
            "name": name,
            "source_id": source_id,
            "target_id": target_id,
            "status": "migrated",
        })
        self.logger.info("Migrated %s: %s (source=%s -> target=%s)", resource_type, name, source_id, target_id)

    def record_failure(self, resource_type: str, name: str, error: str):
        """Track a failed migration."""
        self._ensure_resource(resource_type)
        self.report["resources"][resource_type]["failed"] += 1
        self.report["errors"].append({
            "resource_type": resource_type,
            "name": name,
            "error": str(error),
        })
        self.logger.error("Failed %s: %s -- %s", resource_type, name, error)

    def record_skip(self, resource_type: str, name: str, reason: str):
        """Track a skipped resource."""
        self._ensure_resource(resource_type)
        self.report["resources"][resource_type]["skipped"] += 1
        self.logger.info("Skipped %s: %s -- %s", resource_type, name, reason)

    def record_ee_export(self, resource_type: str, count: int, file_path: str):
        """Track an EE-only reference export."""
        self.report["ee_reference"][resource_type] = {
            "count": count,
            "file_path": file_path,
        }
        self.logger.info("EE export %s: %d items -> %s", resource_type, count, file_path)

    def add_action_item(self, message: str):
        """Append a post-migration action item."""
        self.report["action_items"].append(message)

    def add_rollback(self, method: str, url: str, description: str):
        """Append a rollback command."""
        self.rollback_commands.append({
            "method": method,
            "url": url,
            "description": description,
        })

    def save_report(self) -> str:
        """Save JSON report to migration_export/migration_report.json. Return file path."""
        self.report["completed_at"] = datetime.now(timezone.utc).isoformat()
        report_dir = self.config.backup_dir
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, "migration_report.json")
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(self.report, fh, indent=2, default=str)
        self.logger.info("Migration report saved to %s", report_path)
        return report_path

    def save_rollback_script(self) -> Optional[str]:
        """Generate rollback.sh with curl commands (reversed order). Return path or None."""
        if not self.rollback_commands:
            return None

        report_dir = self.config.backup_dir
        os.makedirs(report_dir, exist_ok=True)
        script_path = os.path.join(report_dir, "rollback.sh")

        api_key = self.config.arcane_api_key or "YOUR_API_KEY_HERE"
        lines = [
            "#!/usr/bin/env bash",
            "# Rollback script -- generated by migrate.py",
            f"# Generated: {datetime.now(timezone.utc).isoformat()}",
            "# Review carefully before executing!",
            "",
            'set -euo pipefail',
            "",
            f'API_KEY="{api_key}"',
            "",
        ]

        for cmd in reversed(self.rollback_commands):
            lines.append(f"# {cmd['description']}")
            lines.append(f'curl -X {cmd["method"]} "{cmd["url"]}" -H "X-API-Key: $API_KEY"')
            lines.append("")

        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

        os.chmod(script_path, 0o755)
        self.logger.info("Rollback script saved to %s", script_path)
        return script_path


# ---------------------------------------------------------------------------
# MigrationEngine
# ---------------------------------------------------------------------------


class MigrationEngine:
    """Orchestrates the full migration with checkpoint/resume and CE/EE branching."""

    PHASES = [
        "portainer_backup", "registries", "git_repos", "networks", "volumes",
        "stacks", "gitops_syncs", "containers", "templates", "users",
        "webhooks", "ee_rbac_export", "ee_audit_export",
    ]

    REGISTRY_TYPE_MAP = {
        1: "custom", 2: "custom", 3: "custom", 4: "custom",
        5: "custom", 6: "dockerhub", 7: "ecr", 8: "custom",
    }

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.portainer = PortainerClient(config, logger)
        self.arcane = ArcaneClient(config, logger)
        self.docker = DockerLocal(config, logger)
        self.ui = WizardUI(config, logger)
        self.report = ReportGenerator(config, logger)
        self._git_repo_map: Dict[str, str] = {}  # stack_id -> arcane_repo_id
        self.state = self._load_state()
        self.discovery: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Task 7: Checkpoint & State Management
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        """Load from checkpoint file if exists and config_hash matches.

        Otherwise return fresh state with all phases pending.
        """
        cp = self.config.checkpoint_file
        if os.path.isfile(cp):
            try:
                with open(cp, "r", encoding="utf-8") as fh:
                    saved = json.load(fh)
                if saved.get("config_hash") == self.config.config_hash():
                    self.logger.info(
                        "Resuming from checkpoint: %s", cp
                    )
                    self._git_repo_map = saved.get("git_repo_map", {})
                    return saved
                self.logger.warning(
                    "Checkpoint config_hash mismatch -- starting fresh"
                )
            except (json.JSONDecodeError, OSError) as exc:
                self.logger.warning("Could not load checkpoint: %s", exc)

        # Fresh state
        return {
            "config_hash": self.config.config_hash(),
            "phases": {phase: "pending" for phase in self.PHASES},
            "migrated_items": {phase: [] for phase in self.PHASES},
        }

    def _save_state(self):
        """Write state to checkpoint file as JSON."""
        self.state["git_repo_map"] = self._git_repo_map
        try:
            with open(self.config.checkpoint_file, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, indent=2, default=str)
        except OSError as exc:
            self.logger.warning("Could not save checkpoint: %s", exc)

    def _phase_status(self, phase: str) -> str:
        """Return status of a phase from state."""
        return self.state.get("phases", {}).get(phase, "pending")

    def _mark_phase(self, phase: str, status: str):
        """Update phase status and save."""
        self.state.setdefault("phases", {})[phase] = status
        self._save_state()

    def _is_migrated(self, phase: str, item_id: str) -> bool:
        """Check if item already migrated (for resume)."""
        migrated = self.state.get("migrated_items", {}).get(phase, [])
        return str(item_id) in migrated

    def _record_migrated(self, phase: str, item_id: str):
        """Add item to migrated list and save."""
        self.state.setdefault("migrated_items", {}).setdefault(phase, [])
        item_str = str(item_id)
        if item_str not in self.state["migrated_items"][phase]:
            self.state["migrated_items"][phase].append(item_str)
        self._save_state()

    def _should_include(self, resource_type: str) -> bool:
        """Check if resource type is in user's scope selection.

        An empty ``selected_items`` dict means nothing was selected (exclude all).
        If the key is present, the resource type is included.  An empty list
        value for the key means *all items* of that type are selected.
        """
        return resource_type in self.config.selected_items

    def _is_ee(self) -> bool:
        """Shorthand for Enterprise Edition check."""
        return self.config.portainer_edition == "EE"

    def _execute_or_log(
        self,
        action: str,
        api_call: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Dry-run wrapper: if dry_run, log and return stub; else call api_call."""
        if self.config.dry_run:
            self.ui.dry_run_msg(action)
            self.logger.debug("[DRY RUN] %s args=%s kwargs=%s", action, args, kwargs)
            return {"dry_run": True, "action": action}
        return api_call(*args, **kwargs)

    # ------------------------------------------------------------------
    # Task 8: Discovery Phase
    # ------------------------------------------------------------------

    def discover(self) -> dict:
        """Enumerate all Portainer resources. Returns discovery dict."""
        self.ui.phase_header(2, 7, "Discovery & Audit")
        discovery: Dict[str, Any] = {}

        # Count items to discover: 8 for CE, 12 for EE
        total_items = 12 if self._is_ee() else 8

        with self.ui.create_progress() as progress:
            task = progress.add_task("Discovering resources...", total=total_items)

            # --- CE + EE resources ---

            # Stacks: classify as file-based vs git-based
            stacks = self.portainer.list_stacks()
            compose_stacks = [s for s in stacks if s.get("Type") == 2]
            git_stacks = [s for s in compose_stacks if s.get("GitConfig")]
            file_stacks = [s for s in compose_stacks if not s.get("GitConfig")]
            discovery["stacks"] = {
                "count": len(compose_stacks),
                "details": f"{len(file_stacks)} file-based, {len(git_stacks)} git-based",
                "edition": "CE + EE",
                "data": compose_stacks,
            }
            progress.advance(task)

            # Containers: separate standalone from compose-managed
            containers = self.portainer.list_containers(all=True)
            standalone = [
                c for c in containers
                if not c.get("Labels", {}).get("com.docker.compose.project")
            ]
            compose_count = len(containers) - len(standalone)
            discovery["standalone_containers"] = {
                "count": len(standalone),
                "details": f"({compose_count} compose-managed excluded)",
                "edition": "CE + EE",
                "data": standalone,
            }
            progress.advance(task)

            # Images
            images = self.portainer.list_images()
            total_size = sum(img.get("Size", 0) for img in images)
            size_gb = total_size / (1024**3)
            discovery["images"] = {
                "count": len(images),
                "details": f"{size_gb:.1f} GB total",
                "edition": "CE + EE",
                "data": images,
            }
            progress.advance(task)

            # Volumes
            vol_data = self.portainer.list_volumes()
            volumes = vol_data.get("Volumes", []) or []
            discovery["volumes"] = {
                "count": len(volumes),
                "details": "",
                "edition": "CE + EE",
                "data": volumes,
            }
            progress.advance(task)

            # Networks: filter out defaults
            networks = self.portainer.list_networks()
            default_nets = {"bridge", "host", "none", "ingress", "docker_gwbridge"}
            user_networks = [
                n for n in networks if n.get("Name") not in default_nets
            ]
            discovery["networks"] = {
                "count": len(user_networks),
                "details": f"({len(networks) - len(user_networks)} default excluded)",
                "edition": "CE + EE",
                "data": user_networks,
            }
            progress.advance(task)

            # Registries
            registries = self.portainer.list_registries()
            discovery["registries"] = {
                "count": len(registries),
                "details": ", ".join(
                    r.get("Name", "")[:20] for r in registries[:3]
                ),
                "edition": "CE + EE",
                "data": registries,
            }
            progress.advance(task)

            # Custom Templates
            templates = self.portainer.list_custom_templates()
            discovery["custom_templates"] = {
                "count": len(templates),
                "details": ", ".join(
                    t.get("Title", "")[:20] for t in templates[:3]
                ),
                "edition": "CE + EE",
                "data": templates,
            }
            progress.advance(task)

            # Users
            users = self.portainer.list_users()
            discovery["users"] = {
                "count": len(users),
                "details": ", ".join(u.get("Username", "") for u in users[:4]),
                "edition": "CE + EE",
                "data": users,
            }
            progress.advance(task)

            # --- EE-only resources ---
            if self._is_ee():
                webhooks = self.portainer.list_webhooks()
                discovery["webhooks"] = {
                    "count": len(webhooks),
                    "details": "",
                    "edition": "EE",
                    "data": webhooks,
                }
                progress.advance(task)

                teams = self.portainer.list_teams()
                discovery["teams"] = {
                    "count": len(teams),
                    "details": ", ".join(
                        t.get("Name", "") for t in teams[:3]
                    ),
                    "edition": "EE",
                    "data": teams,
                }
                progress.advance(task)

                roles = self.portainer.list_roles()
                discovery["roles"] = {
                    "count": len(roles),
                    "details": "",
                    "edition": "EE",
                    "data": roles,
                }
                progress.advance(task)

                edge_stacks = self.portainer.list_edge_stacks()
                discovery["edge_stacks"] = {
                    "count": len(edge_stacks),
                    "details": "(detected)" if edge_stacks else "(none)",
                    "edition": "EE",
                    "data": edge_stacks,
                }
                progress.advance(task)
            else:
                # CE: attempt webhooks gracefully
                webhooks = self.portainer.list_webhooks()
                if webhooks:
                    discovery["webhooks"] = {
                        "count": len(webhooks),
                        "details": "",
                        "edition": "CE",
                        "data": webhooks,
                    }

            # Settings (always export as reference)
            settings = self.portainer.get_settings()
            discovery["settings"] = {
                "count": 1,
                "details": "Exported for reference",
                "edition": "CE + EE",
                "data": settings,
            }

        self.discovery = discovery
        self.ui.show_discovery_summary(discovery, self.config.portainer_edition)
        return discovery

    # ------------------------------------------------------------------
    # Task 9: Data Transformation Helpers
    # ------------------------------------------------------------------

    def _transform_registry(self, reg: dict) -> dict:
        """Map Portainer registry to Arcane CreateContainerRegistryRequest.

        Handles ECR special case (awsAccessKeyId, awsSecretAccessKey, awsRegion).

        Arcane schema fields: url, username, token, description, insecure,
        enabled, registryType, awsAccessKeyId, awsSecretAccessKey, awsRegion.
        Arcane has no ``name`` field for registries; ``description`` carries
        the Portainer ``Name``.  Arcane's ``token`` field holds the password.
        """
        port_type = reg.get("Type", 1)
        registry_type = self.REGISTRY_TYPE_MAP.get(port_type, "custom")

        result: Dict[str, Any] = {
            "url": reg.get("URL", ""),
            "username": reg.get("Username", ""),
            "token": reg.get("Password", ""),          # Arcane calls it "token"
            "description": reg.get("Name", ""),         # Arcane has no "name"; use description
            "insecure": False,
            "enabled": True,
            "registryType": registry_type,
        }

        # ECR special case
        if registry_type == "ecr":
            ecr = reg.get("Ecr", {}) or {}
            result["awsAccessKeyId"] = ecr.get("AccessKeyID", "")
            result["awsSecretAccessKey"] = ecr.get("SecretAccessKey", "")
            result["awsRegion"] = ecr.get("Region", "")

        # Warn about registries that may need manual credential verification
        if port_type in (2, 3, 5):
            type_names = {2: "Quay.io", 3: "Azure ACR", 5: "GitLab"}
            self.logger.warning(
                "Registry '%s' (type: %s) -- credentials may need manual verification in Arcane",
                reg.get("Name", ""),
                type_names.get(port_type, "unknown"),
            )

        return result

    def _transform_stack_to_project(self, stack: dict, compose_content: str) -> dict:
        """Convert Portainer stack to Arcane project payload.

        Converts stack.Env array [{name, value}] to envContent string (KEY=VALUE lines).
        """
        env_vars = stack.get("Env", []) or []
        env_lines = [
            f"{v.get('name', '')}={v.get('value', '')}"
            for v in env_vars
            if v.get("name")
        ]
        env_content = "\n".join(env_lines)
        if env_content:
            env_content += "\n"

        return {
            "name": stack.get("Name", ""),
            "composeContent": compose_content,
            "envContent": env_content,
        }

    def _transform_git_stack_to_gitops(self, stack: dict, repo_id: str) -> dict:
        """Convert a git-based stack to an Arcane GitOps sync payload.

        Extracts GitConfig: branch (strips ``refs/heads/``), composePath,
        autoSync, syncInterval.
        """
        git_cfg = stack.get("GitConfig", {}) or {}
        branch = git_cfg.get("ReferenceName", "main")
        # Strip refs/heads/ prefix if present
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/"):]

        auto_update = stack.get("AutoUpdate", {}) or {}
        auto_sync = bool(auto_update.get("Interval"))
        # Default sync interval: 5 minutes (300s) if auto-sync is on
        sync_interval = auto_update.get("Interval", 300) if auto_sync else 0
        # Coerce to int: handle string values like "5m" or "300"
        if isinstance(sync_interval, str):
            # Try plain int first, then extract leading digits, default 300
            try:
                sync_interval = int(sync_interval)
            except ValueError:
                import re
                m = re.match(r"(\d+)", sync_interval)
                if m:
                    val = int(m.group(1))
                    # If the suffix is 'm' (minutes), convert to seconds
                    if sync_interval.rstrip().endswith("m"):
                        sync_interval = val * 60
                    else:
                        sync_interval = val
                else:
                    sync_interval = 300

        return {
            "name": stack.get("Name", ""),
            "repositoryId": repo_id,
            "branch": branch,
            "composePath": git_cfg.get("ComposeFilePathInRepository", "docker-compose.yml"),
            "autoSync": auto_sync,
            "syncInterval": sync_interval,
        }

    def _transform_git_config_to_repo(self, stack: dict) -> dict:
        """Extract git repository info from a git-based stack.

        Returns payload for Arcane ``create_git_repo``.
        """
        git_cfg = stack.get("GitConfig", {}) or {}
        auth = git_cfg.get("Authentication", {}) or {}

        # Determine auth type
        has_token = bool(auth.get("Password") or auth.get("Token"))
        has_username = bool(auth.get("Username"))
        auth_type = "token" if (has_token or has_username) else "none"

        return {
            "name": f"repo-{stack.get('Name', 'unknown')}",
            "url": git_cfg.get("URL", ""),
            "authType": auth_type,
            "token": auth.get("Password", "") or auth.get("Token", ""),
            "username": auth.get("Username", ""),
            "enabled": True,
        }

    def _transform_network(self, net: dict) -> dict:
        """Map a Portainer/Docker network to Arcane network create payload.

        Preserves driver, IPAM config, labels, and options.
        """
        ipam_cfg = net.get("IPAM", {}) or {}
        ipam_config_list = ipam_cfg.get("Config", []) or []

        # Build IPAM section
        ipam: Dict[str, Any] = {}
        if ipam_cfg.get("Driver"):
            ipam["driver"] = ipam_cfg["Driver"]
        if ipam_config_list:
            ipam["config"] = []
            for subnet_cfg in ipam_config_list:
                entry: Dict[str, str] = {}
                if subnet_cfg.get("Subnet"):
                    entry["subnet"] = subnet_cfg["Subnet"]
                if subnet_cfg.get("Gateway"):
                    entry["gateway"] = subnet_cfg["Gateway"]
                if subnet_cfg.get("IPRange"):
                    entry["ipRange"] = subnet_cfg["IPRange"]
                if subnet_cfg.get("AuxAddress"):
                    entry["auxAddress"] = subnet_cfg["AuxAddress"]
                if entry:
                    ipam["config"].append(entry)
        if ipam_cfg.get("Options"):
            ipam["options"] = ipam_cfg["Options"]

        options: Dict[str, Any] = {
            "driver": net.get("Driver", "bridge"),
            "internal": net.get("Internal", False),
            "attachable": net.get("Attachable", False),
            "enableIPv6": net.get("EnableIPv6", False),
            "labels": net.get("Labels", {}) or {},
            "options": net.get("Options", {}) or {},
        }
        if ipam:
            options["ipam"] = ipam

        return {
            "name": net.get("Name", ""),
            "options": options,
        }

    def _transform_container(self, inspect_data: dict) -> dict:
        """FULL FIDELITY transform of Docker inspect data to Arcane ContainerCreate schema.

        Parses Config, HostConfig, Mounts, NetworkSettings from the inspect payload.
        Every field matters -- uses ``or []`` for nullable list fields to avoid None issues.
        """
        config = inspect_data.get("Config", {}) or {}
        host_config = inspect_data.get("HostConfig", {}) or {}
        mounts_raw = inspect_data.get("Mounts", []) or []
        network_settings = inspect_data.get("NetworkSettings", {}) or {}

        # --- Port bindings ---
        port_bindings_raw = host_config.get("PortBindings", {}) or {}
        port_bindings: Dict[str, Any] = {}
        for container_port, host_list in port_bindings_raw.items():
            # container_port is like "80/tcp"
            port_bindings[container_port] = [
                {
                    "hostIp": binding.get("HostIp", ""),
                    "hostPort": binding.get("HostPort", ""),
                }
                for binding in (host_list or [])
            ]

        # --- Mounts: preserve Source:Destination:Mode ---
        binds = host_config.get("Binds", []) or []
        mounts: List[Dict[str, Any]] = []
        for mount in mounts_raw:
            mount_entry: Dict[str, Any] = {
                "type": mount.get("Type", "volume"),
                "source": mount.get("Source", ""),
                "destination": mount.get("Destination", ""),
                "mode": mount.get("Mode", ""),
                "rw": mount.get("RW", True),
            }
            if mount.get("Driver"):
                mount_entry["driver"] = mount["Driver"]
            mounts.append(mount_entry)

        # --- Restart policy ---
        restart_policy = host_config.get("RestartPolicy", {}) or {}

        # --- Devices ---
        devices_raw = host_config.get("Devices", []) or []
        devices = [
            {
                "PathOnHost": d.get("PathOnHost", ""),
                "PathInContainer": d.get("PathInContainer", ""),
                "CgroupPermissions": d.get("CgroupPermissions", "rwm"),
            }
            for d in devices_raw
        ]

        # --- DNS ---
        dns = host_config.get("Dns", []) or []
        dns_search = host_config.get("DnsSearch", []) or []
        dns_options = host_config.get("DnsOptions", []) or []

        # --- Exposed ports ---
        exposed_ports = config.get("ExposedPorts", {}) or {}

        # --- Volumes as list of strings (Source:Destination:Mode) ---
        volume_strings: List[str] = []
        for mount in mounts_raw:
            src = mount.get("Source", "")
            dst = mount.get("Destination", "")
            mode = mount.get("Mode", "")
            if src and dst:
                entry_str = f"{src}:{dst}"
                if mode:
                    entry_str += f":{mode}"
                volume_strings.append(entry_str)
            elif dst:
                volume_strings.append(dst)
        # Also add any Binds not already represented
        for bind in binds:
            if bind not in volume_strings:
                volume_strings.append(bind)

        # --- Restart policy as string ---
        restart_name = restart_policy.get("Name", "")
        max_retry = restart_policy.get("MaximumRetryCount") or 0
        if restart_name == "on-failure" and max_retry:
            restart_str = f"on-failure:{max_retry}"
        else:
            restart_str = restart_name

        # --- Build the result ---
        result: Dict[str, Any] = {
            # From Config
            "image": config.get("Image", ""),
            "env": config.get("Env", []) or [],
            "cmd": config.get("Cmd", []) or [],
            "entrypoint": config.get("Entrypoint", []) or [],
            "labels": config.get("Labels", {}) or {},
            "hostname": config.get("Hostname", ""),
            "domainname": config.get("Domainname", ""),
            "user": config.get("User", ""),
            "workingDir": config.get("WorkingDir", ""),
            "tty": config.get("Tty", False),
            "openStdin": config.get("OpenStdin", False),
            "volumes": volume_strings,
            "mounts": mounts,
            "exposedPorts": exposed_ports,
            "restartPolicy": restart_str,
            "privileged": host_config.get("Privileged", False),
            # hostConfig
            "hostConfig": {
                "networkMode": host_config.get("NetworkMode", "default"),
                "portBindings": port_bindings,
                "memory": host_config.get("Memory") or 0,
                "memorySwap": host_config.get("MemorySwap") or 0,
                "nanoCpus": host_config.get("NanoCpus") or 0,
                "cpuShares": host_config.get("CpuShares") or 0,
                "capAdd": host_config.get("CapAdd", []) or [],
                "capDrop": host_config.get("CapDrop", []) or [],
                "securityOpt": host_config.get("SecurityOpt", []) or [],
                "readonlyRootfs": host_config.get("ReadonlyRootfs", False),
                "devices": devices,
                "pidsLimit": host_config.get("PidsLimit") or 0,
                "autoRemove": host_config.get("AutoRemove", False),
                "dns": dns,
                "dnsSearch": dns_search,
                "dnsOptions": dns_options,
            },
        }

        # Add healthcheck only if present (prefer runtime override: H2)
        healthcheck_raw = host_config.get("Healthcheck") or config.get("Healthcheck")
        if healthcheck_raw:
            result["healthcheck"] = {
                "test": healthcheck_raw.get("Test", []) or [],
                "interval": healthcheck_raw.get("Interval") or 0,
                "timeout": healthcheck_raw.get("Timeout") or 0,
                "retries": healthcheck_raw.get("Retries") or 0,
                "startPeriod": healthcheck_raw.get("StartPeriod") or 0,
            }

        # Attach container name (strip leading /)
        name = inspect_data.get("Name", "")
        if name.startswith("/"):
            name = name[1:]
        result["name"] = name

        return result

    def _transform_custom_template(self, template: dict, file_content: str) -> dict:
        """Convert Portainer custom template to Arcane template payload."""
        return {
            "name": template.get("Title", ""),
            "description": template.get("Description", ""),
            "content": file_content,
            "envContent": "",
        }

    def _transform_user(self, user: dict) -> dict:
        """Map Portainer user to Arcane user create payload.

        Role mapping: 1 = admin, 2 = user.
        Passwords cannot be migrated -- uses a default placeholder.
        """
        role = user.get("Role", 2)
        if role == 1:
            roles = ["admin"]
        else:
            roles = ["user"]

        return {
            "username": user.get("Username", ""),
            "password": "ChangeMe123!",
            "roles": roles,
        }

    # ------------------------------------------------------------------
    # Task 10: Export to Disk
    # ------------------------------------------------------------------

    def _export_to_disk(self):
        """Save all discovered Portainer data to the export directory.

        Called before any live migration writes.  Respects ``config.dry_run``
        (no files written) and ``_should_include()`` (user scope selection).
        """
        base = Path(self.config.backup_dir)
        manifest: Dict[str, Any] = {
            "tool_version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "portainer_edition": self.config.portainer_edition,
            "portainer_version": self.config.portainer_version,
            "counts": {},
        }

        def _write_json(filepath: Path, data: Any):
            """Write *data* as JSON unless dry-run."""
            if self.config.dry_run:
                self.ui.dry_run_msg(f"Write {filepath}")
                return
            filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)

        def _write_text(filepath: Path, text: str):
            """Write plain text unless dry-run."""
            if self.config.dry_run:
                self.ui.dry_run_msg(f"Write {filepath}")
                return
            filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(text)

        # ---- Registries ----
        if self._should_include("registries"):
            registries = self.discovery.get("registries", {}).get("data", [])
            masked = []
            for r in registries:
                entry = dict(r)
                if "Password" in entry:
                    entry["Password"] = "***MASKED***"
                # Mask ECR credentials if present
                if "Ecr" in entry and isinstance(entry["Ecr"], dict):
                    ecr = dict(entry["Ecr"])
                    if "AccessKeyID" in ecr:
                        ecr["AccessKeyID"] = "***MASKED***"
                    if "SecretAccessKey" in ecr:
                        ecr["SecretAccessKey"] = "***MASKED***"
                    entry["Ecr"] = ecr
                masked.append(entry)
            _write_json(base / "registries" / "registries.json", masked)
            manifest["counts"]["registries"] = len(masked)
            self.ui.success(f"Exported {len(masked)} registries")

        # ---- Stacks ----
        if self._should_include("stacks"):
            stacks = self.discovery.get("stacks", {}).get("data", [])
            for stack in stacks:
                name = stack.get("Name", f"stack_{stack.get('Id', 'unknown')}")
                stack_dir = base / "stacks" / name
                # Compose file
                try:
                    file_resp = self.portainer.get_stack_file(stack.get("Id", ""))
                    compose_content = file_resp.get("StackFileContent", "")
                except Exception:
                    compose_content = ""
                _write_text(stack_dir / "docker-compose.yml", compose_content)
                # .env
                env_vars = stack.get("Env", []) or []
                env_lines = [
                    f"{v.get('name', '')}={v.get('value', '')}"
                    for v in env_vars if v.get("name")
                ]
                _write_text(stack_dir / ".env", "\n".join(env_lines) + "\n" if env_lines else "")
                # metadata
                _write_json(stack_dir / "metadata.json", stack)
            manifest["counts"]["stacks"] = len(stacks)
            self.ui.success(f"Exported {len(stacks)} stacks")

        # ---- Standalone Containers ----
        if self._should_include("standalone_containers"):
            containers = self.discovery.get("standalone_containers", {}).get("data", [])
            inspected = []
            for c in containers:
                cid = c.get("Id", "")
                try:
                    inspect_data = self.portainer.inspect_container(cid)
                    inspected.append(inspect_data)
                except Exception as exc:
                    self.logger.warning("Could not inspect container %s: %s", cid, exc)
                    inspected.append(c)
            _write_json(base / "containers" / "standalone.json", inspected)
            manifest["counts"]["standalone_containers"] = len(inspected)
            self.ui.success(f"Exported {len(inspected)} standalone containers")

        # ---- Networks ----
        if self._should_include("networks"):
            networks = self.discovery.get("networks", {}).get("data", [])
            _write_json(base / "networks" / "networks.json", networks)
            manifest["counts"]["networks"] = len(networks)
            self.ui.success(f"Exported {len(networks)} networks")

        # ---- Volumes ----
        if self._should_include("volumes"):
            volumes = self.discovery.get("volumes", {}).get("data", [])
            _write_json(base / "volumes" / "volumes.json", volumes)
            manifest["counts"]["volumes"] = len(volumes)
            # Backup volume data if Docker is available
            if self.config.has_docker and self.docker.is_available():
                backup_path = base / "volumes" / "backups"
                with self.ui.create_progress() as progress:
                    task = progress.add_task("Backing up volumes...", total=len(volumes))
                    for vol in volumes:
                        vol_name = vol.get("Name", "")
                        if vol_name and not self.config.dry_run:
                            self.docker.backup_volume(vol_name, str(backup_path))
                        elif self.config.dry_run:
                            self.ui.dry_run_msg(f"Backup volume {vol_name}")
                        progress.advance(task)
                self.ui.success(f"Exported {len(volumes)} volumes (with backups)")
            else:
                self.ui.info(f"Exported {len(volumes)} volumes (metadata only -- no Docker)")

        # ---- Custom Templates ----
        if self._should_include("custom_templates"):
            templates = self.discovery.get("custom_templates", {}).get("data", [])
            enriched = []
            for t in templates:
                entry = dict(t)
                try:
                    file_resp = self.portainer.get_custom_template_file(t.get("Id", ""))
                    entry["FileContent"] = file_resp.get("FileContent", "")
                except Exception:
                    entry["FileContent"] = ""
                enriched.append(entry)
            _write_json(base / "templates" / "custom_templates.json", enriched)
            manifest["counts"]["custom_templates"] = len(enriched)
            self.ui.success(f"Exported {len(enriched)} custom templates")

        # ---- Users ----
        if self._should_include("users"):
            users = self.discovery.get("users", {}).get("data", [])
            cleaned = []
            for u in users:
                entry = dict(u)
                entry.pop("Password", None)
                cleaned.append(entry)
            _write_json(base / "users" / "users.json", cleaned)
            manifest["counts"]["users"] = len(cleaned)
            self.ui.success(f"Exported {len(cleaned)} users")

        # ---- Webhooks ----
        if self._should_include("webhooks"):
            webhooks = self.discovery.get("webhooks", {}).get("data", [])
            _write_json(base / "webhooks" / "webhooks.json", webhooks)
            manifest["counts"]["webhooks"] = len(webhooks)
            self.ui.success(f"Exported {len(webhooks)} webhooks")

        # ---- Settings ----
        if self._should_include("settings"):
            settings = self.discovery.get("settings", {}).get("data", {})

            def _mask_settings(obj: Any) -> Any:
                if isinstance(obj, dict):
                    masked = {}
                    for k, v in obj.items():
                        if isinstance(k, str) and any(
                            s in k.lower() for s in ("password", "secret")
                        ):
                            masked[k] = "***MASKED***"
                        else:
                            masked[k] = _mask_settings(v)
                    return masked
                if isinstance(obj, list):
                    return [_mask_settings(item) for item in obj]
                return obj

            _write_json(base / "settings" / "portainer_settings.json", _mask_settings(settings))
            manifest["counts"]["settings"] = 1
            self.ui.success("Exported Portainer settings")

        # ---- EE Reference Exports ----
        if self._is_ee():
            ee_dir = base / "ee_reference"

            teams = self.discovery.get("teams", {}).get("data", [])
            _write_json(ee_dir / "teams.json", teams)
            manifest["counts"]["teams"] = len(teams)
            self.ui.info(f"EE reference: {len(teams)} teams")

            try:
                memberships = self.portainer.list_team_memberships()
            except Exception:
                memberships = []
            _write_json(ee_dir / "team_memberships.json", memberships)
            manifest["counts"]["team_memberships"] = len(memberships)
            self.ui.info(f"EE reference: {len(memberships)} team memberships")

            roles = self.discovery.get("roles", {}).get("data", [])
            _write_json(ee_dir / "roles.json", roles)
            manifest["counts"]["roles"] = len(roles)
            self.ui.info(f"EE reference: {len(roles)} roles")

            try:
                resource_controls = self.portainer.list_resource_controls()
            except Exception:
                resource_controls = []
            _write_json(ee_dir / "resource_controls.json", resource_controls)
            manifest["counts"]["resource_controls"] = len(resource_controls)
            self.ui.info(f"EE reference: {len(resource_controls)} resource controls")

            edge_stacks = self.discovery.get("edge_stacks", {}).get("data", [])
            if edge_stacks:
                _write_json(ee_dir / "edge_stacks.json", edge_stacks)
                manifest["counts"]["edge_stacks"] = len(edge_stacks)
                self.ui.info(f"EE reference: {len(edge_stacks)} edge stacks")

        # ---- Manifest ----
        _write_json(base / "manifest.json", manifest)
        self.ui.success("Export manifest saved")

    # ------------------------------------------------------------------
    # Task 11: Live Migration Phases
    # ------------------------------------------------------------------

    def _portainer_backup(self):
        """Create a Portainer server backup (tar.gz)."""
        phase = "portainer_backup"
        if self._phase_status(phase) == "completed":
            self.ui.info("Portainer backup already completed -- skipping")
            return

        self._mark_phase(phase, "in_progress")

        password = Prompt.ask(
            "[bold]Backup encryption password[/bold] (blank for none)",
            password=True,
            default="",
        )

        if self.config.dry_run:
            self.ui.dry_run_msg("Portainer backup")
            self._mark_phase(phase, "completed")
            return

        try:
            backup_bytes = self.portainer.trigger_backup(password)
            backup_dir = Path(self.config.backup_dir) / "portainer_backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / "portainer_backup.tar.gz"
            with open(backup_path, "wb") as fh:
                fh.write(backup_bytes)

            size = backup_path.stat().st_size
            if size < 100:
                self.ui.warning(
                    f"Portainer backup is suspiciously small ({size} bytes)"
                )
            else:
                self.ui.success(f"Portainer backup saved ({size:,} bytes)")
            self._mark_phase(phase, "completed")
        except http_requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403):
                self.ui.warning(
                    "Portainer backup failed (permission denied) -- not fatal"
                )
                self.logger.warning("Backup permission error: %s", exc)
                self._mark_phase(phase, "completed")
            else:
                self.ui.error(f"Portainer backup failed: {exc}")
                self.logger.error("Backup error: %s", exc)
                self._mark_phase(phase, "failed")
        except Exception as exc:
            self.ui.error(f"Portainer backup failed: {exc}")
            self.logger.error("Backup error: %s", exc)
            self._mark_phase(phase, "failed")

    def _migrate_registries(self):
        """Migrate container registries from Portainer to Arcane."""
        phase = "registries"
        if self._phase_status(phase) == "completed":
            self.ui.info("Registries already migrated -- skipping")
            return
        if not self._should_include("registries"):
            self.ui.info("Registries not selected -- skipping")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        registries = self.discovery.get("registries", {}).get("data", [])

        for reg in registries:
            reg_id = str(reg.get("Id", ""))
            name = reg.get("Name", reg_id)
            if self._is_migrated(phase, reg_id):
                self.report.record_skip("Registries", name, "already migrated")
                continue
            try:
                payload = self._transform_registry(reg)
                result = self._execute_or_log(
                    f"Create registry '{name}'",
                    self.arcane.create_registry,
                    payload,
                )
                target_id = result.get("id", "") if isinstance(result, dict) else ""
                self.report.record_success("Registries", name, reg_id, target_id)
                if not self.config.dry_run:
                    self.report.add_rollback(
                        "DELETE",
                        f"{self.arcane.base_url}/container-registries/{target_id}",
                        f"Delete registry '{name}'",
                    )
                self._record_migrated(phase, reg_id)
            except Exception as exc:
                self.logger.error("Failed to migrate registry %s: %s", name, exc)
                self.report.record_failure("Registries", name, str(exc))
                self.ui.error(f"Registry '{name}' failed: {exc}")

        self._mark_phase(phase, "completed")

    def _migrate_git_repos(self):
        """Migrate git repository configs from git-based stacks."""
        phase = "git_repos"
        if self._phase_status(phase) == "completed":
            self.ui.info("Git repos already migrated -- skipping")
            return
        if not self._should_include("stacks"):
            self.ui.info("Stacks not selected -- skipping git repos")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        stacks = self.discovery.get("stacks", {}).get("data", [])
        git_stacks = [s for s in stacks if s.get("GitConfig")]

        for stack in git_stacks:
            stack_id = str(stack.get("Id", ""))
            name = stack.get("Name", stack_id)
            if self._is_migrated(phase, stack_id):
                self.report.record_skip("Git Repos", name, "already migrated")
                continue
            try:
                payload = self._transform_git_config_to_repo(stack)
                result = self._execute_or_log(
                    f"Create git repo for '{name}'",
                    self.arcane.create_git_repo,
                    payload,
                )
                target_id = result.get("id", "") if isinstance(result, dict) else ""
                self._git_repo_map[stack_id] = str(target_id)
                self.report.record_success("Git Repos", name, stack_id, target_id)
                if not self.config.dry_run:
                    self.report.add_rollback(
                        "DELETE",
                        f"{self.arcane.base_url}/customize/git-repositories/{target_id}",
                        f"Delete git repo for '{name}'",
                    )
                self._record_migrated(phase, stack_id)
            except Exception as exc:
                self.logger.error("Failed to migrate git repo for %s: %s", name, exc)
                self.report.record_failure("Git Repos", name, str(exc))
                self.ui.error(f"Git repo for '{name}' failed: {exc}")

        self._mark_phase(phase, "completed")

    def _migrate_networks(self):
        """Migrate user-defined networks to Arcane."""
        phase = "networks"
        if self._phase_status(phase) == "completed":
            self.ui.info("Networks already migrated -- skipping")
            return
        if not self._should_include("networks"):
            self.ui.info("Networks not selected -- skipping")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        eid = self.config.arcane_environment_id
        networks = self.discovery.get("networks", {}).get("data", [])

        for net in networks:
            net_id = net.get("Id", "")
            name = net.get("Name", net_id)
            if self._is_migrated(phase, net_id):
                self.report.record_skip("Networks", name, "already migrated")
                continue
            try:
                payload = self._transform_network(net)
                result = self._execute_or_log(
                    f"Create network '{name}'",
                    self.arcane.create_network,
                    eid,
                    payload,
                )
                target_id = result.get("Id", "") if isinstance(result, dict) else ""
                self.report.record_success("Networks", name, net_id, target_id)
                if not self.config.dry_run:
                    self.report.add_rollback(
                        "DELETE",
                        f"{self.arcane.base_url}/environments/{eid}/networks/{target_id}",
                        f"Delete network '{name}'",
                    )
                self._record_migrated(phase, net_id)
            except Exception as exc:
                self.logger.error("Failed to migrate network %s: %s", name, exc)
                self.report.record_failure("Networks", name, str(exc))
                self.ui.error(f"Network '{name}' failed: {exc}")

        self._mark_phase(phase, "completed")

    def _migrate_volumes(self):
        """Migrate volumes and optionally restore volume backups."""
        phase = "volumes"
        if self._phase_status(phase) == "completed":
            self.ui.info("Volumes already migrated -- skipping")
            return
        if not self._should_include("volumes"):
            self.ui.info("Volumes not selected -- skipping")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        eid = self.config.arcane_environment_id
        volumes = self.discovery.get("volumes", {}).get("data", [])

        for vol in volumes:
            name = vol.get("Name", "")
            if self._is_migrated(phase, name):
                self.report.record_skip("Volumes", name, "already migrated")
                continue
            try:
                payload = {
                    "name": name,
                    "driver": vol.get("Driver", "local"),
                    "labels": vol.get("Labels", {}) or {},
                    "driverOpts": vol.get("Options", {}) or {},
                }
                result = self._execute_or_log(
                    f"Create volume '{name}'",
                    self.arcane.create_volume,
                    eid,
                    payload,
                )
                target_id = name  # volumes are identified by name
                self.report.record_success("Volumes", name, name, target_id)
                if not self.config.dry_run:
                    self.report.add_rollback(
                        "DELETE",
                        f"{self.arcane.base_url}/environments/{eid}/volumes/{name}",
                        f"Delete volume '{name}'",
                    )
                # Upload backup if it exists
                backup_file = (
                    Path(self.config.backup_dir) / "volumes" / "backups" / f"{name}.tar.gz"
                )
                if backup_file.is_file() and not self.config.dry_run:
                    try:
                        self.arcane.upload_volume_backup(eid, name, str(backup_file))
                        self.ui.success(f"Volume '{name}' backup restored")
                    except Exception as upload_exc:
                        self.ui.warning(f"Volume '{name}' backup upload failed: {upload_exc}")
                self._record_migrated(phase, name)
            except Exception as exc:
                self.logger.error("Failed to migrate volume %s: %s", name, exc)
                self.report.record_failure("Volumes", name, str(exc))
                self.ui.error(f"Volume '{name}' failed: {exc}")

        self._mark_phase(phase, "completed")

    def _migrate_stacks(self):
        """Migrate file-based stacks as Arcane projects."""
        phase = "stacks"
        if self._phase_status(phase) == "completed":
            self.ui.info("Stacks already migrated -- skipping")
            return
        if not self._should_include("stacks"):
            self.ui.info("Stacks not selected -- skipping")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        eid = self.config.arcane_environment_id
        stacks = self.discovery.get("stacks", {}).get("data", [])
        file_stacks = [s for s in stacks if not s.get("GitConfig")]

        for stack in file_stacks:
            stack_id = str(stack.get("Id", ""))
            name = stack.get("Name", stack_id)
            if self._is_migrated(phase, stack_id):
                self.report.record_skip("Stacks", name, "already migrated")
                continue
            try:
                file_resp = self.portainer.get_stack_file(stack.get("Id", ""))
                compose_content = file_resp.get("StackFileContent", "")
                payload = self._transform_stack_to_project(stack, compose_content)
                result = self._execute_or_log(
                    f"Create project '{name}'",
                    self.arcane.create_project,
                    eid,
                    payload,
                )
                target_id = result.get("id", "") if isinstance(result, dict) else ""
                self.report.record_success("Stacks", name, stack_id, target_id)
                if not self.config.dry_run:
                    self.report.add_rollback(
                        "DELETE",
                        f"{self.arcane.base_url}/environments/{eid}/projects/{target_id}/destroy",
                        f"Destroy project '{name}'",
                    )
                self._record_migrated(phase, stack_id)
            except Exception as exc:
                self.logger.error("Failed to migrate stack %s: %s", name, exc)
                self.report.record_failure("Stacks", name, str(exc))
                self.ui.error(f"Stack '{name}' failed: {exc}")

        self._mark_phase(phase, "completed")

    def _migrate_gitops_syncs(self):
        """Create GitOps syncs for git-based stacks."""
        phase = "gitops_syncs"
        if self._phase_status(phase) == "completed":
            self.ui.info("GitOps syncs already migrated -- skipping")
            return
        if not self._should_include("stacks"):
            self.ui.info("Stacks not selected -- skipping GitOps syncs")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        eid = self.config.arcane_environment_id
        stacks = self.discovery.get("stacks", {}).get("data", [])
        git_stacks = [s for s in stacks if s.get("GitConfig")]

        for stack in git_stacks:
            stack_id = str(stack.get("Id", ""))
            name = stack.get("Name", stack_id)
            if self._is_migrated(phase, stack_id):
                self.report.record_skip("GitOps Syncs", name, "already migrated")
                continue
            repo_id = self._git_repo_map.get(stack_id)
            if not repo_id:
                self.report.record_skip(
                    "GitOps Syncs", name, "no matching git repo migrated"
                )
                self.ui.warning(f"GitOps sync for '{name}' skipped -- no repo mapping")
                continue
            try:
                payload = self._transform_git_stack_to_gitops(stack, repo_id)
                result = self._execute_or_log(
                    f"Create GitOps sync '{name}'",
                    self.arcane.create_gitops_sync,
                    eid,
                    payload,
                )
                target_id = result.get("id", "") if isinstance(result, dict) else ""
                self.report.record_success("GitOps Syncs", name, stack_id, target_id)
                if not self.config.dry_run:
                    self.report.add_rollback(
                        "DELETE",
                        f"{self.arcane.base_url}/environments/{eid}/gitops-syncs/{target_id}",
                        f"Delete GitOps sync '{name}'",
                    )
                self._record_migrated(phase, stack_id)
            except Exception as exc:
                self.logger.error("Failed to create GitOps sync for %s: %s", name, exc)
                self.report.record_failure("GitOps Syncs", name, str(exc))
                self.ui.error(f"GitOps sync for '{name}' failed: {exc}")

        self._mark_phase(phase, "completed")

    def _migrate_containers(self):
        """Migrate standalone containers to Arcane."""
        phase = "containers"
        if self._phase_status(phase) == "completed":
            self.ui.info("Containers already migrated -- skipping")
            return
        if not self._should_include("standalone_containers"):
            self.ui.info("Containers not selected -- skipping")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        eid = self.config.arcane_environment_id
        containers = self.discovery.get("standalone_containers", {}).get("data", [])

        for c in containers:
            cid = c.get("Id", "")
            # Use first name (strip leading /) or fall back to short id
            names = c.get("Names", [])
            name = names[0].lstrip("/") if names else cid[:12]
            if self._is_migrated(phase, cid):
                self.report.record_skip("Containers", name, "already migrated")
                continue
            try:
                inspect_data = self.portainer.inspect_container(cid)
                payload = self._transform_container(inspect_data)
                result = self._execute_or_log(
                    f"Create container '{name}'",
                    self.arcane.create_container,
                    eid,
                    payload,
                )
                target_id = result.get("Id", result.get("id", "")) if isinstance(result, dict) else ""
                self.report.record_success("Containers", name, cid, target_id)
                if not self.config.dry_run:
                    self.report.add_rollback(
                        "DELETE",
                        f"{self.arcane.base_url}/environments/{eid}/containers/{target_id}?force=true",
                        f"Delete container '{name}'",
                )
                # Start the container if it was running
                state = inspect_data.get("State", {})
                was_running = (
                    state.get("Status", "").lower() == "running"
                    if isinstance(state, dict)
                    else False
                )
                if was_running and target_id and not self.config.dry_run:
                    try:
                        self.arcane.start_container(eid, target_id)
                        self.ui.info(f"Started container '{name}'")
                    except Exception as start_exc:
                        self.ui.warning(f"Could not start container '{name}': {start_exc}")
                self._record_migrated(phase, cid)
            except Exception as exc:
                self.logger.error("Failed to migrate container %s: %s", name, exc)
                self.report.record_failure("Containers", name, str(exc))
                self.ui.error(f"Container '{name}' failed: {exc}")

        self._mark_phase(phase, "completed")

    def _migrate_templates(self):
        """Migrate custom templates to Arcane."""
        phase = "templates"
        if self._phase_status(phase) == "completed":
            self.ui.info("Templates already migrated -- skipping")
            return
        if not self._should_include("custom_templates"):
            self.ui.info("Custom Templates not selected -- skipping")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        templates = self.discovery.get("custom_templates", {}).get("data", [])

        for t in templates:
            tid = str(t.get("Id", ""))
            name = t.get("Title", tid)
            if self._is_migrated(phase, tid):
                self.report.record_skip("Custom Templates", name, "already migrated")
                continue
            try:
                file_resp = self.portainer.get_custom_template_file(t.get("Id", ""))
                file_content = file_resp.get("FileContent", "")
                payload = self._transform_custom_template(t, file_content)
                result = self._execute_or_log(
                    f"Create template '{name}'",
                    self.arcane.create_template,
                    payload,
                )
                target_id = result.get("id", "") if isinstance(result, dict) else ""
                self.report.record_success("Custom Templates", name, tid, target_id)
                if not self.config.dry_run:
                    self.report.add_rollback(
                        "DELETE",
                        f"{self.arcane.base_url}/templates/{target_id}",
                    f"Delete template '{name}'",
                )
                self._record_migrated(phase, tid)
            except Exception as exc:
                self.logger.error("Failed to migrate template %s: %s", name, exc)
                self.report.record_failure("Custom Templates", name, str(exc))
                self.ui.error(f"Template '{name}' failed: {exc}")

        self._mark_phase(phase, "completed")

    def _migrate_users(self):
        """Migrate users to Arcane, skipping duplicates."""
        phase = "users"
        if self._phase_status(phase) == "completed":
            self.ui.info("Users already migrated -- skipping")
            return
        if not self._should_include("users"):
            self.ui.info("Users not selected -- skipping")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        users = self.discovery.get("users", {}).get("data", [])

        # Get existing Arcane users to avoid duplicates
        try:
            existing = self.arcane.list_users() if not self.config.dry_run else []
        except Exception:
            existing = []
        existing_usernames = {
            u.get("username", "").lower() for u in existing
        }

        for user in users:
            uid = str(user.get("Id", ""))
            username = user.get("Username", uid)
            if self._is_migrated(phase, uid):
                self.report.record_skip("Users", username, "already migrated")
                continue
            if username.lower() in existing_usernames:
                self.report.record_skip("Users", username, "already exists in Arcane")
                self.ui.info(f"User '{username}' already exists -- skipped")
                self._record_migrated(phase, uid)
                continue
            try:
                payload = self._transform_user(user)
                result = self._execute_or_log(
                    f"Create user '{username}'",
                    self.arcane.create_user,
                    payload,
                )
                target_id = result.get("id", "") if isinstance(result, dict) else ""
                self.report.record_success("Users", username, uid, target_id)
                if not self.config.dry_run:
                    self.report.add_rollback(
                        "DELETE",
                        f"{self.arcane.base_url}/users/{target_id}",
                        f"Delete user '{username}'",
                    )
                self.report.add_action_item(
                    f"User '{username}' was created with default password 'ChangeMe123!' -- must be changed"
                )
                self._record_migrated(phase, uid)
            except Exception as exc:
                self.logger.error("Failed to migrate user %s: %s", username, exc)
                self.report.record_failure("Users", username, str(exc))
                self.ui.error(f"User '{username}' failed: {exc}")

        self._mark_phase(phase, "completed")

    def _migrate_webhooks(self):
        """Handle webhooks -- requires manual target mapping."""
        phase = "webhooks"
        if self._phase_status(phase) == "completed":
            self.ui.info("Webhooks already handled -- skipping")
            return
        if not self._should_include("webhooks"):
            self.ui.info("Webhooks not selected -- skipping")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        webhooks = self.discovery.get("webhooks", {}).get("data", [])

        for wh in webhooks:
            wh_id = str(wh.get("Id", ""))
            name = f"webhook-{wh_id}"
            self.report.record_skip(
                "Webhooks", name, "needs manual target mapping"
            )
            self.ui.warning(
                f"Webhook '{name}' skipped -- needs manual target mapping"
            )

        self._mark_phase(phase, "completed")

    def _export_ee_rbac(self):
        """Export EE RBAC data (teams, memberships, roles, resource controls)."""
        phase = "ee_rbac_export"
        if self._phase_status(phase) == "completed":
            self.ui.info("EE RBAC export already completed -- skipping")
            return
        if not self._is_ee():
            self.ui.info("Not EE edition -- skipping RBAC export")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        base = Path(self.config.backup_dir) / "ee_reference"

        def _write(filename: str, data: Any, label: str):
            filepath = base / filename
            if not self.config.dry_run:
                filepath.parent.mkdir(parents=True, exist_ok=True)
                with open(filepath, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, default=str)
            else:
                self.ui.dry_run_msg(f"Write {filepath}")
            count = len(data) if isinstance(data, list) else 1
            self.report.record_ee_export(label, count, str(filepath))

        teams = self.discovery.get("teams", {}).get("data", [])
        _write("teams.json", teams, "Teams")

        try:
            memberships = self.portainer.list_team_memberships()
        except Exception:
            memberships = []
        _write("team_memberships.json", memberships, "Team Memberships")

        roles = self.discovery.get("roles", {}).get("data", [])
        _write("roles.json", roles, "Roles")

        try:
            resource_controls = self.portainer.list_resource_controls()
        except Exception:
            resource_controls = []
        _write("resource_controls.json", resource_controls, "Resource Controls")

        self._mark_phase(phase, "completed")
        self.ui.success("EE RBAC reference exported")

    def _export_ee_audit(self):
        """Export EE audit/activity logs (optional, version-dependent)."""
        phase = "ee_audit_export"
        if self._phase_status(phase) == "completed":
            self.ui.info("EE audit export already completed -- skipping")
            return
        if not self._is_ee():
            self.ui.info("Not EE edition -- skipping audit export")
            self._mark_phase(phase, "completed")
            return
        if not self._should_include("activity_logs"):
            self.ui.info("Activity Logs not selected -- skipping audit export")
            self._mark_phase(phase, "completed")
            return

        self._mark_phase(phase, "in_progress")
        self.report.record_skip(
            "Activity Logs",
            "audit_logs",
            "Audit API varies by EE version -- skipped",
        )
        self.ui.info("EE audit export skipped (API varies by EE version)")
        self._mark_phase(phase, "completed")

    # ------------------------------------------------------------------
    # Task 12: Pre-flight checks
    # ------------------------------------------------------------------

    def preflight_checks(self) -> list:
        """Run pre-flight checks before live migration.

        Returns a list of dicts with keys: name, status (pass|warn|fail), details.
        """
        results: List[Dict[str, str]] = []

        # 1. Arcane API health
        try:
            self.arcane.health_check()
            results.append({
                "name": "Arcane API Health",
                "status": "pass",
                "details": "Arcane API is reachable and healthy",
            })
        except Exception as exc:
            results.append({
                "name": "Arcane API Health",
                "status": "fail",
                "details": f"Arcane API health check failed: {exc}",
            })

        # 2. Arcane environment accessible
        try:
            eid = self.config.arcane_environment_id or "0"
            self.arcane.get_environment(eid)
            results.append({
                "name": "Arcane Environment",
                "status": "pass",
                "details": f"Environment '{eid}' is accessible",
            })
        except Exception as exc:
            results.append({
                "name": "Arcane Environment",
                "status": "fail",
                "details": f"Cannot access Arcane environment: {exc}",
            })

        # 3. Naming conflicts
        try:
            eid = self.config.arcane_environment_id or "0"
            existing_projects = self.arcane.list_projects(eid)
            existing_names = {
                p.get("name", "").lower() for p in existing_projects
            }
            portainer_stacks = self.discovery.get("stacks", {}).get("data", [])
            conflicts = [
                s.get("Name", s.get("name", ""))
                for s in portainer_stacks
                if s.get("Name", s.get("name", "")).lower() in existing_names
            ]
            if conflicts:
                results.append({
                    "name": "Naming Conflicts",
                    "status": "warn",
                    "details": f"Conflicting stack names: {', '.join(conflicts)}",
                })
            else:
                results.append({
                    "name": "Naming Conflicts",
                    "status": "pass",
                    "details": "No naming conflicts detected",
                })
        except Exception as exc:
            results.append({
                "name": "Naming Conflicts",
                "status": "warn",
                "details": f"Could not check naming conflicts: {exc}",
            })

        # 4. Disk space
        try:
            backup_parent = Path(self.config.backup_dir).parent
            backup_parent.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(str(backup_parent))
            free_gb = usage.free / (1024 ** 3)
            if free_gb < 1.0:
                results.append({
                    "name": "Disk Space",
                    "status": "warn",
                    "details": f"Low disk space: {free_gb:.2f} GB free",
                })
            else:
                results.append({
                    "name": "Disk Space",
                    "status": "pass",
                    "details": f"{free_gb:.1f} GB free",
                })
        except Exception as exc:
            results.append({
                "name": "Disk Space",
                "status": "warn",
                "details": f"Could not check disk space: {exc}",
            })

        return results

    # ------------------------------------------------------------------
    # Task 12: Main orchestrator
    # ------------------------------------------------------------------

    def run(self, args=None):
        """Run the full migration wizard."""
        try:
            # ── Phase 0: Welcome ──────────────────────────────────
            self.ui.banner()

            # Check for existing checkpoint
            cp = self.config.checkpoint_file
            if os.path.isfile(cp):
                if args and getattr(args, "resume", False):
                    resume = True
                else:
                    resume = Confirm.ask(
                        "[yellow]Existing checkpoint found.[/] Resume previous migration?",
                        default=True,
                    )
                if not resume:
                    os.remove(cp)
                    self.state = self._load_state()
                    self.ui.info("Starting fresh migration")
                else:
                    self.ui.info("Resuming from checkpoint")

            # ── Phase 1: Connection Setup ─────────────────────────
            self.ui.phase_header("1", 7, "Connection Setup")

            # Portainer connection
            self.ui.ask_portainer_connection()
            self.portainer = PortainerClient(self.config, self.logger)

            try:
                info = self.portainer.test_connection()
                self.ui.success(f"Connected to Portainer at {self.config.portainer_url}")
            except Exception as exc:
                self.ui.error(f"Cannot connect to Portainer: {exc}")
                return

            try:
                edition_info = self.portainer.detect_edition()
                self.ui.show_edition_panel(
                    self.config.portainer_edition,
                    self.config.portainer_version,
                )
                self.ui.success(
                    f"Portainer {self.config.portainer_edition} "
                    f"v{self.config.portainer_version} detected"
                )
            except Exception as exc:
                self.ui.error(f"Cannot detect Portainer edition: {exc}")
                return

            # Mark EE-only phases as completed on CE so all-done check works
            if not self._is_ee():
                for phase in ["ee_rbac_export", "ee_audit_export"]:
                    if self._phase_status(phase) == "pending":
                        self._mark_phase(phase, "completed")

            try:
                endpoints = self.portainer.list_endpoints()
            except Exception as exc:
                self.ui.error(f"Cannot list Portainer endpoints: {exc}")
                return
            if not endpoints:
                self.ui.error("No Portainer endpoints found. Cannot continue.")
                return

            self.config.portainer_endpoint_id = self.ui.select_endpoint(endpoints)
            self.portainer = PortainerClient(self.config, self.logger)

            # Arcane connection
            self.ui.ask_arcane_connection()
            self.arcane = ArcaneClient(self.config, self.logger)

            if self.config.arcane_username and self.config.arcane_password:
                try:
                    self.arcane.login(
                        self.config.arcane_username,
                        self.config.arcane_password,
                    )
                    self.ui.success("Authenticated with Arcane")
                except Exception as exc:
                    self.ui.error(f"Arcane login failed: {exc}")
                    return

            try:
                version_info = self.arcane.get_version()
                version_str = (
                    version_info
                    if isinstance(version_info, str)
                    else version_info.get("version", "unknown")
                    if isinstance(version_info, dict)
                    else str(version_info)
                )
                self.ui.success(f"Connected to Arcane v{version_str}")
            except Exception as exc:
                self.ui.error(f"Cannot connect to Arcane: {exc}")
                return

            # Select Arcane environment
            try:
                arcane_envs = self.arcane.list_environments()
                self.config.arcane_environment_id = self.ui.select_arcane_environment(
                    arcane_envs
                )
            except Exception as exc:
                self.logger.debug("Could not list Arcane environments: %s", exc)
                self.config.arcane_environment_id = "0"
                self.ui.info("Using default Arcane environment (0)")

            # Recompute config_hash now that connection details are known
            self.state["config_hash"] = self.config.config_hash()
            self._save_state()

            # ── Phase 1.5: Portainer Backup ───────────────────────
            skip_backup = args and getattr(args, "skip_backup", False)
            if not skip_backup:
                self.ui.phase_header("1.5", 7, "Portainer Backup")
                do_backup = Confirm.ask(
                    "Create a Portainer backup before proceeding?",
                    default=True,
                )
                if do_backup:
                    try:
                        self._portainer_backup()
                    except Exception as exc:
                        self.ui.error(f"Portainer backup failed: {exc}")
                        if not Confirm.ask("Continue without backup?", default=False):
                            return
                else:
                    self.ui.warning("Skipping Portainer backup (user choice)")
                    self._mark_phase("portainer_backup", "completed")

            # ── Phase 2: Discovery ────────────────────────────────
            self.discovery = self.discover()

            # ── Phase 3: Strategy Selection ───────────────────────
            self.ui.phase_header(3, 7, "Strategy Selection")
            self.ui.ask_strategy()

            # Enforce --export-only regardless of user selection
            if args and getattr(args, "export_only", False):
                self.config.strategy = "export"
                self.ui.info("--export-only flag set: forcing export strategy")

            selected = self.ui.ask_scope_confirmation(
                self.discovery,
                self.config.portainer_edition,
            )
            if not selected:
                self.ui.warning("No items selected for migration. Exiting.")
                return
            self.ui.show_migration_plan(self.config)

            # ── Phase 4: Pre-Flight Checks ────────────────────────
            if self.config.strategy == "live":
                self.ui.phase_header(4, 7, "Pre-Flight Checks")
                results = self.preflight_checks()
                if not self.ui.show_preflight_results(results):
                    return

            # ── Phase 5: Execution ────────────────────────────────
            self.ui.phase_header(5, 7, "Execution")

            # Always export first
            self._export_to_disk()

            if self.config.strategy == "live":
                # Build phase list dynamically
                core_phases = [
                    ("5a: Registries", self._migrate_registries),
                    ("5b: Git Repos", self._migrate_git_repos),
                    ("5c: Networks", self._migrate_networks),
                    ("5d: Volumes", self._migrate_volumes),
                    ("5e: Stacks", self._migrate_stacks),
                    ("5f: GitOps Syncs", self._migrate_gitops_syncs),
                    ("5g: Containers", self._migrate_containers),
                    ("5h: Templates", self._migrate_templates),
                    ("5i: Users", self._migrate_users),
                ]
                ee_phases: list = []
                if self._is_ee():
                    ee_phases = [
                        ("5j: Webhooks", self._migrate_webhooks),
                        ("5k: EE RBAC Export", self._export_ee_rbac),
                        ("5l: EE Audit Export", self._export_ee_audit),
                    ]
                else:
                    ee_phases = [
                        ("5j: Webhooks", self._migrate_webhooks),
                    ]
                phases = core_phases + ee_phases

                with self.ui.create_progress() as progress:
                    task = progress.add_task(
                        "Migrating...", total=len(phases)
                    )
                    for label, fn in phases:
                        progress.update(task, description=f"[cyan]{label}[/]")
                        try:
                            fn()
                        except Exception as exc:
                            self.ui.error(f"{label} failed: {exc}")
                            self.logger.exception("Phase %s failed", label)
                            self._save_state()
                            if not Confirm.ask(
                                "Continue with remaining phases?",
                                default=True,
                            ):
                                break
                        progress.advance(task)

            # ── Phase 6: Verification & Report ────────────────────
            self.ui.phase_header(6, 7, "Verification & Report")
            report_file = self.report.save_report()
            self.report.report["report_file"] = report_file
            rollback_file = self.report.save_rollback_script()
            self.ui.show_final_report(
                self.report.report, self.config.portainer_edition
            )
            if rollback_file:
                self.ui.info(f"Rollback script: {rollback_file}")

            # Check if all phases completed
            all_done = all(
                status == "completed"
                for status in self.state.get("phases", {}).values()
            )
            if all_done:
                # Clean up checkpoint file
                if os.path.isfile(self.config.checkpoint_file):
                    os.remove(self.config.checkpoint_file)
                self.ui.success("Migration completed!")
            else:
                self.ui.warning(
                    "Some phases incomplete. Run with --resume to continue."
                )

        except KeyboardInterrupt:
            self._save_state()
            self.ui.warning(
                "Migration interrupted. Run with --resume to continue."
            )
        except Exception as exc:
            self.logger.exception("Unhandled error in migration engine")
            self.ui.error(f"Migration failed: {exc}")
            self._save_state()


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="migrate",
        description="Portainer-to-Arcane Migration Tool",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previously interrupted migration",
    )
    parser.add_argument(
        "--import-dir",
        metavar="PATH",
        help="Import from a previously exported migration directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the migration without making changes",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Only export from Portainer; do not import into Arcane",
    )
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="Skip the Portainer backup step",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="Path to a JSON configuration file",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args()


def setup_logging(config: Config) -> logging.Logger:
    """Configure file (DEBUG) + console (INFO) logging. Returns the logger."""
    logger = logging.getLogger("migrate")
    logger.setLevel(logging.DEBUG)

    # Rich console handler -- INFO and above
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
    )
    rich_handler.setLevel(logging.INFO)
    logger.addHandler(rich_handler)

    # File handler -- DEBUG and above (if log_file is set)
    if config.log_file:
        fh = logging.FileHandler(config.log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()
    config = Config()
    config.detect_platform()

    if args.dry_run:
        config.dry_run = True
    if args.export_only:
        config.strategy = "export"
    if args.config:
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
            for key, value in cfg_data.items():
                if hasattr(config, key):
                    setattr(config, key, value)
        except Exception as e:
            console.print(f"[red]Error loading config: {e}[/]")
            sys.exit(1)

    logger = setup_logging(config)
    logger.info(f"Migration tool v{__version__} starting on {config.platform_name}")

    try:
        engine = MigrationEngine(config, logger)
        engine.run(args=args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Migration interrupted. Run with --resume to continue.[/]")
        sys.exit(130)
    except Exception as e:
        logger.exception("Unhandled error")
        console.print(f"\n[red]Fatal error: {e}[/]")
        console.print(f"[dim]Check log file for details[/]")
        sys.exit(1)
