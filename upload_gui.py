#!/usr/bin/env python3
"""
Fast File Uploader (PySide6)
Qt-based UI with drag-and-drop uploads and remote file browser.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypedDict
from zoneinfo import ZoneInfo

from PySide6 import QtCore, QtGui, QtWidgets
from typing_extensions import override

from common import SSHConfig


class FileSystemItem(TypedDict):
    name: str
    display_name: str
    is_dir: bool
    is_link: bool
    link_target: str | None
    perms: str
    size: str
    mtime: str


class RemoteFileSystem:
    """Handle remote filesystem operations via SSH"""

    host: str
    port: str
    user: str
    identity: str
    control_path: str
    cache: dict[str, list[FileSystemItem]]

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
            raise RuntimeError("hpnssh not found; install HPN-SSH to use the uploader.")
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
            raise RuntimeError(f"SSH command failed: {e.stderr}") from e

    def list_directory(self, path: str) -> list[FileSystemItem]:
        """List files and directories at path"""
        if path in self.cache:
            return self.cache[path]
        command = (
            f'cd "{path}" 2>/dev/null && '
            "TZ=UTC ls -lA --time-style=+%Y-%m-%dT%H:%M:%S "
            '--group-directories-first 2>/dev/null || echo "ERROR"'
        )
        output = self._run_ssh_command(command)

        if output.strip() == "ERROR":
            return []

        items: list[FileSystemItem] = []
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
            link_target: str | None = None
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

    def clear_cache(self, path: str | None = None) -> None:
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

    host: str
    port: str
    user: str
    identity: str

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

    def upload(
        self,
        local_path: str,
        remote_path: str,
        progress_callback: Callable[[bool | None, str], None] | None = None,
    ) -> bool:
        """Upload file or folder to remote path"""
        local_path_obj = Path(local_path)

        if not local_path_obj.exists():
            if progress_callback:
                progress_callback(False, f"Not found: {local_path_obj}")
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

        if local_path_obj.is_dir():
            # Upload the folder itself (not just its contents) to the destination
            # No trailing slash on source = copy the folder itself
            local_str = str(local_path_obj)
            remote_dest = f"{self.user}@{self.host}:{remote_base}/"
        else:
            local_str = str(local_path_obj)
        cmd.extend([local_str, remote_dest])

        try:
            if progress_callback:
                progress_callback(None, f"Uploading {local_path_obj.name}...")

            _ = subprocess.run(cmd, capture_output=True, text=True, check=True)

            if progress_callback:
                progress_callback(True, f"✅ {local_path_obj.name}")

            return True

        except subprocess.CalledProcessError as e:
            err = e.stderr or ""
            if progress_callback:
                progress_callback(False, f"❌ {local_path_obj.name}: {err}")
            return False

    def download(
        self,
        remote_path: str,
        local_dest: str,
        is_dir: bool,
        progress_callback: Callable[[bool | None, str], None] | None = None,
    ) -> bool:
        """Download file or folder from remote"""
        local_dest_path = Path(local_dest)
        local_dest_path.parent.mkdir(parents=True, exist_ok=True)

        source = f"{self.user}@{self.host}:{remote_path}"
        if is_dir:
            source = f"{source.rstrip('/')}/"
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
            _ = subprocess.run(cmd, capture_output=True, text=True, check=True)
            if progress_callback:
                progress_callback(True, f"✅ Downloaded {Path(remote_path).name}")
            return True
        except subprocess.CalledProcessError as e:
            err = e.stderr or ""
            if progress_callback:
                progress_callback(False, f"❌ Download failed: {err}")
            return False


class UploadWorker(QtCore.QThread):
    progress: QtCore.Signal = QtCore.Signal(str)
    finished_: QtCore.Signal = QtCore.Signal(bool)
    uploader: FileUploader
    local_path: str
    remote_path: str

    def __init__(self, uploader: FileUploader, local_path: str, remote_path: str):
        super().__init__()
        self.uploader = uploader
        self.local_path = local_path
        self.remote_path = remote_path

    @override
    def run(self) -> None:
        def cb(success: bool | None, message: str) -> None:
            if message:
                self.progress.emit(message)

        success = self.uploader.upload(self.local_path, self.remote_path, cb)
        self.finished_.emit(success)


class DownloadWorker(QtCore.QThread):
    progress: QtCore.Signal = QtCore.Signal(str)
    finished_: QtCore.Signal = QtCore.Signal(bool)
    uploader: FileUploader
    remote_path: str
    local_dest: str
    is_dir: bool

    def __init__(
        self,
        uploader: FileUploader,
        remote_path: str,
        local_dest: str,
        is_dir: bool,
    ):
        super().__init__()
        self.uploader = uploader
        self.remote_path = remote_path
        self.local_dest = local_dest
        self.is_dir = is_dir

    @override
    def run(self) -> None:
        def cb(success: bool | None, message: str) -> None:
            if message:
                self.progress.emit(message)

        success = self.uploader.download(
            self.remote_path, self.local_dest, self.is_dir, cb
        )
        self.finished_.emit(success)


class RemoteListWorker(QtCore.QThread):
    completed: QtCore.Signal = QtCore.Signal(str, list)
    failed: QtCore.Signal = QtCore.Signal(str)
    remote_fs: RemoteFileSystem
    path: str

    def __init__(self, remote_fs: RemoteFileSystem, path: str):
        super().__init__()
        self.remote_fs = remote_fs
        self.path = path

    @override
    def run(self) -> None:
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


TypeRole = QtCore.Qt.ItemDataRole.UserRole + 1
PathRole = QtCore.Qt.ItemDataRole.UserRole + 2


class RemoteTreeView(QtWidgets.QTreeView):
    dropRequested: QtCore.Signal = QtCore.Signal(list, str)
    renameRequested: QtCore.Signal = QtCore.Signal(str, str)
    deleteRequested: QtCore.Signal = QtCore.Signal(str)
    downloadRequested: QtCore.Signal = QtCore.Signal(str, bool)
    bookmarkRequested: QtCore.Signal = QtCore.Signal(str)
    current_path: str

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_path = "/"
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DropOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_context_menu)

    def set_current_path(self, path: str) -> None:
        self.current_path = path

    @override
    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    @override
    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            pos = event.position().toPoint()
            idx = self.indexAt(pos)
            if idx.isValid():
                self.setCurrentIndex(idx)
            event.acceptProposedAction()
        else:
            event.ignore()

    @override
    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        if not event.mimeData().hasUrls():
            event.ignore()
            return

        local_paths = [
            url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()
        ]

        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        target_path = self.current_path

        if idx.isValid():
            item_type = idx.data(TypeRole)
            name = idx.data(PathRole) or idx.data(QtCore.Qt.ItemDataRole.DisplayRole)
            if item_type == "folder":
                target_path = str(Path(self.current_path) / name)
            elif item_type == "link":
                target_path = str(Path(self.current_path) / name)
            elif item_type == "parent":
                target_path = str(Path(self.current_path).parent)

        self.dropRequested.emit(local_paths, target_path)
        event.acceptProposedAction()

    def open_context_menu(self, pos: QtCore.QPoint) -> None:
        idx = self.indexAt(pos)
        if not idx.isValid():
            return
        item_type = idx.data(TypeRole)
        name = idx.data(PathRole) or idx.data(QtCore.Qt.ItemDataRole.DisplayRole)
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
    navigateRequested: QtCore.Signal = QtCore.Signal(str)
    dropRequested: QtCore.Signal = QtCore.Signal(list, str)
    renameRequested: QtCore.Signal = QtCore.Signal(str, str)
    deleteRequested: QtCore.Signal = QtCore.Signal(str)
    downloadRequested: QtCore.Signal = QtCore.Signal(str, bool)
    bookmarkRequested: QtCore.Signal = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setIndentation(18)
        self.setRootIsDecorated(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setExpandsOnDoubleClick(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DropOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_context_menu)

    @override
    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    @override
    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            pos = event.position().toPoint()
            idx = self.indexAt(pos)
            if idx.isValid():
                self.setCurrentIndex(idx)
            event.acceptProposedAction()
        else:
            event.ignore()

    @override
    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        local_paths = [
            url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()
        ]
        if not local_paths:
            event.ignore()
            return
        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        target_path = "/"
        if idx.isValid():
            target_path = idx.data(PathRole) or target_path
        self.dropRequested.emit(local_paths, target_path)
        event.acceptProposedAction()

    def open_context_menu(self, pos: QtCore.QPoint) -> None:
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
    navigateRequested: QtCore.Signal = QtCore.Signal(str)
    dropRequested: QtCore.Signal = QtCore.Signal(list, str)
    removeRequested: QtCore.Signal = QtCore.Signal(str)
    _suppress_nav: bool

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._suppress_nav = False
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_context_menu)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DropOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)
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

    @override
    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    @override
    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            pos = event.position().toPoint()
            if self.itemAt(pos):
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            event.ignore()

    @override
    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        local_paths = [
            url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()
        ]
        if not local_paths:
            event.ignore()
            return
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        if item:
            target_path = item.data(QtCore.Qt.ItemDataRole.UserRole)
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            self.dropRequested.emit(local_paths, target_path)
            event.acceptProposedAction()
        else:
            event.ignore()
        # Suppress navigation triggered by mouse release after drop
        self._suppress_nav = True
        QtCore.QTimer.singleShot(200, lambda: setattr(self, "_suppress_nav", False))

    def on_click(self, item: QtWidgets.QListWidgetItem) -> None:
        if self._suppress_nav:
            return
        path = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if path:
            self.navigateRequested.emit(path)

    def on_double_click(self, item: QtWidgets.QListWidgetItem) -> None:
        self.on_click(item)

    def open_context_menu(self, pos: QtCore.QPoint) -> None:
        item = self.itemAt(pos)
        if not item:
            return
        menu = QtWidgets.QMenu(self)
        remove_action = menu.addAction("Remove bookmark")
        action = menu.exec(self.mapToGlobal(pos))
        if action == remove_action:
            path = item.data(QtCore.Qt.ItemDataRole.UserRole)
            self.removeRequested.emit(path)


class UploaderWindow(QtWidgets.QMainWindow):
    host_info: dict[str, str] | None
    remote_fs: RemoteFileSystem | None
    uploader: FileUploader | None
    current_remote_path: str
    workers: list[QtCore.QThread]
    _initializing_hosts: bool
    folder_workers: dict[str, RemoteListWorker]
    _refresh_expand_flag: bool
    bookmarks_file: Path
    bookmarks: dict[str, list[str]]
    folder_model: QtGui.QStandardItemModel
    folder_tree: FolderTreeView
    bookmark_list: BookmarkList
    path_edit: QtWidgets.QLineEdit
    host_combo: QtWidgets.QComboBox
    status_label: QtWidgets.QLabel
    model: QtGui.QStandardItemModel
    tree: RemoteTreeView
    log_box: QtWidgets.QPlainTextEdit
    expanded_folders: set[str]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SSH File Transfer")
        self.resize(1100, 750)

        self.host_info = None
        self.remote_fs = None
        self.uploader = None
        self.current_remote_path = "/"
        self.workers = []
        self._initializing_hosts = False
        self.folder_workers = {}
        self._refresh_expand_flag = True
        self.bookmarks_file = Path(__file__).parent / "bookmarks.json"
        self.bookmarks = {}
        self.expanded_folders = set()

        self._build_palette()
        self._setup_ui()
        self._load_bookmarks()
        QtCore.QTimer.singleShot(0, self._populate_hosts)

        app = QtWidgets.QApplication.instance()
        if app:
            app.aboutToQuit.connect(self.stop_workers)

    def _load_bookmarks(self) -> None:
        """Load bookmarks from JSON file"""
        if self.bookmarks_file.exists():
            try:
                with open(self.bookmarks_file, "r", encoding="utf-8") as f:
                    self.bookmarks = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.bookmarks = {}
        else:
            self.bookmarks = {}
        self._update_bookmark_list()

    def _save_bookmarks(self) -> None:
        """Save bookmarks to JSON file"""
        try:
            with open(self.bookmarks_file, "w", encoding="utf-8") as f:
                json.dump(self.bookmarks, f, indent=2)
        except IOError as e:
            self.log_box.appendPlainText(f"Error saving bookmarks: {e}")

    def _update_bookmark_list(self) -> None:
        """Update the bookmark list widget"""
        self.bookmark_list.clear()
        host = self.host_combo.currentText()
        if host in self.bookmarks:
            for path in sorted(self.bookmarks[host]):
                item = QtWidgets.QListWidgetItem(Path(path).name)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
                item.setToolTip(path)
                self.bookmark_list.addItem(item)

    def connect_to_host(self) -> None:
        """Connect to the selected host"""
        host = self.host_combo.currentText()
        if not host:
            return

        try:
            ssh_config = SSHConfig()
            self.host_info = ssh_config.get_host_info(host)
            self.remote_fs = RemoteFileSystem(
                host=self.host_info["hostname"],
                port=self.host_info["port"],
                user=self.host_info["user"],
                identity=self.host_info["identity"],
            )
            self.uploader = FileUploader(
                host=self.host_info["hostname"],
                port=self.host_info["port"],
                user=self.host_info["user"],
                identity=self.host_info["identity"],
            )
            self.log_box.appendPlainText(f"Connected to {host}")
            self._initialize_folder_view()
            self.refresh_remote_view()
            self._update_bookmark_list()
        except Exception as e:
            self.log_box.appendPlainText(f"Failed to connect to {host}: {e}")

    def _populate_hosts(self) -> None:
        """Populate host dropdown from SSH config"""
        self._initializing_hosts = True
        self.host_combo.clear()
        try:
            ssh_config = SSHConfig()
            hosts = ssh_config.list_hosts()
            self.host_combo.addItems(hosts)
            if hosts:
                self.host_combo.setCurrentIndex(0)
                self.connect_to_host()
        except Exception as e:
            self.log_box.appendPlainText(f"Error loading hosts: {e}")
        finally:
            self._initializing_hosts = False

    def _initialize_folder_view(self) -> None:
        """Set up the root of the folder tree"""
        self.folder_model.clear()
        self.folder_tree.setRootIndex(QtCore.QModelIndex())  # Reset root index
        self.expanded_folders.clear()

        if not self.host_info or not self.remote_fs:
            return

        root_path = "/"

        icon_provider = QtWidgets.QFileIconProvider()
        folder_icon = icon_provider.icon(QtWidgets.QFileIconProvider.IconType.Folder)

        root_item = QtGui.QStandardItem(folder_icon, root_path)
        root_item.setData(root_path, PathRole)
        root_item.setEditable(False)

        self.folder_model.appendRow(root_item)

        # Manually load root folder contents
        self._load_folder_contents(root_item, root_path)

        # Expand root after loading starts
        self.folder_tree.expand(root_item.index())
        self.expanded_folders.add(root_path)

    def stop_workers(self) -> None:
        """Stop all running workers"""
        for worker in self.workers:
            if worker.isRunning():
                worker.terminate()
                worker.wait()

    def _load_folder_contents(self, item: QtGui.QStandardItem, path: str) -> None:
        """Load folder contents for a given item and path"""
        if not self.remote_fs:
            return

        worker = RemoteListWorker(self.remote_fs, path)

        def on_complete(p: str, items: list[FileSystemItem]) -> None:
            if p != path:
                return

            icon_provider = QtWidgets.QFileIconProvider()
            folder_icon = icon_provider.icon(
                QtWidgets.QFileIconProvider.IconType.Folder
            )
            file_icon = icon_provider.icon(QtWidgets.QFileIconProvider.IconType.File)

            for file_item in items:
                name = file_item["name"]
                full_path = str(Path(p) / name)

                is_folder_like = file_item["is_dir"] or file_item["is_link"]
                icon = folder_icon if is_folder_like else file_icon

                child_item = QtGui.QStandardItem(icon, name)
                child_item.setData(full_path, PathRole)
                child_item.setEditable(False)

                if is_folder_like:
                    dummy_item = QtGui.QStandardItem()
                    dummy_item.setEditable(False)
                    child_item.appendRow(dummy_item)

                item.appendRow(child_item)

            self.workers = [w for w in self.workers if not w.isFinished()]

        worker.completed.connect(on_complete)
        worker.failed.connect(self._on_list_failed)
        self.workers.append(worker)
        worker.start()

    def on_folder_expanded(self, index: QtCore.QModelIndex) -> None:
        """Dynamically load folder contents when expanded"""
        item = self.folder_model.itemFromIndex(index)
        if not item:
            return

        path = item.data(PathRole)
        if path:
            self.expanded_folders.add(path)

        # If item has a dummy child, remove it before loading real children
        if item.hasChildren():
            first_child = item.child(0)
            if first_child and first_child.data(PathRole) is None:
                item.removeRow(0)
            else:
                # Already populated
                return

        if not path or not self.remote_fs:
            return

        # Use the common loading method
        self._load_folder_contents(item, path)

    def on_folder_clicked(self, index: QtCore.QModelIndex) -> None:
        """Update main view when a folder is clicked"""
        path = index.data(PathRole)
        item = self.folder_model.itemFromIndex(index)
        if not item or not path:
            return

        # If it's a file (no children and no dummy child), don't navigate
        if not item.hasChildren():
            return

        if path and path != self.current_remote_path:
            self.current_remote_path = path
            self.path_edit.setText(path)
            self.refresh_remote_view()

    def _find_folder_item(self, path: str) -> QtGui.QStandardItem | None:
        """Find a folder item by its path in the folder tree"""

        def search_item(
            item: QtGui.QStandardItem, target_path: str
        ) -> QtGui.QStandardItem | None:
            if item.data(PathRole) == target_path:
                return item
            for row in range(item.rowCount()):
                child = item.child(row)
                if child:
                    result = search_item(child, target_path)
                    if result:
                        return result
            return None

        root = self.folder_model.invisibleRootItem()
        for row in range(root.rowCount()):
            item = root.child(row)
            if item:
                result = search_item(item, path)
                if result:
                    return result
        return None

    def _refresh_expanded_folder(self, path: str) -> None:
        """Refresh an expanded folder's contents"""
        if not self.remote_fs or path not in self.expanded_folders:
            return

        item = self._find_folder_item(path)
        if not item:
            return

        # Clear cache for this path
        self.remote_fs.clear_cache(path)

        # Remove all children
        item.removeRows(0, item.rowCount())

        # Load fresh data using the common method
        self._load_folder_contents(item, path)

    def on_folder_collapsed(self, index: QtCore.QModelIndex) -> None:
        """Clear children when a folder is collapsed to allow reloading"""
        item = self.folder_model.itemFromIndex(index)
        if item:
            path = item.data(PathRole)
            if path:
                self.expanded_folders.discard(path)

            if item.hasChildren():
                # Don't remove if it's already a dummy
                if item.child(0).data(PathRole) is not None:
                    item.removeRows(0, item.rowCount())
                    # Add dummy item back
                    dummy_item = QtGui.QStandardItem()
                    dummy_item.setEditable(False)
                    item.appendRow(dummy_item)

    def handle_drop(self, local_paths: list[str], remote_path: str) -> None:
        """Handle drag and drop operation"""
        if not self.uploader:
            return

        for path in local_paths:
            worker = UploadWorker(self.uploader, path, remote_path)
            worker.progress.connect(self.log_box.appendPlainText)
            worker.finished_.connect(
                lambda success, target=remote_path: self._on_upload_complete(
                    success, target
                )
            )
            self.workers.append(worker)
            worker.start()

    def _on_upload_complete(self, success: bool, target_path: str) -> None:
        """Handle upload completion - refresh views if they're showing the upload destination"""
        if not success:
            return

        # Refresh the detailed view if it's showing the upload destination
        if self.current_remote_path == target_path:
            self.refresh_remote_view(force=True)

        # Refresh the folder tree if the upload destination is expanded
        if target_path in self.expanded_folders:
            self._refresh_expanded_folder(target_path)

    def handle_rename(self, path: str, name: str) -> None:
        """Handle rename request"""
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename", "Enter new name:", text=name
        )
        if not ok or not new_name or new_name == name:
            return

        if not self.remote_fs:
            return

        parent_path = Path(path).parent
        new_path = str(parent_path / new_name)

        try:
            self.remote_fs.rename_path(path, new_path)
            self.log_box.appendPlainText(f"Renamed: {path} -> {new_path}")
            # Refresh parent directory
            if str(parent_path) == self.current_remote_path:
                self.refresh_remote_view(force=True)
            else:
                self.remote_fs.clear_cache(str(parent_path))
        except Exception as e:
            self.log_box.appendPlainText(f"Error renaming {path}: {e}")

    def handle_delete(self, path: str) -> None:
        """Handle delete request"""
        msg_box = QtWidgets.QMessageBox(self)
        msg_box.setWindowTitle("Confirm Delete")
        msg_box.setText(f"Are you sure you want to delete <strong>{path}</strong>?")
        msg_box.setStandardButtons(
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No
        )
        msg_box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        ret = msg_box.exec()

        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        if not self.remote_fs:
            return

        try:
            self.remote_fs.delete_path(path)
            self.log_box.appendPlainText(f"Deleted: {path}")
            # Refresh parent directory
            parent_path = str(Path(path).parent)
            if parent_path == self.current_remote_path:
                self.refresh_remote_view(force=True)
            else:
                self.remote_fs.clear_cache(parent_path)
        except Exception as e:
            self.log_box.appendPlainText(f"Error deleting {path}: {e}")

    def handle_download(self, path: str, is_dir: bool) -> None:
        """Handle download request"""
        if not self.uploader:
            return

        downloads_path = Path.home() / "Downloads"
        downloads_path.mkdir(exist_ok=True)
        dest_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Download to...", str(downloads_path / Path(path).name)
        )
        if not dest_path:
            return

        worker = DownloadWorker(self.uploader, path, dest_path, is_dir)
        worker.progress.connect(self.log_box.appendPlainText)
        worker.finished_.connect(
            lambda success: self.log_box.appendPlainText(
                "Download finished." if success else "Download failed."
            )
        )
        self.workers.append(worker)
        worker.start()

    def add_bookmark(self, path: str) -> None:
        host = self.host_combo.currentText()
        if not host:
            return
        if host not in self.bookmarks:
            self.bookmarks[host] = []
        if path not in self.bookmarks[host]:
            self.bookmarks[host].append(path)
            self._save_bookmarks()
            self._update_bookmark_list()
            self.log_box.appendPlainText(f"Bookmark added: {path}")

    def navigate_to_bookmark(self, path: str) -> None:
        self.path_edit.setText(path)
        self.navigate_to_path()

    def remove_bookmark(self, path: str) -> None:
        host = self.host_combo.currentText()
        if not host or host not in self.bookmarks:
            return
        if path in self.bookmarks[host]:
            self.bookmarks[host].remove(path)
            self._save_bookmarks()
            self._update_bookmark_list()
            self.log_box.appendPlainText(f"Bookmark removed: {path}")

    def navigate_to_path(self) -> None:
        path = self.path_edit.text()
        if path != self.current_remote_path:
            self.current_remote_path = path
            self.refresh_remote_view()
            self._sync_folder_view(path)

    def _sync_folder_view(self, path_to_sync: str) -> None:
        """Expand the folder view to the specified path."""
        if not path_to_sync or path_to_sync == "/":
            return

        parts = Path(path_to_sync).parts
        current_path_str = "/"
        parent_item = self.folder_model.invisibleRootItem()

        for i, part in enumerate(parts):
            if i == 0:  # Skip root "/"
                continue

            current_path_str = str(Path(current_path_str) / part)
            found_item = None
            for row in range(parent_item.rowCount()):
                child_item = parent_item.child(row)
                if child_item and child_item.data(PathRole) == current_path_str:
                    found_item = child_item
                    break

            if found_item:
                index = self.folder_model.indexFromItem(found_item)
                if not self.folder_tree.isExpanded(index):
                    self.folder_tree.expand(index)
                parent_item = found_item
            else:
                # If a part of the path is not found, stop.
                # This can happen if the directory is not yet loaded.
                break

    def go_up_directory(self) -> None:
        """Go up to the parent directory"""
        new_path = str(Path(self.current_remote_path).parent)
        if new_path != self.current_remote_path:
            self.current_remote_path = new_path
            self.path_edit.setText(self.current_remote_path)
            self.refresh_remote_view()
            self._sync_folder_view(new_path)

    def refresh_remote_view(self, force: bool = False) -> None:
        if not self.remote_fs:
            return
        if force:
            self.remote_fs.clear_cache(self.current_remote_path)

        self.status_label.setText("Loading...")
        self.model.removeRows(0, self.model.rowCount())

        worker = RemoteListWorker(self.remote_fs, self.current_remote_path)
        worker.completed.connect(self._on_list_completed)
        worker.failed.connect(self._on_list_failed)
        self.workers.append(worker)
        worker.start()

    def _on_list_completed(self, path: str, items: list[FileSystemItem]) -> None:
        if path != self.current_remote_path:
            return

        self.model.removeRows(0, self.model.rowCount())

        # Disable sorting temporarily to ensure ".." stays first
        self.tree.setSortingEnabled(False)

        if Path(path).parent != Path(path):
            parent_item = QtGui.QStandardItem("..")
            parent_item.setData("parent", TypeRole)
            # Make the ".." item not sortable by prefixing with a character that sorts first
            parent_item.setData(" ..", QtCore.Qt.ItemDataRole.DisplayRole)
            self.model.appendRow(
                [parent_item, QtGui.QStandardItem(""), QtGui.QStandardItem("")]
            )

        for item in items:
            try:
                name_item = QtGui.QStandardItem(item["display_name"])
                name_item.setData(item["name"], PathRole)
                name_item.setData(
                    "folder"
                    if item["is_dir"]
                    else "link"
                    if item["is_link"]
                    else "file",
                    TypeRole,
                )
                name_item.setToolTip(item["display_name"])

                icon_provider = QtWidgets.QFileIconProvider()
                icon = (
                    icon_provider.icon(QtWidgets.QFileIconProvider.IconType.Folder)
                    if item["is_dir"]
                    else self.style().standardIcon(
                        QtWidgets.QStyle.StandardPixmap.SP_FileLinkIcon
                    )
                    if item["is_link"]
                    else icon_provider.icon(QtWidgets.QFileIconProvider.IconType.File)
                )
                name_item.setIcon(icon)

                mtime_item = QtGui.QStandardItem(format_mtime(item["mtime"]))
                size_item = QtGui.QStandardItem(human_size(item["size"]))

                self.model.appendRow([name_item, mtime_item, size_item])
            except Exception as e:
                self.log_box.appendPlainText(
                    f"Error processing item {item.get('name', '')}: {e}"
                )

        # Re-enable sorting - the ".." will stay first due to the space prefix
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, QtCore.Qt.SortOrder.AscendingOrder)

        self.status_label.setText("")
        self.tree.set_current_path(path)
        self.workers = [w for w in self.workers if not w.isFinished()]

    def _on_list_failed(self, error: str) -> None:
        """Handle list failure"""
        self.log_box.appendPlainText(f"Error listing directory: {error}")
        self.status_label.setText("Error!")
        self.workers = [w for w in self.workers if not w.isFinished()]

    def on_host_changed(self, host: str) -> None:
        """Handle host change"""
        if self._initializing_hosts:
            return
        self.model.removeRows(0, self.model.rowCount())
        self.folder_model.removeRows(0, self.folder_model.rowCount())
        self.log_box.clear()
        try:
            self.connect_to_host()
        except Exception as e:
            self.log_box.appendPlainText(f"Failed to connect to {host}: {e}")

    def on_tree_double_click(self, index: QtCore.QModelIndex) -> None:
        """Handle double click on a remote item"""
        try:
            item_type = index.data(TypeRole)
            if item_type in ("folder", "link"):
                name = index.data(PathRole) or index.data(
                    QtCore.Qt.ItemDataRole.DisplayRole
                )
                new_path = str(Path(self.current_remote_path) / name)
                self.current_remote_path = new_path
                self.path_edit.setText(self.current_remote_path)
                self.refresh_remote_view()
                self._sync_folder_view(new_path)
            elif item_type == "parent":
                self.go_up_directory()
        except Exception as e:
            self.log_box.appendPlainText(f"Error navigating: {e}")

    def _build_palette(self) -> None:
        palette = QtGui.QPalette()
        palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor("#0d1017"))
        palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor("#0f1626"))
        palette.setColor(
            QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor("#121826")
        )
        palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor("#e6edf3"))
        palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor("#23c4b8"))
        palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor("#0b111b"))
        palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor("#1b2434"))
        palette.setColor(
            QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#e6edf3")
        )
        self.setPalette(palette)

        self.setStyleSheet(
            """
            QWidget { color: #e6edf3; background: #0d1017; font-family: \"Segoe UI\", \"Helvetica Neue\", sans-serif; font-size: 10pt; }
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

    def _setup_ui(self) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        splitter = QtWidgets.QSplitter()
        splitter.setChildrenCollapsible(False)

        # Sidebar with vertical splitter for resizable bookmarks
        sidebar_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        sidebar_splitter.setChildrenCollapsible(False)

        # Folder tree widget
        folder_widget = QtWidgets.QWidget()
        folder_layout = QtWidgets.QVBoxLayout(folder_widget)
        folder_layout.setContentsMargins(0, 0, 6, 0)
        folder_layout.setSpacing(0)
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
        folder_layout.addWidget(self.folder_tree)
        sidebar_splitter.addWidget(folder_widget)

        # Bookmarks widget
        bookmarks_widget = QtWidgets.QWidget()
        bookmarks_layout = QtWidgets.QVBoxLayout(bookmarks_widget)
        bookmarks_layout.setContentsMargins(0, 0, 6, 0)
        bookmarks_layout.setSpacing(6)
        bookmarks_header = QtWidgets.QLabel("Bookmarks")
        bookmarks_header.setObjectName("muted")
        bookmarks_header.setStyleSheet(
            "padding: 4px 6px; font-weight: 600; letter-spacing: 0.3px;"
        )
        bookmarks_layout.addWidget(bookmarks_header)
        self.bookmark_list = BookmarkList()
        self.bookmark_list.navigateRequested.connect(self.navigate_to_bookmark)
        self.bookmark_list.dropRequested.connect(self.handle_drop)
        self.bookmark_list.removeRequested.connect(self.remove_bookmark)
        bookmarks_layout.addWidget(self.bookmark_list)
        sidebar_splitter.addWidget(bookmarks_widget)

        # Set initial sizes: calculate bookmark height based on minimal or 250px
        total_height = self.height() or 750  # Use default if not set yet
        bookmark_height = max(
            250, int(total_height * 0.15)
        )  # At least 250px or 15% of height
        folder_height = total_height - bookmark_height
        sidebar_splitter.setSizes([folder_height, bookmark_height])

        splitter.addWidget(sidebar_splitter)
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
        up_btn.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ArrowUp)
        )
        up_btn.setToolTip("Up one directory")
        up_btn.clicked.connect(self.go_up_directory)
        path_bar.addWidget(up_btn)
        refresh_btn = QtWidgets.QToolButton()
        refresh_btn.setObjectName("ghost")
        refresh_btn.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_BrowserReload)
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
        self.tree.sortByColumn(0, QtCore.Qt.SortOrder.AscendingOrder)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
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


if __name__ == "__main__":
    import sys

    app = QtWidgets.QApplication(sys.argv)
    window = UploaderWindow()
    window.show()
    sys.exit(app.exec())
