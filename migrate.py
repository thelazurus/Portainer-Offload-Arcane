#!/usr/bin/env python3
"""
Portainer-to-Arcane Migration Tool

Migrates Docker resources (stacks, containers, volumes, networks, registries,
users, settings) from Portainer CE/EE to Arcane.

Usage:
    python migrate.py [options]

See --help for full option list.
"""

__version__ = "0.1.0"

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
from typing import Optional, List, Dict, Any, Callable  # noqa: E402

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

console = Console()

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
        self.base_url = config.arcane_url.rstrip("/")

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
        self.logger.debug("%s %s json=%s params=%s", method, url, json_data, params)
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
        self.config.arcane_refresh_token = data.get("refresh_token", "")
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
        return self._get("/registries")

    def create_registry(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/registries", json_data=data)

    # -- Git repos ---------------------------------------------------------

    def list_git_repos(self) -> List[Dict[str, Any]]:
        return self._get("/git-repos")

    def create_git_repo(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/git-repos", json_data=data)

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
        return self._post(f"/environments/{eid}/gitops-sync", json_data=data)

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
                f"/environments/{eid}/volumes/{name}/backup",
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
            table.add_row(rtype.replace("_", " ").title(), str(count), str(details), "CE+EE")

        # EE-only resources
        if edition == "EE":
            ee_types = [
                "webhooks", "teams", "team_memberships", "roles",
                "resource_controls", "edge_stacks",
            ]
            for rtype in ee_types:
                data = discovery.get(rtype, {})
                count = data.get("count", 0) if isinstance(data, dict) else len(data) if isinstance(data, list) else 0
                details = data.get("details", "") if isinstance(data, dict) else ""
                table.add_row(rtype.replace("_", " ").title(), str(count), str(details), "EE only")

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
        ]
        ee_types = [
            "webhooks", "teams", "team_memberships", "roles",
            "resource_controls", "edge_stacks",
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
            if count > 0 and Confirm.ask(
                f"    [blue]Migrate {rt.replace('_', ' ')}?[/blue] ({count} found)",
                default=True,
            ):
                self.config.selected_items[rt] = []

        if edition == "EE":
            self.console.print("\n  [bold]EE-only resources:[/bold]")
            for rt in ee_types:
                data = discovery.get(rt, {})
                count = data.get("count", 0) if isinstance(data, dict) else len(data) if isinstance(data, list) else 0
                if count > 0 and Confirm.ask(
                    f"    [blue]Migrate {rt.replace('_', ' ')}?[/blue] ({count} found)",
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
            status = r.get("status", "PASS")
            if status == "PASS":
                styled_status = "[green]PASS[/green]"
            elif status == "WARN":
                styled_status = "[yellow]WARN[/yellow]"
            else:
                styled_status = "[red]FAIL[/red]"
                has_fail = True
            table.add_row(r.get("check", ""), styled_status, r.get("details", ""))

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

        lines = [
            "#!/usr/bin/env bash",
            "# Rollback script -- generated by migrate.py",
            f"# Generated: {datetime.now(timezone.utc).isoformat()}",
            "# Review carefully before executing!",
            "",
            'set -euo pipefail',
            "",
        ]

        for cmd in reversed(self.rollback_commands):
            lines.append(f"# {cmd['description']}")
            lines.append(f"curl -X {cmd['method']} \"{cmd['url']}\"")
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
        self.state = self._load_state()
        self.discovery: Dict[str, Any] = {}
        self._git_repo_map: Dict[str, str] = {}  # stack_id -> arcane_repo_id

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
            discovery["Stacks"] = {
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
            discovery["Standalone Containers"] = {
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
            discovery["Images"] = {
                "count": len(images),
                "details": f"{size_gb:.1f} GB total",
                "edition": "CE + EE",
                "data": images,
            }
            progress.advance(task)

            # Volumes
            vol_data = self.portainer.list_volumes()
            volumes = vol_data.get("Volumes", []) or []
            discovery["Volumes"] = {
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
            discovery["Networks"] = {
                "count": len(user_networks),
                "details": f"({len(networks) - len(user_networks)} default excluded)",
                "edition": "CE + EE",
                "data": user_networks,
            }
            progress.advance(task)

            # Registries
            registries = self.portainer.list_registries()
            discovery["Registries"] = {
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
            discovery["Custom Templates"] = {
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
            discovery["Users"] = {
                "count": len(users),
                "details": ", ".join(u.get("Username", "") for u in users[:4]),
                "edition": "CE + EE",
                "data": users,
            }
            progress.advance(task)

            # --- EE-only resources ---
            if self._is_ee():
                webhooks = self.portainer.list_webhooks()
                discovery["Webhooks"] = {
                    "count": len(webhooks),
                    "details": "",
                    "edition": "EE",
                    "data": webhooks,
                }
                progress.advance(task)

                teams = self.portainer.list_teams()
                discovery["Teams"] = {
                    "count": len(teams),
                    "details": ", ".join(
                        t.get("Name", "") for t in teams[:3]
                    ),
                    "edition": "EE",
                    "data": teams,
                }
                progress.advance(task)

                roles = self.portainer.list_roles()
                discovery["Roles"] = {
                    "count": len(roles),
                    "details": "",
                    "edition": "EE",
                    "data": roles,
                }
                progress.advance(task)

                edge_stacks = self.portainer.list_edge_stacks()
                discovery["Edge Stacks"] = {
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
                    discovery["Webhooks"] = {
                        "count": len(webhooks),
                        "details": "",
                        "edition": "CE",
                        "data": webhooks,
                    }

            # Settings (always export as reference)
            settings = self.portainer.get_settings()
            discovery["Settings"] = {
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
        """
        port_type = reg.get("Type", 1)
        registry_type = self.REGISTRY_TYPE_MAP.get(port_type, "custom")

        result: Dict[str, Any] = {
            "url": reg.get("URL", ""),
            "username": reg.get("Username", ""),
            "token": reg.get("Password", ""),
            "description": reg.get("Name", ""),
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
                    "HostIp": binding.get("HostIp", ""),
                    "HostPort": binding.get("HostPort", ""),
                }
                for binding in (host_list or [])
            ]

        # --- Mounts: preserve Source:Destination:Mode ---
        binds = host_config.get("Binds", []) or []
        mounts: List[Dict[str, Any]] = []
        for mount in mounts_raw:
            mount_entry: Dict[str, Any] = {
                "Type": mount.get("Type", "volume"),
                "Source": mount.get("Source", ""),
                "Destination": mount.get("Destination", ""),
                "Mode": mount.get("Mode", ""),
                "RW": mount.get("RW", True),
            }
            if mount.get("Driver"):
                mount_entry["Driver"] = mount["Driver"]
            mounts.append(mount_entry)

        # --- Restart policy ---
        restart_policy = host_config.get("RestartPolicy", {}) or {}

        # --- Healthcheck ---
        healthcheck_raw = config.get("Healthcheck", {}) or {}
        healthcheck: Dict[str, Any] = {}
        if healthcheck_raw:
            healthcheck = {
                "Test": healthcheck_raw.get("Test", []) or [],
                "Interval": healthcheck_raw.get("Interval", 0),
                "Timeout": healthcheck_raw.get("Timeout", 0),
                "Retries": healthcheck_raw.get("Retries", 0),
                "StartPeriod": healthcheck_raw.get("StartPeriod", 0),
            }

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

        # --- Build the result ---
        result: Dict[str, Any] = {
            # From Config
            "Image": config.get("Image", ""),
            "Env": config.get("Env", []) or [],
            "Cmd": config.get("Cmd", []) or [],
            "Entrypoint": config.get("Entrypoint", []) or [],
            "Labels": config.get("Labels", {}) or {},
            "Hostname": config.get("Hostname", ""),
            "Domainname": config.get("Domainname", ""),
            "User": config.get("User", ""),
            "WorkingDir": config.get("WorkingDir", ""),
            "Tty": config.get("Tty", False),
            "OpenStdin": config.get("OpenStdin", False),
            "ExposedPorts": exposed_ports,
            # HostConfig
            "HostConfig": {
                "NetworkMode": host_config.get("NetworkMode", "default"),
                "PortBindings": port_bindings,
                "Binds": binds,
                "Memory": host_config.get("Memory", 0),
                "MemorySwap": host_config.get("MemorySwap", 0),
                "NanoCpus": host_config.get("NanoCpus", 0),
                "CpuShares": host_config.get("CpuShares", 0),
                "Privileged": host_config.get("Privileged", False),
                "CapAdd": host_config.get("CapAdd", []) or [],
                "CapDrop": host_config.get("CapDrop", []) or [],
                "SecurityOpt": host_config.get("SecurityOpt", []) or [],
                "ReadonlyRootfs": host_config.get("ReadonlyRootfs", False),
                "Devices": devices,
                "PidsLimit": host_config.get("PidsLimit", 0),
                "AutoRemove": host_config.get("AutoRemove", False),
                "Dns": dns,
                "DnsSearch": dns_search,
                "DnsOptions": dns_options,
                "RestartPolicy": {
                    "Name": restart_policy.get("Name", ""),
                    "MaximumRetryCount": restart_policy.get("MaximumRetryCount", 0),
                },
            },
            "Mounts": mounts,
        }

        # Add Healthcheck only if present
        if healthcheck:
            result["Healthcheck"] = healthcheck

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
    if args.dry_run:
        config.dry_run = True
    if args.export_only:
        config.strategy = "export"

    config.detect_platform()

    # Determine log file name
    config.log_file = f"migration_{config.platform_name}.log"

    logger = setup_logging(config)

    # Banner
    banner_text = Text()
    banner_text.append("Portainer ", style="bold cyan")
    banner_text.append("-> ", style="bold white")
    banner_text.append("Arcane ", style="bold green")
    banner_text.append("Migration Tool", style="bold white")
    banner_text.append(f"\nv{__version__}", style="dim")
    banner_text.append(f"  |  Platform: {config.platform_name}", style="dim")
    banner_text.append(f"  |  Docker: {'yes' if config.has_docker else 'no'}", style="dim")
    if config.dry_run:
        banner_text.append("\n[DRY RUN MODE]", style="bold yellow")

    console.print(
        Panel(
            banner_text,
            title="migrate.py",
            border_style="bright_blue",
            padding=(1, 2),
        )
    )

    logger.info("Migration tool initialized on %s", config.platform_name)
