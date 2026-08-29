#!/usr/bin/env python3
"""
Common functions for SSH, Vast.ai, and S3 integration.
"""

from __future__ import annotations

import json
import subprocess
import shlex


def diagnose_proxycommand_failure(proxycommand: str | None) -> str | None:
    """Run a local proxy command briefly to capture immediate setup failures."""
    if not proxycommand:
        return None

    try:
        result = subprocess.run(
            ["bash", "-lc", proxycommand],
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
        )
    except subprocess.TimeoutExpired:
        return None
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        return details or None
    except FileNotFoundError:
        return f"ProxyCommand not found: {proxycommand}"

    output = (result.stderr or result.stdout or "").strip()
    return output or None


from pathlib import Path
from typing import TypedDict, cast


class S3HostConfig(TypedDict, total=False):
    profile: str
    bucket: str
    prefix: str  # optional key prefix, e.g. "uploads/"
    endpoint_url: str  # optional, e.g. "https://s3.us-west-1.wasabisys.com"


def load_s3_hosts() -> dict[str, S3HostConfig]:
    """Load S3 host definitions from s3_hosts.json next to this file."""
    config_path = Path(__file__).parent / "s3_hosts.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def get_s3_host_names() -> list[str]:
    """Return the list of S3 host alias names."""
    return list(load_s3_hosts().keys())


def get_s3_host_config(name: str) -> S3HostConfig | None:
    """Return the S3HostConfig for a given host alias, or None."""
    return load_s3_hosts().get(name)


class VastPort(TypedDict):
    HostPort: int


class VastInstance(TypedDict):
    actual_status: str
    public_ipaddr: str
    ports: dict[str, list[VastPort]]


class ResolvedVastEndpoint(TypedDict):
    host: str
    port: str


class ResolvedSSHConnection(TypedDict):
    host: str
    port: str
    proxycommand: str | None
    ssh_target: str | None
    used_vast_endpoint: bool


def _should_use_config_route(host_info: dict[str, str]) -> bool:
    """Return True when SSH must follow the configured alias/proxy path."""
    proxycommand = host_info.get("proxycommand", "")
    alias = host_info.get("alias", "")
    hostname = host_info.get("hostname", "")

    if "vast-proxy.sh" in proxycommand:
        return True
    if proxycommand and alias and hostname == alias:
        return True
    return False


class SSHConfig:
    """Parse SSH config to get connection details"""

    config_path: Path
    host_info: dict[str, dict[str, str]]

    def __init__(self, config_path: str = "~/.ssh/config"):
        self.config_path = Path(config_path).expanduser()
        self.host_info = {}

    def get_host_info(self, host: str = "vast") -> dict[str, str]:
        """Extract host, port, user, and identity file from SSH config"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"SSH config not found at {self.config_path}")

        current_host: str | None = None
        host_config: dict[str, str] = {}

        with open(self.config_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()

                if line.startswith("Host "):
                    if current_host == host and host_config:
                        if "hostname" not in host_config:
                            host_config["hostname"] = host
                        host_config["alias"] = host
                        return self._resolve_proxy_jump(host_config)
                    current_host = line.split()[1]
                    host_config = {}

                elif current_host == host:
                    if line.startswith("HostName "):
                        host_config["hostname"] = line.split()[1]
                    elif line.startswith("Port "):
                        host_config["port"] = line.split()[1]
                    elif line.startswith("User "):
                        host_config["user"] = line.split()[1]
                    elif line.startswith("IdentityFile "):
                        identity = line.split()[1]
                        host_config["identity"] = str(Path(identity).expanduser())
                    elif line.startswith("ProxyJump "):
                        host_config["proxyjump"] = line.split()[1]
                    elif line.startswith("ProxyCommand "):
                        # Extract full ProxyCommand (can have multiple words)
                        host_config["proxycommand"] = line[
                            len("ProxyCommand ") :
                        ].strip()

        if current_host == host and host_config:
            if "port" not in host_config:
                host_config["port"] = "22"
            if "hostname" not in host_config:
                host_config["hostname"] = host
            host_config["alias"] = host
            return self._resolve_proxy_jump(host_config)

        raise ValueError(f"Host '{host}' not found in SSH config")

    def _resolve_proxy_jump(self, host_config: dict[str, str]) -> dict[str, str]:
        """Resolve ProxyJump to ProxyCommand if present"""
        # Ensure port is set (default to 22)
        if "port" not in host_config:
            host_config["port"] = "22"

        if "proxyjump" in host_config and "proxycommand" not in host_config:
            # Get jump host info
            jump_host = host_config["proxyjump"]
            jump_info = self.get_host_info(jump_host)

            # Build ProxyCommand from jump host
            ssh_cmd = "ssh"
            if "port" in jump_info:
                ssh_cmd += f" -p {jump_info['port']}"
            if "identity" in jump_info:
                ssh_cmd += f" -i {jump_info['identity']}"
            if "user" in jump_info and "hostname" in jump_info:
                ssh_cmd += f" {jump_info['user']}@{jump_info['hostname']}"
            elif "hostname" in jump_info:
                ssh_cmd += f" {jump_info['hostname']}"

            ssh_cmd += " -W %h:%p"
            host_config["proxycommand"] = ssh_cmd

        return host_config

    def list_hosts(self) -> list[str]:
        """List host aliases from SSH config"""
        if not self.config_path.exists():
            return []
        hosts: list[str] = []
        with open(self.config_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("Host "):
                    parts = line.split()
                    if len(parts) >= 2:
                        name = parts[1]
                        if name.startswith("git") or name.startswith("github"):
                            continue
                        hosts.append(name)
        return hosts


def _load_vast_instance_for_host(hostname: str) -> VastInstance | None:
    try:
        result = subprocess.run(
            ["vastai", "show", "instances", "--raw"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    try:
        instances = cast(list[VastInstance], json.loads(result.stdout))
    except json.JSONDecodeError:
        return None

    running = [inst for inst in instances if inst.get("actual_status") == "running"]
    for inst in running:
        if inst.get("public_ipaddr") == hostname:
            return inst

    if len(running) == 1:
        return running[0]

    return None


def _resolve_vast_port(hostname: str, container_port: int) -> str | None:
    inst = _load_vast_instance_for_host(hostname)
    if not inst:
        return None

    ports = inst.get("ports", {})
    key = f"{container_port}/tcp"
    entries = ports.get(key) or []
    if not entries:
        return None

    host_port = entries[0].get("HostPort")
    if not host_port:
        return None

    return str(host_port)


def resolve_vast_endpoint(
    hostname: str, container_port: int = 2222
) -> ResolvedVastEndpoint | None:
    """Resolve a Vast.ai SSH endpoint to public_ip:host_port for a container port."""
    inst = _load_vast_instance_for_host(hostname)
    if not inst:
        return None

    host = inst.get("public_ipaddr")
    if not host:
        return None

    ports = inst.get("ports", {})
    key = f"{container_port}/tcp"
    entries = ports.get(key) or []
    if not entries:
        return None

    host_port = entries[0].get("HostPort")
    if not host_port:
        return None

    return {"host": host, "port": str(host_port)}


def resolve_ssh_connection_candidates(
    host_info: dict[str, str], container_port: int = 2222
) -> list[ResolvedSSHConnection]:
    """Resolve the SSH route for a host."""
    original = {
        "host": host_info["hostname"],
        "port": host_info.get("port", "22"),
        "proxycommand": host_info.get("proxycommand"),
        "ssh_target": host_info.get("alias") or host_info["hostname"],
        "used_vast_endpoint": False,
    }

    if host_info.get("alias") in {"vast", "vast-ai"}:
        # Use the configured `ssh vast` route exactly. Vast.ai can report a
        # private or stale forwarded address that is not reachable locally.
        return [original]

    candidates: list[ResolvedSSHConnection] = [original]
    if not _should_use_config_route(host_info):
        endpoint = resolve_vast_endpoint(original["host"], container_port)
        if endpoint:
            mapped = {
                "host": endpoint["host"],
                "port": endpoint["port"],
                "proxycommand": None,
                "ssh_target": None,
                "used_vast_endpoint": True,
            }
            if (
                mapped["host"] != original["host"]
                or mapped["port"] != original["port"]
            ):
                candidates.insert(0, mapped)

    unique: list[ResolvedSSHConnection] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for candidate in candidates:
        key = (
            candidate["host"],
            candidate["port"],
            candidate["proxycommand"],
            candidate["ssh_target"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def resolve_ssh_connection(
    host_info: dict[str, str], container_port: int = 2222
) -> ResolvedSSHConnection:
    """Resolve the preferred SSH route for a host."""
    return resolve_ssh_connection_candidates(host_info, container_port)[0]
