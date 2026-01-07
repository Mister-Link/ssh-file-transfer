#!/usr/bin/env python3
"""
Fast File Uploader (PySide6)
Qt-based UI with drag-and-drop uploads and remote file browser.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from PySide6 import QtCore, QtGui, QtWidgets


class SSHConfig:
    """Parse SSH config to get connection details"""

    def __init__(self, config_path: str = "~/.ssh/config"):
        self.config_path = Path(config_path).expanduser()

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

    def list_hosts(self) -> List[str]:
        """List host aliases from SSH config"""
        if not self.config_path.exists():
            return []
        hosts = []
        with open(self.config_path, "r") as f:
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


class RemoteFileSystem:
    """Handle remote filesystem operations via SSH"""

    def __init__(self, host: str, port: str, user: str, identity: str):
        self.host = host
        self.port = port
        self.user = user
        self.identity = identity
        self.control_path = str(
            Path(tempfile.gettempdir()) / f"uploader-ssh-{os.getpid()}"
        )
        self.cache = {}

    def _run_ssh_command(self, command: str) -> str:
        """Execute command on remote host"""
        ssh_bin = shutil.which("hpnssh")
        if not ssh_bin:
            raise Exception("hpnssh not found; install HPN-SSH to use the uploader.")
        ssh_cmd = [
            ssh_bin,
            "-p",
            self.port,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "BatchMode=yes",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self.control_path}",
            "-o",
            "ControlPersist=60",
        ]

        if self.identity:
            ssh_cmd.extend(["-i", self.identity])

        ssh_cmd.extend([f"{self.user}@{self.host}", command])

        try:
            result = subprocess.run(
                ssh_cmd, capture_output=True, text=True, check=True, timeout=10
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise Exception(f"SSH command failed: {e.stderr}")

    def list_directory(self, path: str) -> List[dict]:
        """List files and directories at path"""
        if path in self.cache:
            return self.cache[path]
        command = (
            'cd "{path}" 2>/dev/null && '
            "TZ=UTC ls -lA --time-style=+%Y-%m-%dT%H:%M:%S "
            '--group-directories-first 2>/dev/null || echo "ERROR"'
        ).format(path=path)
        output = self._run_ssh_command(command)

        if output.strip() == "ERROR":
            return []

        items = []
        for line in output.strip().split("\n"):
            if not line or line.startswith("total"):
                continue

            parts = line.split(None, 6)
            if len(parts) < 7:
                continue

            perms = parts[0]
            raw_name = parts[6]
            mtime = parts[5]

            if raw_name in [".", ".."]:
                continue

            is_dir = perms.startswith("d")
            is_link = perms.startswith("l")
            link_target = None
            display_name = raw_name

            if is_link and " -> " in raw_name:
                link_name, link_target = raw_name.split(" -> ", 1)
                raw_name = link_name
                display_name = f"{link_name} -> {link_target}"

            items.append(
                {
                    "name": raw_name,
                    "display_name": display_name,
                    "is_dir": is_dir,
                    "is_link": is_link,
                    "link_target": link_target,
                    "perms": perms,
                    "size": parts[4],
                    "mtime": mtime,
                }
            )

        self.cache[path] = items
        return items

    def clear_cache(self, path: Optional[str] = None):
        """Clear directory cache"""
        if path:
            self.cache.pop(path, None)
        else:
            self.cache.clear()

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Rename/move a file or folder"""
        cmd = f"mv {shlex.quote(old_path)} {shlex.quote(new_path)}"
        self._run_ssh_command(cmd)

    def delete_path(self, target_path: str) -> None:
        """Delete file or folder recursively"""
        cmd = f"rm -rf {shlex.quote(target_path)}"
        self._run_ssh_command(cmd)


class FileUploader:
    """Handle file uploads using rsync"""

    def __init__(self, host: str, port: str, user: str, identity: str):
        self.host = host
        self.port = port
        self.user = user
        self.identity = identity

    def _build_ssh_args(self) -> str:
        """Build SSH arguments for rsync"""
        ssh_bin = shutil.which("hpnssh")
        if not ssh_bin:
            raise RuntimeError("hpnssh not found; install HPN-SSH to use the uploader.")
        ssh_args = f"{ssh_bin} -p {self.port}"
        if self.identity:
            ssh_args += f" -i {self.identity}"
        ssh_args += (
            " -o StrictHostKeyChecking=no -o BatchMode=yes"
            " -o Compression=no -o Ciphers=aes128-gcm@openssh.com,chacha20-poly1305@openssh.com"
        )
        return ssh_args

    def upload(self, local_path: str, remote_path: str, progress_callback=None) -> bool:
        """Upload file or folder to remote path"""
        local_path = Path(local_path)

        if not local_path.exists():
            if progress_callback:
                progress_callback(False, f"Not found: {local_path}")
            return False

        remote_base = remote_path.rstrip("/")
        remote_dest = f"{self.user}@{self.host}:{remote_base}"

        cmd = [
            "rsync",
            "-av",
            "--info=progress2",
            "--skip-compress=png,jpg,jpeg,webp,gif,mp4,mkv,zip,7z",
            "-e",
            self._build_ssh_args(),
        ]

        if local_path.is_dir():
            local_name = local_path.name
            remote_name = Path(remote_base).name
            if remote_name == local_name:
                remote_parent = str(Path(remote_base).parent).rstrip("/")
                local_str = str(local_path)
                remote_dest = f"{self.user}@{self.host}:{remote_parent}/"
            else:
                local_str = f"{local_path}/"
                remote_dest = f"{self.user}@{self.host}:{remote_base}/"
        else:
            local_str = str(local_path)
        cmd.extend([local_str, remote_dest])

        try:
            if progress_callback:
                progress_callback(None, f"Uploading {local_path.name}...")

            subprocess.run(cmd, capture_output=True, text=True, check=True)

            if progress_callback:
                progress_callback(True, f"✅ {local_path.name}")

            return True

        except subprocess.CalledProcessError as e:
            err = e.stderr or ""
            if progress_callback:
                progress_callback(False, f"❌ {local_path.name}: {err}")
            return False

    def download(
        self, remote_path: str, local_dest: str, is_dir: bool, progress_callback=None
    ) -> bool:
        """Download file or folder from remote"""
        local_dest_path = Path(local_dest)
        local_dest_path.parent.mkdir(parents=True, exist_ok=True)

        source = f"{self.user}@{self.host}:{remote_path}"
        cmd = [
            "rsync",
            "-av",
            "--info=progress2",
            "--skip-compress=png,jpg,jpeg,webp,gif,mp4,mkv,zip,7z",
            "-e",
            self._build_ssh_args(),
            source,
            str(local_dest_path),
        ]

        try:
            if progress_callback:
                progress_callback(None, f"Downloading {Path(remote_path).name}...")
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            if progress_callback:
                progress_callback(True, f"✅ Downloaded {Path(remote_path).name}")
            return True
        except subprocess.CalledProcessError as e:
            err = e.stderr or ""
            if progress_callback:
                progress_callback(False, f"❌ Download failed: {err}")
            return False


class UploadWorker(QtCore.QThread):
    progress = QtCore.Signal(str)
    finished = QtCore.Signal(bool)

    def __init__(self, uploader: FileUploader, local_path: str, remote_path: str):
        super().__init__()
        self.uploader = uploader
        self.local_path = local_path
        self.remote_path = remote_path

    def run(self):
        def cb(success, message):
            if message:
                self.progress.emit(message)

        success = self.uploader.upload(self.local_path, self.remote_path, cb)
        self.finished.emit(success)


class DownloadWorker(QtCore.QThread):
    progress = QtCore.Signal(str)
    finished = QtCore.Signal(bool)

    def __init__(
        self, uploader: FileUploader, remote_path: str, local_dest: str, is_dir: bool
    ):
        super().__init__()
        self.uploader = uploader
        self.remote_path = remote_path
        self.local_dest = local_dest
        self.is_dir = is_dir

    def run(self):
        def cb(success, message):
            if message:
                self.progress.emit(message)

        success = self.uploader.download(
            self.remote_path, self.local_dest, self.is_dir, cb
        )
        self.finished.emit(success)


class RemoteListWorker(QtCore.QThread):
    completed = QtCore.Signal(str, list)
    failed = QtCore.Signal(str)

    def __init__(self, remote_fs: RemoteFileSystem, path: str):
        super().__init__()
        self.remote_fs = remote_fs
        self.path = path

    def run(self):
        try:
            items = self.remote_fs.list_directory(self.path)
            self.completed.emit(self.path, items)
        except Exception as e:
            self.failed.emit(str(e))


def human_size(size_str: str) -> str:
    """Convert a size string (bytes) to human-friendly format"""
    try:
        size = int(size_str)
    except Exception:
        return size_str
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    value = int(round(size))
    return f"{value:>6} {units[idx]:>2}"


def format_mtime(iso_str: str) -> str:
    """Convert UTC timestamp to Mountain Time for display."""
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        mt = dt.astimezone(ZoneInfo("America/Denver"))
        return f"{mt.strftime('%B')} {mt.day}, {mt.hour:02d}:{mt.minute:02d}"
    except Exception:
        return iso_str


TypeRole = QtCore.Qt.UserRole + 1
PathRole = QtCore.Qt.UserRole + 2


class RemoteTreeView(QtWidgets.QTreeView):
    dropRequested = QtCore.Signal(list, str)
    renameRequested = QtCore.Signal(str, str)
    deleteRequested = QtCore.Signal(str)
    downloadRequested = QtCore.Signal(str, bool)
    bookmarkRequested = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_path = "/"
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DropOnly)
        self.setDefaultDropAction(QtCore.Qt.CopyAction)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_context_menu)

    def set_current_path(self, path: str):
        self.current_path = path

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent):
        if event.mimeData().hasUrls():
            pos = (
                event.position().toPoint()
                if hasattr(event, "position")
                else event.pos()
            )
            idx = self.indexAt(pos)
            if idx.isValid():
                self.setCurrentIndex(idx)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent):
        if not event.mimeData().hasUrls():
            event.ignore()
            return

        local_paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                local_paths.append(url.toLocalFile())

        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        idx = self.indexAt(pos)
        target_path = self.current_path

        if idx.isValid():
            item_type = idx.data(TypeRole)
            name = idx.data(PathRole) or idx.data(QtCore.Qt.DisplayRole)
            if item_type == "folder":
                target_path = str(Path(self.current_path) / name)
            elif item_type == "link":
                target_path = str(Path(self.current_path) / name)
            elif item_type == "parent":
                target_path = str(Path(self.current_path).parent)

        self.dropRequested.emit(local_paths, target_path)
        event.acceptProposedAction()

    def open_context_menu(self, pos: QtCore.QPoint):
        idx = self.indexAt(pos)
        if not idx.isValid():
            return
        item_type = idx.data(TypeRole)
        name = idx.data(PathRole) or idx.data(QtCore.Qt.DisplayRole)
        if item_type == "parent":
            return

        menu = QtWidgets.QMenu(self)
        download_action = menu.addAction("Download")
        bookmark_action = None
        if item_type in ("folder", "link"):
            bookmark_action = menu.addAction("Add bookmark")
        rename_action = menu.addAction("Rename")
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.viewport().mapToGlobal(pos))

        full_path = str(Path(self.current_path) / name)
        if action == download_action:
            self.downloadRequested.emit(full_path, item_type in ("folder", "link"))
        elif bookmark_action and action == bookmark_action:
            self.bookmarkRequested.emit(full_path)
        elif action == rename_action:
            self.renameRequested.emit(full_path, name)
        elif action == delete_action:
            self.deleteRequested.emit(full_path)


class FolderTreeView(QtWidgets.QTreeView):
    navigateRequested = QtCore.Signal(str)
    dropRequested = QtCore.Signal(list, str)
    renameRequested = QtCore.Signal(str, str)
    deleteRequested = QtCore.Signal(str)
    downloadRequested = QtCore.Signal(str, bool)
    bookmarkRequested = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setIndentation(18)
        self.setRootIsDecorated(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setExpandsOnDoubleClick(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DropOnly)
        self.setDefaultDropAction(QtCore.Qt.CopyAction)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_context_menu)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent):
        if event.mimeData().hasUrls():
            pos = (
                event.position().toPoint()
                if hasattr(event, "position")
                else event.pos()
            )
            idx = self.indexAt(pos)
            if idx.isValid():
                self.setCurrentIndex(idx)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent):
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        local_paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                local_paths.append(url.toLocalFile())
        if not local_paths:
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        idx = self.indexAt(pos)
        target_path = "/"
        if idx.isValid():
            target_path = idx.data(PathRole) or target_path
        self.dropRequested.emit(local_paths, target_path)
        event.acceptProposedAction()

    def open_context_menu(self, pos: QtCore.QPoint):
        idx = self.indexAt(pos)
        if not idx.isValid():
            return
        path = idx.data(PathRole)
        if not path:
            return
        menu = QtWidgets.QMenu(self)
        download_action = menu.addAction("Download")
        bookmark_action = menu.addAction("Add bookmark")
        rename_action = menu.addAction("Rename")
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.viewport().mapToGlobal(pos))
        name = Path(path).name or path
        if action == download_action:
            self.downloadRequested.emit(path, True)
        elif action == bookmark_action:
            self.bookmarkRequested.emit(path)
        elif action == rename_action:
            self.renameRequested.emit(path, name)
        elif action == delete_action:
            self.deleteRequested.emit(path)


class BookmarkList(QtWidgets.QListWidget):
    navigateRequested = QtCore.Signal(str)
    dropRequested = QtCore.Signal(list, str)
    removeRequested = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._suppress_nav = False
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setUniformItemSizes(True)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_context_menu)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DropOnly)
        self.setDefaultDropAction(QtCore.Qt.CopyAction)
        self.itemClicked.connect(self.on_click)
        self.itemDoubleClicked.connect(self.on_double_click)
        self.setSpacing(2)
        self.setStyleSheet(
            """
            QListWidget {
                background: #0f1626;
                border: 1px solid #1f2937;
                border-radius: 8px;
                padding: 6px;
            }
            QListWidget::item {
                padding: 5px 8px;
                margin: 1px 0;
                border-radius: 6px;
                color: #e6edf3;
            }
            QListWidget::item:hover {
                background: #1b2434;
            }
            QListWidget::item:selected {
                background: #2f4d63;
                color: #e6edf3;
            }
            """
        )

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent):
        if event.mimeData().hasUrls():
            pos = (
                event.position().toPoint()
                if hasattr(event, "position")
                else event.pos()
            )
            if self.itemAt(pos):
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent):
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        local_paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                local_paths.append(url.toLocalFile())
        if not local_paths:
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        item = self.itemAt(pos)
        if item:
            target_path = item.data(QtCore.Qt.UserRole)
            event.setDropAction(QtCore.Qt.CopyAction)
            self.dropRequested.emit(local_paths, target_path)
            event.acceptProposedAction()
        else:
            event.ignore()
        # Suppress navigation triggered by mouse release after drop
        self._suppress_nav = True
        QtCore.QTimer.singleShot(200, lambda: setattr(self, "_suppress_nav", False))

    def on_click(self, item: QtWidgets.QListWidgetItem):
        if self._suppress_nav:
            return
        path = item.data(QtCore.Qt.UserRole)
        if path:
            self.navigateRequested.emit(path)

    def on_double_click(self, item: QtWidgets.QListWidgetItem):
        self.on_click(item)

    def open_context_menu(self, pos: QtCore.QPoint):
        item = self.itemAt(pos)
        if not item:
            return
        menu = QtWidgets.QMenu(self)
        remove_action = menu.addAction("Remove bookmark")
        action = menu.exec(self.mapToGlobal(pos))
        if action == remove_action:
            path = item.data(QtCore.Qt.UserRole)
            self.removeRequested.emit(path)


class UploaderWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SSH File Transfer")
        self.resize(1100, 750)

        self.host_info = None
        self.remote_fs: Optional[RemoteFileSystem] = None
        self.uploader: Optional[FileUploader] = None
        self.current_remote_path = "/home/user"
        self.workers: List[QtCore.QThread] = []
        self._initializing_hosts = False
        self.folder_workers = {}
        self._refresh_expand_flag = True
        self.bookmarks_file = Path(__file__).parent / "bookmarks.json"
        self.bookmarks = {}

        self._build_palette()
        self._setup_ui()
        QtCore.QTimer.singleShot(0, self.connect_to_host)
        self._populate_hosts()
        app = QtWidgets.QApplication.instance()
        if app:
            app.aboutToQuit.connect(self._stop_workers)

    def _build_palette(self):
        palette = QtGui.QPalette()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#0d1017"))
        palette.setColor(QtGui.QPalette.Base, QtGui.QColor("#0f1626"))
        palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#121826"))
        palette.setColor(QtGui.QPalette.Text, QtGui.QColor("#e6edf3"))
        palette.setColor(QtGui.QPalette.Button, QtGui.QColor("#23c4b8"))
        palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#0b111b"))
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#1b2434"))
        palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#e6edf3"))
        self.setPalette(palette)

        self.setStyleSheet(
            """
            QWidget { color: #e6edf3; background: #0d1017; font-family: "Segoe UI", "Helvetica Neue", sans-serif; font-size: 10pt; }
            QLineEdit, QPlainTextEdit { background: #0f1626; border: 1px solid #1f2937; border-radius: 6px; padding: 8px; selection-background-color: #1b2434; }
            QTreeView { background: #0f1626; border: 1px solid #1f2937; border-radius: 6px; alternate-background-color: #121826; selection-background-color: #1b2434; }
            QHeaderView::section { background: #0f1626; color: #9fb3c8; border: 0; padding: 6px 8px; }
            QPushButton { border-radius: 6px; padding: 10px 14px; font-weight: 600; }
            QPushButton#accent { background: #23c4b8; color: #0b111b; border: 0; }
            QPushButton#ghost { background: #0f1626; color: #e6edf3; border: 1px solid #1f2937; }
            QLabel#muted { color: #9fb3c8; }
            QStatusBar { background: #0f1626; border: 0; color: #9fb3c8; }
            """
        )

    def _setup_ui(self):
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        splitter = QtWidgets.QSplitter()
        splitter.setChildrenCollapsible(False)

        # Sidebar
        sidebar_widget = QtWidgets.QWidget()
        sidebar_layout = QtWidgets.QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(0, 0, 6, 0)
        sidebar_layout.setSpacing(6)
        self.folder_model = QtGui.QStandardItemModel(0, 1)
        self.folder_tree = FolderTreeView()
        self.folder_tree.setModel(self.folder_model)
        self.folder_tree.expanded.connect(self.on_folder_expanded)
        self.folder_tree.clicked.connect(self.on_folder_clicked)
        self.folder_tree.collapsed.connect(self.on_folder_collapsed)
        self.folder_tree.dropRequested.connect(self.handle_drop)
        self.folder_tree.renameRequested.connect(self.handle_rename)
        self.folder_tree.deleteRequested.connect(self.handle_delete)
        self.folder_tree.downloadRequested.connect(self.handle_download)
        self.folder_tree.bookmarkRequested.connect(self.add_bookmark)
        sidebar_layout.addWidget(self.folder_tree)
        sidebar_layout.setStretch(1, 1)

        # Bookmarks
        bookmarks_header = QtWidgets.QLabel("Bookmarks")
        bookmarks_header.setObjectName("muted")
        bookmarks_header.setStyleSheet(
            "padding: 4px 6px; font-weight: 600; letter-spacing: 0.3px;"
        )
        sidebar_layout.addWidget(bookmarks_header)
        self.bookmark_list = BookmarkList()
        self.bookmark_list.navigateRequested.connect(self.navigate_to_bookmark)
        self.bookmark_list.dropRequested.connect(self.handle_drop)
        self.bookmark_list.removeRequested.connect(self.remove_bookmark)
        sidebar_layout.addWidget(self.bookmark_list)

        splitter.addWidget(sidebar_widget)
        splitter.setStretchFactor(0, 0)

        # Main pane
        main_widget = QtWidgets.QWidget()
        main_layout = QtWidgets.QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(6)

        path_bar = QtWidgets.QHBoxLayout()
        self.path_edit = QtWidgets.QLineEdit(self.current_remote_path)
        self.path_edit.returnPressed.connect(self.navigate_to_path)
        path_bar.addWidget(self.path_edit)
        up_btn = QtWidgets.QToolButton()
        up_btn.setObjectName("ghost")
        up_btn.setText("^")
        up_btn.setToolTip("Up one directory")
        up_btn.clicked.connect(self.go_up_directory)
        path_bar.addWidget(up_btn)
        refresh_btn = QtWidgets.QToolButton()
        refresh_btn.setObjectName("ghost")
        refresh_btn.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.SP_BrowserReload)
        )
        refresh_btn.clicked.connect(lambda: self.refresh_remote_view(force=True))
        path_bar.addWidget(refresh_btn)

        # Host dropdown to the right, aligned
        path_bar.addStretch()
        self.host_combo = QtWidgets.QComboBox()
        self.host_combo.setEditable(False)
        self.host_combo.setFixedWidth(220)
        self.host_combo.currentTextChanged.connect(self.on_host_changed)
        path_bar.addWidget(self.host_combo)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("muted")
        path_bar.addWidget(self.status_label)
        main_layout.addLayout(path_bar)

        self.model = QtGui.QStandardItemModel(0, 3)
        self.model.setHorizontalHeaderLabels(["Name", "Modified", "Size"])

        self.tree = RemoteTreeView()
        self.tree.setModel(self.model)
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, QtCore.Qt.AscendingOrder)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        self.tree.doubleClicked.connect(self.on_tree_double_click)
        self.tree.dropRequested.connect(self.handle_drop)
        self.tree.renameRequested.connect(self.handle_rename)
        self.tree.deleteRequested.connect(self.handle_delete)
        self.tree.downloadRequested.connect(self.handle_download)
        self.tree.bookmarkRequested.connect(self.add_bookmark)
        self.tree.setColumnWidth(0, 600)
        self.tree.setColumnWidth(1, 170)
        self.tree.setColumnWidth(2, 110)
        main_layout.addWidget(self.tree)

        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(140)
        main_layout.addWidget(self.log_box)
        main_layout.setStretch(1, 1)
        main_layout.setStretch(2, 0)

        splitter.addWidget(main_widget)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter, 1)

        self.setCentralWidget(central)
        self.statusBar().hide()

    def build_folder_root(self, base_path: str):
        """Initialize folder tree with base path"""
        self.folder_model.clear()
        self.folder_model.setHorizontalHeaderLabels(["Folders"])
        style = self.style()
        folder_icon = style.standardIcon(QtWidgets.QStyle.SP_DirIcon)

        root_item = QtGui.QStandardItem(folder_icon, "/")
        root_item.setData("/", PathRole)
        root_item.setEditable(False)
        root_item.setData(False, QtCore.Qt.UserRole)  # loaded flag
        root_item.appendRow(QtGui.QStandardItem())  # dummy
        self.folder_model.appendRow(root_item)
        self.folder_tree.expand(self.folder_model.indexFromItem(root_item))
        self.ensure_folder_children(root_item)
        self.sync_folder_selection(base_path)

    def ensure_folder_children(self, item: QtGui.QStandardItem):
        """Lazy-load children for a folder item asynchronously"""
        loaded = item.data(QtCore.Qt.UserRole)
        if loaded:
            return
        path = item.data(PathRole)
        if not path or not self.remote_fs:
            return
        if path in self.folder_workers:
            return

        worker = RemoteListWorker(self.remote_fs, path)
        worker.setParent(self)

        def on_done(p, items, target_item=item):
            self.folder_workers.pop(p, None)
            target_item.removeRows(0, target_item.rowCount())
            style = self.style()
            folder_icon = style.standardIcon(QtWidgets.QStyle.SP_DirIcon)
            link_icon = style.standardIcon(QtWidgets.QStyle.SP_DirLinkIcon)
            for entry in items:
                if entry.get("is_dir") or entry.get("is_link"):
                    icon = folder_icon if entry.get("is_dir") else link_icon
                    display_name = entry["name"]
                    child = QtGui.QStandardItem(icon, display_name)
                    child.setEditable(False)
                    child.setData(str(Path(p) / entry["name"]), PathRole)
                    child.setData(False, QtCore.Qt.UserRole)
                    child.appendRow(QtGui.QStandardItem())  # dummy
                    target_item.appendRow(child)
            target_item.setData(True, QtCore.Qt.UserRole)

        def on_fail(err, p=path):
            self.folder_workers.pop(p, None)

        worker.completed.connect(on_done)
        worker.failed.connect(on_fail)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda: self.folder_workers.pop(path, None))
        self.folder_workers[path] = worker
        worker.start()

    def on_folder_expanded(self, index: QtCore.QModelIndex):
        item = self.folder_model.itemFromIndex(index)
        if item:
            self.ensure_folder_children(item)

    def on_folder_clicked(self, index: QtCore.QModelIndex):
        item = self.folder_model.itemFromIndex(index)
        if not item:
            return
        path = item.data(PathRole)
        if path:
            self.current_remote_path = path
            self.refresh_remote_view()
            self.path_edit.setText(path)

    def on_folder_collapsed(self, index: QtCore.QModelIndex):
        item = self.folder_model.itemFromIndex(index)
        if not item:
            return
        path = item.data(PathRole)
        current = Path(self.current_remote_path)
        collapsed = Path(path)
        # If current path is inside the collapsed folder, move up to the collapsed path
        try:
            current.relative_to(collapsed)
            self.current_remote_path = str(collapsed)
            self.refresh_remote_view(expand=False)
            self.path_edit.setText(self.current_remote_path)
            # Do not auto-expand again; just select the collapsed folder
            idx = self.folder_model.indexFromItem(item)
            if idx.isValid():
                self.folder_tree.blockSignals(True)
                self.folder_tree.setCurrentIndex(idx)
                self.folder_tree.blockSignals(False)
        except Exception:
            pass

    def log(self, message: str):
        self.log_box.appendPlainText(message)
        self.log_box.verticalScrollBar().setValue(
            self.log_box.verticalScrollBar().maximum()
        )

    def set_status(self, text: str):
        self.status_label.setText(text)
        # Status bar hidden; label only

    def _populate_hosts(self):
        """Load hosts from SSH config into the dropdown"""
        self._initializing_hosts = True
        try:
            ssh_config = SSHConfig()
            hosts = ssh_config.list_hosts()
        except Exception:
            hosts = []

        if not hosts:
            hosts = ["vast-ai"]

        current = self.host_combo.currentText()
        self.host_combo.clear()
        self.host_combo.addItems(hosts)

        if current and current in hosts:
            self.host_combo.setCurrentText(current)
        elif "vast-ai" in hosts:
            self.host_combo.setCurrentText("vast-ai")
        else:
            self.host_combo.setCurrentIndex(0)
        self._initializing_hosts = False
        self.load_bookmarks()
        self.update_bookmark_list()

    def on_host_changed(self, text: str):
        if self._initializing_hosts:
            return
        if text:
            self.connect_to_host()

    def connect_to_host(self):
        host_alias = (self.host_combo.currentText() or "vast-ai").strip()
        self.set_status(f"Connecting to {host_alias}...")
        self.remote_fs = None
        self.uploader = None

        if not shutil.which("hpnssh"):
            self.set_status("HPN-SSH not found")
            QtWidgets.QMessageBox.critical(
                self,
                "HPN-SSH Missing",
                "hpnssh not found on PATH.\n\nInstall HPN-SSH to use the uploader.",
            )
            return
        if not shutil.which("rsync"):
            self.set_status("rsync not found")
            QtWidgets.QMessageBox.critical(
                self,
                "rsync Missing",
                "rsync not found on PATH.\n\nInstall rsync to use the uploader.",
            )
            return

        try:
            ssh_config = SSHConfig()
            self.host_info = ssh_config.get_host_info(host_alias)

            hostname = self.host_info.get("hostname")
            port = self.host_info.get("port", "22")
            user = self.host_info.get("user", "user")
            identity = self.host_info.get("identity", "")

            mapped_port = _resolve_vast_port(hostname, 2222)
            if mapped_port:
                port = mapped_port

            self.remote_fs = RemoteFileSystem(hostname, port, user, identity)
            self.uploader = FileUploader(hostname, port, user, identity)

            self.set_status(f"Connected: {user}@{hostname}:{port}")
            self.log(f"Connected to {user}@{hostname}:{port}")
            self.update_bookmark_list()
            self.build_folder_root(self.current_remote_path)
            self.refresh_remote_view(force=True)
        except FileNotFoundError as e:
            self.set_status("SSH config not found")
            QtWidgets.QMessageBox.critical(
                self, "Connection Error", f"{e}\n\nAdd '{host_alias}' to ~/.ssh/config."
            )
        except Exception as e:
            self.set_status("Connection failed")
            QtWidgets.QMessageBox.critical(
                self,
                "Connection Error",
                f"Failed to connect to '{host_alias}':\n\n{e}\n\nCheck your ~/.ssh/config entry.",
            )

    def refresh_remote_view(self, expand: bool = True, force: bool = False):
        if not self.remote_fs:
            return

        path = self.current_remote_path
        if force:
            self.remote_fs.clear_cache(path)
        self._refresh_expand_flag = expand
        # keep status stable during refresh
        self.model.removeRows(0, self.model.rowCount())
        worker = RemoteListWorker(self.remote_fs, path)
        worker.completed.connect(self.on_remote_loaded)
        worker.failed.connect(self.on_remote_load_failed)
        self._register_worker(worker)
        worker.start()

    def on_remote_loaded(self, path: str, items: list):
        if self.remote_fs:
            self.remote_fs.cache[path] = items
        self.current_remote_path = path
        self.tree.set_current_path(path)
        self.path_edit.setText(path)
        self.model.removeRows(0, self.model.rowCount())

        style = self.style()
        folder_icon = style.standardIcon(QtWidgets.QStyle.SP_DirIcon)
        file_icon = style.standardIcon(QtWidgets.QStyle.SP_FileIcon)
        link_icon = style.standardIcon(QtWidgets.QStyle.SP_FileLinkIcon)

        if Path(path) != Path("/"):
            parent_item = QtGui.QStandardItem(folder_icon, "..")
            parent_item.setData("parent", TypeRole)
            parent_item.setEditable(False)
            modified_item = QtGui.QStandardItem("")
            size_item = QtGui.QStandardItem("")
            for item in (modified_item, size_item):
                item.setEditable(False)
            size_item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.model.appendRow([parent_item, modified_item, size_item])

        for item in items:
            icon = folder_icon if item["is_dir"] else file_icon
            if item["is_link"]:
                icon = link_icon
            display_name = item.get("display_name") or item["name"]
            name_item = QtGui.QStandardItem(icon, display_name)
            name_item.setData(item["name"], PathRole)
            item_type = "folder" if item["is_dir"] else "file"
            if item["is_link"]:
                item_type = "link"
            name_item.setData(item_type, TypeRole)
            name_item.setEditable(False)
            modified_item = QtGui.QStandardItem(format_mtime(item.get("mtime", "")))
            modified_item.setEditable(False)
            size_item = QtGui.QStandardItem(human_size(item.get("size", "")))
            size_item.setEditable(False)
            size_item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.model.appendRow([name_item, modified_item, size_item])

        self.set_status("")
        self.log(f"Loaded {len(items)} items from {path}")
        self.sync_folder_selection(path, expand=self._refresh_expand_flag)

    def on_remote_load_failed(self, error: str):
        self.set_status("Failed to load directory")
        self.log(f"❌ Failed to load: {error}")
        QtWidgets.QMessageBox.critical(
            self, "Error", f"Failed to load directory:\n{error}"
        )

    def on_tree_double_click(self, index: QtCore.QModelIndex):
        item_type = index.data(TypeRole)
        name = index.data(PathRole) or index.data(QtCore.Qt.DisplayRole)
        if item_type == "parent":
            self.current_remote_path = str(Path(self.current_remote_path).parent)
        elif item_type in ("folder", "link"):
            self.current_remote_path = str(Path(self.current_remote_path) / name)
        else:
            return
        self.refresh_remote_view()
        self.sync_folder_selection(self.current_remote_path)

    def navigate_to_path(self):
        new_path = self.path_edit.text().strip()
        if new_path:
            self.current_remote_path = new_path
            self.refresh_remote_view()
            self.sync_folder_selection(new_path)

    def go_up_directory(self):
        self.current_remote_path = str(Path(self.current_remote_path).parent)
        self.refresh_remote_view()
        self.sync_folder_selection(self.current_remote_path)

    def handle_drop(self, paths: List[str], target_path: str):
        if not self.uploader:
            QtWidgets.QMessageBox.warning(
                self, "Not connected", "Connect to a host first."
            )
            return
        if not paths or not target_path:
            return
        refresh_after = target_path == self.current_remote_path
        entries_by_name = {}
        if self.remote_fs:
            self.remote_fs.clear_cache(target_path if refresh_after else None)
            try:
                entries = self.remote_fs.list_directory(target_path)
                entries_by_name = {e.get("name"): e for e in entries if e.get("name")}
            except Exception:
                entries_by_name = {}

        last_idx = len(paths) - 1
        for i, local_path in enumerate(paths):
            local_name = Path(local_path).name
            target_name = local_name
            existing = entries_by_name.get(target_name)
            overwrite = False

            while existing:
                current_name = target_name
                text, ok = QtWidgets.QInputDialog.getText(
                    self,
                    "Name conflict",
                    f"'{target_name}' exists in {target_path}.\n"
                    "Enter a new name or keep it to overwrite:",
                    text=target_name,
                )
                if not ok:
                    target_name = None
                    break
                text = text.strip()
                if not text:
                    continue
                if text == current_name:
                    overwrite = True
                    target_name = text
                    break
                target_name = text
                existing = entries_by_name.get(target_name)
                if not existing:
                    break

            if not target_name:
                continue

            if overwrite and existing and self.remote_fs:
                try:
                    self.remote_fs.delete_path(str(Path(target_path) / target_name))
                    entries_by_name.pop(target_name, None)
                except Exception:
                    pass

            remote_dest = str(Path(target_path) / target_name)
            entries_by_name[target_name] = {"name": target_name}
            worker = UploadWorker(self.uploader, local_path, remote_dest)
            worker.progress.connect(self.log)
            if refresh_after and i == last_idx:
                worker.finished.connect(
                    lambda success: self.refresh_remote_view(force=True)
                )
            self._register_worker(worker)
            worker.start()

    def sync_folder_selection(self, path: str, expand: bool = True):
        """Select/expand folder tree to the current path"""
        target = Path(path)
        # build chain of parts
        segments = target.parts
        if not segments:
            return

        def find_child(parent_item, name):
            for i in range(parent_item.rowCount()):
                child = parent_item.child(i)
                if child and child.text() == name:
                    return child
            return None

        item = self.folder_model.invisibleRootItem().child(0)
        current_path = Path("/")
        for seg in segments[1:]:
            current_path = current_path / seg
            if item is None:
                break
            if expand:
                self.ensure_folder_children(item)
            next_item = find_child(item, seg)
            if next_item:
                idx = self.folder_model.indexFromItem(next_item)
                if expand:
                    self.folder_tree.setExpanded(idx, True)
                item = next_item
            else:
                break

        if item:
            idx = self.folder_model.indexFromItem(item)
            self.folder_tree.setCurrentIndex(idx)

    def handle_rename(self, full_path: str, current_name: str):
        if not self.remote_fs:
            QtWidgets.QMessageBox.warning(
                self, "Not connected", "Connect to a host first."
            )
            return
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename", f"Rename '{current_name}' to:", text=current_name
        )
        if not ok or not new_name.strip() or new_name.strip() == current_name:
            return
        new_name = new_name.strip()
        new_path = str(Path(full_path).with_name(new_name))
        try:
            self.remote_fs.rename_path(full_path, new_path)
            self.log(f"Renamed {current_name} -> {new_name}")
            if self.remote_fs:
                self.remote_fs.clear_cache(str(Path(full_path).parent))
            self.refresh_remote_view(force=True)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Rename failed", str(e))

    def handle_delete(self, full_path: str):
        if not self.remote_fs:
            QtWidgets.QMessageBox.warning(
                self, "Not connected", "Connect to a host first."
            )
            return
        target_name = Path(full_path).name
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete",
            f"Delete '{target_name}'?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        try:
            self.remote_fs.delete_path(full_path)
            self.log(f"Deleted {target_name}")
            if self.remote_fs:
                self.remote_fs.clear_cache(str(Path(full_path).parent))
            self.refresh_remote_view(force=True)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Delete failed", str(e))

    def handle_download(self, remote_path: str, is_dir: bool):
        if not self.uploader:
            QtWidgets.QMessageBox.warning(
                self, "Not connected", "Connect to a host first."
            )
            return

        default_dir = str(Path.home() / "Downloads")
        target_dir = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select download destination", default_dir
        )
        if not target_dir:
            return
        remote_name = Path(remote_path).name or "download"
        dest_path = Path(target_dir) / remote_name
        counter = 1
        while dest_path.exists():
            dest_path = Path(target_dir) / f"{remote_name}_{counter}"
            counter += 1
        local_dest = str(dest_path)

        if self.remote_fs:
            parent = str(Path(remote_path).parent)
            self.remote_fs.clear_cache(parent)

        worker = DownloadWorker(self.uploader, remote_path, local_dest, is_dir)
        worker.progress.connect(self.log)
        self._register_worker(worker)
        worker.start()

    def _register_worker(self, worker: QtCore.QThread):
        """Track and clean up workers safely"""
        worker.setParent(self)
        self.workers.append(worker)
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        worker.finished.connect(worker.deleteLater)

    def _cleanup_worker(self, worker: QtCore.QThread):
        try:
            worker.deleteLater()
        except Exception:
            pass
        if worker in self.workers:
            self.workers.remove(worker)

    def _stop_workers(self):
        # Best-effort shutdown of running workers
        for w in list(self.folder_workers.values()):
            try:
                w.requestInterruption()
                w.wait(500)
            except Exception:
                pass
        self.folder_workers.clear()
        for w in list(self.workers):
            try:
                w.requestInterruption()
                w.wait(500)
            except Exception:
                pass
        self.workers.clear()

    # Bookmarks
    def load_bookmarks(self):
        try:
            if self.bookmarks_file.exists():
                with open(self.bookmarks_file, "r") as f:
                    self.bookmarks = json.load(f)
            else:
                self.bookmarks = {}
        except Exception:
            self.bookmarks = {}

    def save_bookmarks(self):
        try:
            with open(self.bookmarks_file, "w") as f:
                json.dump(self.bookmarks, f, indent=2)
        except Exception:
            pass

    def current_host_key(self) -> str:
        return self.host_combo.currentText() or "vast-ai"

    def update_bookmark_list(self):
        host = self.current_host_key()
        entries = self.bookmarks.get(host, [])
        self.bookmark_list.clear()
        for p in entries:
            item = QtWidgets.QListWidgetItem(p)
            item.setData(QtCore.Qt.UserRole, p)
            self.bookmark_list.addItem(item)

    def add_bookmark(self, path: str):
        if not self.remote_fs:
            QtWidgets.QMessageBox.warning(
                self, "Not connected", "Connect to a host first."
            )
            return
        path = str(Path(path))
        if path != "/":
            parent = str(Path(path).parent)
            name = Path(path).name
            try:
                entries = self.remote_fs.list_directory(parent)
            except Exception:
                entries = []
            matched = next((e for e in entries if e.get("name") == name), None)
            if not matched or not (matched.get("is_dir") or matched.get("is_link")):
                QtWidgets.QMessageBox.warning(
                    self,
                    "Unsupported bookmark",
                    "Only folders and symbolic links can be bookmarked.",
                )
                return
        host = self.current_host_key()
        entries = self.bookmarks.get(host, [])
        if path not in entries:
            entries.append(path)
            self.bookmarks[host] = entries
            self.save_bookmarks()
            self.update_bookmark_list()

    def remove_bookmark(self, path: str):
        host = self.current_host_key()
        entries = self.bookmarks.get(host, [])
        if path in entries:
            entries.remove(path)
            self.bookmarks[host] = entries
            self.save_bookmarks()
            self.update_bookmark_list()

    def navigate_to_bookmark(self, path: str):
        if path:
            self.current_remote_path = path
            self.path_edit.setText(path)
            self.refresh_remote_view(force=True)
            self.sync_folder_selection(path)


def main():
    try:
        app = QtWidgets.QApplication(sys.argv)
    except Exception as e:
        print("PySide6 is required for the GUI. Install with: pip install PySide6")
        print(f"Error: {e}")
        sys.exit(1)

    window = UploaderWindow()
    window.show()
    exit_code = app.exec()
    window._stop_workers()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
