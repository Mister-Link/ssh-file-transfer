#!/usr/bin/env python3
"""
Common functions for SSH and Vast.ai integration.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TypedDict, cast


class VastPort(TypedDict):
    HostPort: int


class VastInstance(TypedDict):
    actual_status: str
    public_ipaddr: str
    ports: dict[str, list[VastPort]]


class SSHConfig:
    """Parse SSH config to get connection details"""

    config_path: Path
    host_info: dict[str, dict[str, str]]

    def __init__(self, config_path: str = "~/.ssh/config"):
        self.config_path = Path(config_path).expanduser()
        self.host_info = {}

    def get_host_info(self, host: str = "vast-ai") -> dict[str, str]:
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
            return self._resolve_proxy_jump(host_config)

        raise ValueError(f"Host '{host}' not found in SSH config")

    def _resolve_proxy_jump(self, host_config: dict[str, str]) -> dict[str, str]:
        """Resolve ProxyJump to ProxyCommand if present"""
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
