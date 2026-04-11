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
