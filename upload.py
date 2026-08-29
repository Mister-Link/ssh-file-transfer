#!/usr/bin/env python3
"""
Fast file/folder uploader for remote hosts (Vast.ai / TensorDock)
Uses parallel transfers for speed and reads connection info from SSH config
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from common import SSHConfig, diagnose_proxycommand_failure, resolve_ssh_connection_candidates


class FileUploader:
    """Fast parallel file uploader using rsync over SSH"""

    host: str
    port: str
    user: str
    identity: str
    remote_path: str
    max_workers: int
    proxycommand: str | None
    ssh_target: str | None

    def __init__(
        self,
        host: str,
        port: str,
        user: str,
        identity: str,
        remote_path: str = "/home/user/",
        max_workers: int = 4,
        proxycommand: str | None = None,
        ssh_target: str | None = None,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.identity = identity
        self.remote_path = remote_path
        self.max_workers = max_workers
        self.proxycommand = self._normalize_proxycommand(proxycommand)
        self.ssh_target = ssh_target

    def _normalize_proxycommand(self, proxycommand: str | None) -> str | None:
        if not proxycommand:
            return None
        if not proxycommand.startswith("ssh "):
            return proxycommand
        if "ConnectTimeout=" in proxycommand:
            return proxycommand
        return proxycommand.replace(
            "ssh ",
            "ssh -o ConnectTimeout=6 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 ",
            1,
        )

    def _build_ssh_args(self) -> str:
        """Build SSH arguments for rsync"""
        ssh_bin = shutil.which("hpnssh")
        if not ssh_bin:
            raise RuntimeError("hpnssh not found; install HPN-SSH to upload.")
        ssh_args = f"{ssh_bin}"
        if self.ssh_target:
            ssh_args += f" -F {shlex.quote(str(Path('~/.ssh/config').expanduser()))}"
        else:
            ssh_args += f" -p {self.port}"
            if self.identity:
                ssh_args += f" -i {self.identity}"
        ssh_args += (
            " -o StrictHostKeyChecking=no -o BatchMode=yes"
            " -o ClearAllForwardings=yes"
            " -o Compression=no -o Ciphers=aes128-gcm@openssh.com,chacha20-poly1305@openssh.com"
        )
        if self.proxycommand:
            ssh_args += f" -o 'ProxyCommand={self.proxycommand}'"
        return ssh_args

    def probe_connection(self) -> None:
        """Verify that this SSH route accepts a command."""
        cmd = shlex.split(self._build_ssh_args()) + [self.ssh_target or f"{self.user}@{self.host}", "echo 1"]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30 if self.proxycommand else 10)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            diagnostic = self._proxy_diagnostic(stderr)
            if diagnostic:
                raise RuntimeError(f"{stderr}\n\nProxyCommand failed locally: {diagnostic}") from e
            raise RuntimeError(stderr or "SSH probe failed") from e

    def _proxy_diagnostic(self, stderr: str) -> str | None:
        if not self.proxycommand:
            return None
        if "UNKNOWN port 65535" not in stderr and "UNKNOWN-65535" not in stderr:
            return None
        return diagnose_proxycommand_failure(self.proxycommand)

    def _ensure_remote_rsync(self) -> None:
        """Ensure rsync is installed on the remote host."""
        if getattr(self, "_rsync_checked", False):
            return

        ssh_cmd = shlex.split(self._build_ssh_args())
        cmd = ssh_cmd + [
            self.ssh_target or f"{self.user}@{self.host}",
            "command -v rsync >/dev/null 2>&1 || "
            "(sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y rsync) || "
            "(apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y rsync)",
        ]

        try:
            subprocess.run(cmd, capture_output=True, check=True)
            self._rsync_checked = True
        except subprocess.CalledProcessError:
            # Fail gracefully, the actual rsync command will fail and show its error if rsync is still missing
            pass

    def upload_file(
        self, local_path: str, remote_subpath: str = ""
    ) -> tuple[bool, str]:
        """Upload a single file using rsync"""
        path_obj = Path(local_path)

        if not path_obj.exists():
            return False, f"File not found: {path_obj}"

        self._ensure_remote_rsync()

        # Construct remote path
        target = self.ssh_target or f"{self.user}@{self.host}"
        remote_dest = f"{target}:{self.remote_path}"
        if remote_subpath:
            remote_dest += f"{remote_subpath}/"

        # Build rsync command
        cmd = [
            "rsync",
            "-a",  # archive mode, no compression for speed on PNGs
            "--info=progress2",
            "--skip-compress=png,jpg,jpeg,webp,gif,mp4,mkv,zip,7z",
            "-e",
            self._build_ssh_args(),
            str(path_obj),
            remote_dest,
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            return True, f"✅ {path_obj.name}"
        except subprocess.CalledProcessError as e:
            return False, f"❌ {path_obj.name}: {e.stderr}"

    def upload_folder(
        self,
        local_folder: str,
        remote_subpath: str = "",
        exclude: list[str] | None = None,
    ) -> None:
        """Upload entire folder with parallel file transfers"""
        path_obj = Path(local_folder)

        if not path_obj.exists():
            print(f"❌ Folder not found: {path_obj}")
            return

        if not path_obj.is_dir():
            print(f"❌ Not a directory: {path_obj}")
            return

        self._ensure_remote_rsync()

        # Use rsync for the whole folder (faster than individual files)
        target = self.ssh_target or f"{self.user}@{self.host}"
        remote_dest = f"{target}:{self.remote_path}"
        if remote_subpath:
            remote_dest += f"{remote_subpath}/"

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
                f"{local_folder}/",  # trailing slash = contents only
                remote_dest,
            ]
        )

        print(
            f"📤 Uploading {path_obj.name}/ to {self.host}:{self.remote_path}{remote_subpath}"
        )
        print(f"   Command: {' '.join(cmd[:3])} ... {path_obj.name}/")

        try:
            _ = subprocess.run(cmd, check=True)
            print("✅ Upload complete!")
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
  %(prog)s myfile.txt --host vast        # Specify different SSH config host
        """,
    )

    _ = parser.add_argument("path", help="File or folder to upload")
    _ = parser.add_argument(
        "-r",
        "--remote",
        default="",
        help="Remote subdirectory (relative to /home/user/)",
    )
    _ = parser.add_argument(
        "--host", default="vast", help="SSH config host name (default: vast)"
    )
    _ = parser.add_argument(
        "-e",
        "--exclude",
        action="append",
        default=[],
        help="Exclude pattern (can be used multiple times)",
    )
    _ = parser.add_argument(
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

        connections = resolve_ssh_connection_candidates(host_info, 2222)
        user = host_info.get("user", "user")
        identity = host_info.get("identity", "")
        uploader: FileUploader | None = None
        hostname = ""
        port = "22"
        last_error: Exception | None = None

        for index, connection in enumerate(connections):
            hostname = connection["host"]
            port = connection["port"]
            proxycommand = connection["proxycommand"]
            ssh_target = connection["ssh_target"]

            if connection["used_vast_endpoint"]:
                print(
                    "ℹ️ Using Vast.ai mapped endpoint "
                    f"{hostname}:{port} for container port 2222"
                )
                if host_info.get("proxycommand"):
                    print("ℹ️ Bypassing configured jump host for direct Vast.ai endpoint")
            elif index > 0 and host_info.get("proxycommand"):
                print("ℹ️ Direct Vast.ai endpoint failed; falling back to SSH config route")

            candidate = FileUploader(
                host=hostname,
                port=str(port),
                user=str(user),
                identity=str(identity),
                remote_path=args.remote_base,
                proxycommand=proxycommand,
                ssh_target=ssh_target,
            )
            try:
                candidate.probe_connection()
                uploader = candidate
                break
            except Exception as e:
                last_error = e
                if index + 1 < len(connections):
                    print(f"⚠️ SSH route failed for {hostname}:{port}: {e}")
                    continue
                raise

        if uploader is None:
            if last_error is not None:
                raise last_error
            raise RuntimeError("No working SSH route found")

        print(f"🔗 Connecting to {user}@{hostname}:{port}")

    except Exception as e:
        print(f"❌ Error reading SSH config: {e}")
        print("\n💡 Make sure your SSH config has the correct host/port entry")
        sys.exit(1)

    # Create uploader
    if not hostname:
        print("❌ Hostname not found in SSH config")
        sys.exit(1)


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
