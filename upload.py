#!/usr/bin/env python3
"""
Fast file/folder uploader for remote hosts (Vast.ai / TensorDock)
Uses parallel transfers for speed and reads connection info from SSH config
"""

import argparse
import configparser
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple


class SSHConfig:
    """Parse SSH config to get connection details"""

    def __init__(self, config_path: str = "~/.ssh/config"):
        self.config_path = Path(config_path).expanduser()
        self.host_info = {}

    def get_host_info(self, host: str = "vast-ai") -> dict:
        """Extract host, port, user, and identity file from SSH config"""
        if not self.config_path.exists():
            raise FileNotFoundError(f"SSH config not found at {self.config_path}")

        current_host = None
        host_config = {}

        with open(self.config_path, "r") as f:
            for line in f:
                line = line.strip()

                if line.startswith("Host "):
                    if current_host == host and host_config:
                        return host_config
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

        if current_host == host and host_config:
            return host_config

        raise ValueError(f"Host '{host}' not found in SSH config")


def _load_vast_instance_for_host(hostname: str) -> Optional[dict]:
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
        instances = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    if not isinstance(instances, list):
        return None

    running = [inst for inst in instances if inst.get("actual_status") == "running"]
    for inst in running:
        if inst.get("public_ipaddr") == hostname:
            return inst

    if len(running) == 1:
        return running[0]

    return None


def _resolve_vast_port(hostname: str, container_port: int) -> Optional[str]:
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


class FileUploader:
    """Fast parallel file uploader using rsync over SSH"""

    def __init__(
        self,
        host: str,
        port: str,
        user: str,
        identity: str,
        remote_path: str = "/home/user/",
        max_workers: int = 4,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.identity = identity
        self.remote_path = remote_path
        self.max_workers = max_workers

    def _build_ssh_args(self) -> str:
        """Build SSH arguments for rsync"""
        ssh_bin = shutil.which("hpnssh")
        if not ssh_bin:
            raise RuntimeError("hpnssh not found; install HPN-SSH to upload.")
        ssh_args = f"{ssh_bin} -p {self.port}"
        if self.identity:
            ssh_args += f" -i {self.identity}"
        ssh_args += (
            " -o StrictHostKeyChecking=no -o BatchMode=yes"
            " -o Compression=no -o Ciphers=aes128-gcm@openssh.com,chacha20-poly1305@openssh.com"
        )
        return ssh_args

    def upload_file(
        self, local_path: str, remote_subpath: str = ""
    ) -> Tuple[bool, str]:
        """Upload a single file using rsync"""
        local_path = Path(local_path)

        if not local_path.exists():
            return False, f"File not found: {local_path}"

        # Construct remote path
        remote_dest = f"{self.user}@{self.host}:{self.remote_path}"
        if remote_subpath:
            remote_dest += remote_subpath + "/"

        # Build rsync command
        cmd = [
            "rsync",
            "-a",  # archive mode, no compression for speed on PNGs
            "--info=progress2",
            "--skip-compress=png,jpg,jpeg,webp,gif,mp4,mkv,zip,7z",
            "-e",
            self._build_ssh_args(),
            str(local_path),
            remote_dest,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, f"✅ {local_path.name}"
        except subprocess.CalledProcessError as e:
            return False, f"❌ {local_path.name}: {e.stderr}"

    def upload_folder(
        self,
        local_folder: str,
        remote_subpath: str = "",
        exclude: Optional[List[str]] = None,
    ) -> None:
        """Upload entire folder with parallel file transfers"""
        local_folder = Path(local_folder)

        if not local_folder.exists():
            print(f"❌ Folder not found: {local_folder}")
            return

        if not local_folder.is_dir():
            print(f"❌ Not a directory: {local_folder}")
            return

        # Use rsync for the whole folder (faster than individual files)
        remote_dest = f"{self.user}@{self.host}:{self.remote_path}"
        if remote_subpath:
            remote_dest += remote_subpath + "/"

        cmd = [
            "rsync",
            "-av",  # archive, verbose, no compression for speed on PNGs
            "--info=progress2",
            "--skip-compress=png,jpg,jpeg,webp,gif,mp4,mkv,zip,7z",
            "-e",
            self._build_ssh_args(),
        ]

        # Add exclusions
        if exclude:
            for pattern in exclude:
                cmd.extend(["--exclude", pattern])

        cmd.extend(
            [
                str(local_folder) + "/",  # trailing slash = contents only
                remote_dest,
            ]
        )

        print(
            f"📤 Uploading {local_folder.name}/ to {self.host}:{self.remote_path}{remote_subpath}"
        )
        print(f"   Command: {' '.join(cmd[:3])} ... {local_folder.name}/")

        try:
            subprocess.run(cmd, check=True)
            print(f"✅ Upload complete!")
        except subprocess.CalledProcessError as e:
            print(f"❌ Upload failed: {e}")
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Fast file/folder uploader for remote hosts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s myfile.txt                    # Upload file to /home/user/
  %(prog)s myfolder/                     # Upload entire folder
  %(prog)s myfile.txt -r uploads/        # Upload to /home/user/uploads/
  %(prog)s . -r project/ -e node_modules -e .git  # Upload current dir, exclude patterns
  %(prog)s myfile.txt --host vast-ai     # Specify different SSH config host
        """,
    )

    parser.add_argument("path", help="File or folder to upload")
    parser.add_argument(
        "-r",
        "--remote",
        default="",
        help="Remote subdirectory (relative to /home/user/)",
    )
    parser.add_argument(
        "--host", default="vast-ai", help="SSH config host name (default: vast-ai)"
    )
    parser.add_argument(
        "-e",
        "--exclude",
        action="append",
        default=[],
        help="Exclude pattern (can be used multiple times)",
    )
    parser.add_argument(
        "--remote-base",
        default="/home/user/",
        help="Remote base path (default: /home/user/)",
    )

    args = parser.parse_args()

    if not shutil.which("hpnssh"):
        print("❌ hpnssh not found on PATH.")
        print("💡 Install HPN-SSH to use the uploader.")
        sys.exit(1)
    if not shutil.which("rsync"):
        print("❌ rsync not found on PATH.")
        print("💡 Install rsync to use the uploader.")
        sys.exit(1)

    # Get connection info from SSH config
    try:
        ssh_config = SSHConfig()
        host_info = ssh_config.get_host_info(args.host)

        hostname = host_info.get("hostname")
        port = host_info.get("port", "22")
        user = host_info.get("user", "user")
        identity = host_info.get("identity", "")

        if shutil.which("hpnssh"):
            mapped_port = _resolve_vast_port(hostname, 2222)
            if mapped_port:
                if mapped_port != str(port):
                    print(
                        f"ℹ️ Using Vast.ai mapped port {mapped_port} for container port 2222"
                    )
                port = mapped_port

        print(f"🔗 Connecting to {user}@{hostname}:{port}")

    except Exception as e:
        print(f"❌ Error reading SSH config: {e}")
        print("\n💡 Make sure your SSH config has the correct host/port entry")
        sys.exit(1)

    # Create uploader
    uploader = FileUploader(
        host=hostname,
        port=port,
        user=user,
        identity=identity,
        remote_path=args.remote_base,
    )

    # Upload
    local_path = Path(args.path)

    if local_path.is_file():
        success, msg = uploader.upload_file(str(local_path), args.remote)
        print(msg)
        sys.exit(0 if success else 1)
    elif local_path.is_dir():
        uploader.upload_folder(str(local_path), args.remote, exclude=args.exclude)
    else:
        print(f"❌ Path not found: {local_path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
