"""
Server page: create, import and run dedicated Minecraft servers.

Layout
------
[ header: Servers | count | + New | Import jar | Open folder | Delete     ]
[ server list (left) | info + [Start] [Stop] + status   (right top)       ]
[                              console output           (right middle)    ]
[                              [ command input ..... ] [Send] (bottom)    ]

Servers live under ``%APPDATA%/Cove/servers/<name>/`` (beside the instances).
The user imports their own jar; :func:`launcher.servers.detect_server_jar`
reports the Minecraft version and whether it supports mods, plugins or both
(dual loaders). The process runs via QProcess with the server folder as its
working directory, so worlds / eula.txt / server.properties land next to the
jar. Only one server runs at a time.
"""

from __future__ import annotations

import os
import subprocess

from PyQt6.QtCore import QProcess, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    ToolButton,
)

from ..java_detector import detect_java_installations
from ..page_scroll import scrollable_page_root
from ..servers import Server, ServerError, ServerRegistry
from .console_page import ConsolePanel


def _build_qss() -> str:
    from ..theme import fg, fg_muted, hairline, is_dark_now

    card_bg = ("rgba(255, 255, 255, 0.05)" if is_dark_now()
               else "rgba(255, 255, 255, 0.6)")
    line = hairline()
    return f"""
QFrame#serverHeader, QFrame#runCard {{ background: {card_bg}; border: 1px solid {line}; border-radius: 12px; }}
QFrame#serverHeader QLabel, QFrame#runCard QLabel {{ background: transparent; border: none; }}
QListWidget {{ background: transparent; border: 1px solid {line}; border-radius: 10px; outline: none; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 8px; }}
QListWidget::item:selected {{ background: rgba(63, 167, 167, 0.30); color: {fg()}; }}
QListWidget::item:hover:!selected {{ background: rgba(128, 128, 128, 0.15); }}
QLabel#serverMeta {{ color: rgba(128,128,128,0.9); background: transparent; }}
QLabel#serverStatus {{ color: rgba(128,128,128,0.9); background: transparent; }}
QLabel#badgeMods {{ color: {fg()}; background: rgba(63, 167, 167, 0.18); border: 1px solid rgba(63, 167, 167, 0.45); border-radius: 8px; padding: 2px 8px; font-size: 11px; font-weight: bold; }}
QLabel#badgePlugins {{ color: {fg()}; background: rgba(120, 200, 120, 0.16); border: 1px solid rgba(120, 200, 120, 0.45); border-radius: 8px; padding: 2px 8px; font-size: 11px; font-weight: bold; }}
QLabel#badgeDual {{ color: {fg()}; background: rgba(230, 180, 60, 0.16); border: 1px solid rgba(230, 180, 60, 0.45); border-radius: 8px; padding: 2px 8px; font-size: 11px; font-weight: bold; }}
QLineEdit {{ background: transparent; border: 1px solid {line}; border-radius: 6px; padding: 7px 10px; color: {fg()}; }}
QLineEdit:focus {{ border: 1px solid #3fa7a7; }}
QLabel#emptyHint {{ color: {fg_muted()}; font-size: 15px; }}
"""


class ServerPage(QWidget):
    """List, inspect, run and command the user's Minecraft servers."""

    selectionChanged = pyqtSignal(object)  # Server | None

    def __init__(self, registry: ServerRegistry, log, toast,
                 parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("serversPage")
        self.setStyleSheet(_build_qss())
        self._registry = registry
        self._log = log
        self._toast = toast
        self._servers: list[Server] = []
        self._process = QProcess(self)
        self._running_name: str | None = None
        self._java_path: str | None = None    # resolved lazily, then cached

        # Content lives in a transparent QScrollArea: the list + run-card row
        # demands ~988px, and without the scroll area Qt would clamp the whole
        # main window wider on show ("boot splash size" bug).
        root = scrollable_page_root(self, margins=(24, 20, 24, 16), spacing=14)

        # -- header card ------------------------------------------------------
        header = QFrame()
        header.setObjectName("serverHeader")
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(16, 12, 12, 12)
        header_lay.setSpacing(10)

        title = SubtitleLabel("Servers")
        title.setStyleSheet("font-size: 20px;")
        header_lay.addWidget(title)

        self.count_label = BodyLabel("0")
        self.count_label.setObjectName("countPill")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_label.setFixedHeight(24)
        self.count_label.setMinimumWidth(28)
        header_lay.addWidget(self.count_label)
        header_lay.addStretch(1)

        self.new_button = ToolButton(FluentIcon.ADD)
        self.new_button.setToolTip("Create a new server")
        self.import_button = ToolButton(FluentIcon.ZIP_FOLDER)
        self.import_button.setToolTip("Import your own server jar")
        self.folder_button = ToolButton(FluentIcon.FOLDER)
        self.folder_button.setToolTip("Open server folder")
        self.edit_button = ToolButton(FluentIcon.SETTING)
        self.edit_button.setToolTip("Edit settings, properties, worlds, files")
        self.delete_button = ToolButton(FluentIcon.DELETE)
        self.delete_button.setToolTip("Delete selected server")
        for button in (self.new_button, self.import_button,
                       self.folder_button, self.edit_button,
                       self.delete_button):
            header_lay.addWidget(button)
        root.addWidget(header)

        # -- body: server list (left) | run card (right) ----------------------
        body = QHBoxLayout()
        body.setSpacing(14)

        self.list_widget = QListWidget()
        self.list_widget.setWordWrap(True)
        self.list_widget.setMinimumWidth(256)
        self.list_widget.setMaximumWidth(300)
        self.list_widget.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.addWidget(self.list_widget)

        right = QVBoxLayout()
        right.setSpacing(10)

        # Run card: detected info + Start/Stop + status ------------------------
        run_card = QFrame()
        run_card.setObjectName("runCard")
        run_lay = QVBoxLayout(run_card)
        run_lay.setContentsMargins(16, 12, 16, 12)
        run_lay.setSpacing(8)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self.name_label = BodyLabel("No server selected")
        font = self.name_label.font()
        font.setPointSize(13)
        font.setBold(True)
        self.name_label.setFont(font)
        name_row.addWidget(self.name_label)
        name_row.addStretch(1)
        self.mods_badge = QLabel("MODS")
        self.mods_badge.setObjectName("badgeMods")
        self.plugins_badge = QLabel("PLUGINS")
        self.plugins_badge.setObjectName("badgePlugins")
        self.dual_badge = QLabel("DUAL LOADER")
        self.dual_badge.setObjectName("badgeDual")
        for badge in (self.mods_badge, self.plugins_badge, self.dual_badge):
            badge.setVisible(False)
            name_row.addWidget(badge)
        run_lay.addLayout(name_row)

        self.meta_label = QLabel("Create a server to get started.")
        self.meta_label.setObjectName("serverMeta")
        self.meta_label.setWordWrap(True)
        run_lay.addWidget(self.meta_label)

        control_row = QHBoxLayout()
        control_row.setSpacing(8)
        self.start_button = PrimaryPushButton(FluentIcon.PLAY, "Start")
        self.stop_button = PushButton("Stop")
        self.stop_button.setEnabled(False)
        self.status_label = QLabel("Stopped")
        self.status_label.setObjectName("serverStatus")
        control_row.addWidget(self.start_button)
        control_row.addWidget(self.stop_button)
        control_row.addStretch(1)
        control_row.addWidget(self.status_label)
        run_lay.addLayout(control_row)
        right.addWidget(run_card)

        # Console --------------------------------------------------------------
        self.console = ConsolePanel()
        self.console.setMinimumHeight(160)
        right.addWidget(self.console, stretch=1)

        # Command input --------------------------------------------------------
        command_row = QHBoxLayout()
        command_row.setSpacing(8)
        self.input = LineEdit()
        self.input.setPlaceholderText(
            "Type a server command (e.g. say hello) and press Enter")
        self.input.setEnabled(False)
        self.send_button = PushButton(FluentIcon.SEND, "Send")
        self.send_button.setEnabled(False)
        command_row.addWidget(self.input, stretch=1)
        command_row.addWidget(self.send_button)
        right.addLayout(command_row)

        body.addLayout(right, stretch=1)

        # Empty state (overlays the list area when there are no servers) -------
        self.empty_label = BodyLabel(
            "No servers yet.\nCreate one with  +  then import a server jar."
        )
        self.empty_label.setObjectName("emptyHint")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        body.addWidget(self.empty_label, stretch=1)

        root.addLayout(body, stretch=1)

        # -- wiring ------------------------------------------------------------
        self.list_widget.currentRowChanged.connect(self._on_selection)
        self.new_button.clicked.connect(self._on_new)
        self.import_button.clicked.connect(self._on_import)
        self.folder_button.clicked.connect(self._on_open_folder)
        self.edit_button.clicked.connect(self._on_edit)
        self.delete_button.clicked.connect(self._on_delete)
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self._on_stop)
        self.send_button.clicked.connect(self._send_command)
        self.input.returnPressed.connect(self._send_command)

        self._process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._on_process_output)
        self._process.finished.connect(self._on_process_finished)
        self._process.errorOccurred.connect(self._on_process_error)

        self.refresh(self._registry.list())

    # -- state -----------------------------------------------------------------
    def refresh(self, servers: list[Server] | None = None) -> None:
        """Rebuild the list from the registry (or an explicit snapshot)."""
        selected = self.current_server()
        self._servers = list(
            self._registry.list() if servers is None else servers)
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for server in self._servers:
            caption = self._caption(server)
            item = QListWidgetItem(f"{server.name}\n{caption}")
            item.setData(Qt.ItemDataRole.UserRole, server.name)
            self.list_widget.addItem(item)
        # Keep the previous selection where possible.
        row = 0
        if selected is not None:
            for i, server in enumerate(self._servers):
                if server.name == selected.name:
                    row = i
                    break
        if self._servers:
            self.list_widget.setCurrentRow(row)
        self.list_widget.blockSignals(False)
        self.count_label.setText(str(len(self._servers)))
        self._set_empty_state(not self._servers)
        self._update_info()

    @staticmethod
    def _caption(server: Server) -> str:
        from ..constants import SERVER_TYPES

        display = SERVER_TYPES.get(
            server.server_type, ("Unknown", False, False))[0]
        if server.jar:
            version = server.mc_version or "?"
            return f"{display} · MC {version}"
        return "No jar imported yet"

    def current_server(self) -> Server | None:
        name = self.current_name()
        if not name:
            return None
        for server in self._servers:
            if server.name == name:
                return server
        return None

    def current_name(self) -> str | None:
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._servers):
            return self._servers[row].name
        return None

    def _set_empty_state(self, empty: bool) -> None:
        self.list_widget.setVisible(not empty)
        self.empty_label.setVisible(empty)
        for button in (self.import_button, self.folder_button,
                       self.edit_button, self.delete_button):
            button.setEnabled(not empty)
        if empty:
            self.name_label.setText("No server selected")
            self.meta_label.setText("Create a server to get started.")
            for badge in (self.mods_badge, self.plugins_badge,
                          self.dual_badge):
                badge.setVisible(False)
            self.start_button.setEnabled(False)

    def _on_selection(self, _row: int) -> None:
        self._update_info()
        self._update_controls()
        try:
            self.selectionChanged.emit(self.current_server())
        except Exception:
            pass

    def _update_info(self) -> None:
        server = self.current_server()
        if server is None:
            self.name_label.setText("No server selected")
            self.meta_label.setText("Create a server to get started.")
            for badge in (self.mods_badge, self.plugins_badge,
                          self.dual_badge):
                badge.setVisible(False)
        else:
            self.name_label.setText(server.name)
            if server.jar:
                from ..constants import SERVER_TYPES
                display = SERVER_TYPES.get(
                    server.server_type, ("Unknown", False, False))[0]
                bits = [f"{display} server"]
                if server.mc_version:
                    bits.insert(0, f"Minecraft {server.mc_version}")
                bits.append(f"jar: {server.jar}")
                self.meta_label.setText("  ·  ".join(bits))
            else:
                self.meta_label.setText(
                    "No jar imported. Use 'Import jar' to add "
                    "server.jar to this folder.")
            self.mods_badge.setVisible(bool(server.supports_mods))
            self.plugins_badge.setVisible(bool(server.supports_plugins))
            self.dual_badge.setVisible(bool(server.is_dual))
        self._update_controls()

    def _update_controls(self) -> None:
        server = self.current_server()
        running = self._running_name
        selected_is_running = (
            server is not None and running == server.name)
        can_start = (
            server is not None
            and bool(server.jar)
            and running is None
        )
        self.start_button.setEnabled(can_start)
        self.stop_button.setEnabled(selected_is_running)
        busy_with = running is not None and running != (
            server.name if server else None)
        self.input.setEnabled(selected_is_running)
        self.send_button.setEnabled(selected_is_running)
        if running is None:
            self.status_label.setText("Stopped")
        elif selected_is_running:
            self.status_label.setText(f"Running ({running})")
        elif busy_with:
            self.status_label.setText(
                f"Another server is running: {running}")
        self.delete_button.setEnabled(
            server is not None and running != server.name
            and bool(self._servers))
        self.edit_button.setEnabled(server is not None)

    # -- actions ----------------------------------------------------------------
    def _on_new(self) -> None:
        # Parent dialogs to the top-level window so the modal mask covers the
        # whole launcher, not just this page (same pattern as the rest).
        from ..dialogs import NewServerDialog

        dialog = NewServerDialog(self.window())
        if not dialog.exec():
            return
        name = dialog.name_edit.text().strip()
        try:
            server = self._registry.create(name)
        except ServerError as exc:
            self._toast("error", "Could not create server", str(exc))
            return
        self.refresh()
        self._select_by_name(server.name)
        self._log(f"Server folder created: {server.dir}")
        self._toast("success", "Server created",
                    f"'{server.name}' is ready. Import a server jar next.")
        self.console.append_log(f"--- Created server '{server.name}' ---")
        self.console.append_log(f"--- Folder: {server.dir} ---")

    def _on_import(self) -> None:
        server = self.current_server()
        if server is None:
            self._toast("warning", "No server",
                        "Create a server first.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import server jar", "",
            "Server jars (*.jar);;All files (*)",
        )
        if not path:
            return
        try:
            info = self._registry.import_jar(server, path)
        except ServerError as exc:
            self._toast("error", "Import failed", str(exc))
            return
        except OSError as exc:
            self._toast("error", "Import failed", str(exc))
            return
        self.refresh()
        self._select_by_name(server.name)
        summary = info.display_name
        if info.mc_version:
            summary = f"{info.mc_version} · {summary}"
        if info.is_dual:
            summary += " — supports mods + plugins"
        elif info.supports_mods:
            summary += " — mods only"
        elif info.supports_plugins:
            summary += " — plugins only"
        else:
            summary += " — could not fully detect"
        self._log(f"Imported {info.filename} for '{server.name}': {summary}")
        self._toast("success", "Server jar imported", summary)
        self.console.append_log(f"--- Imported {info.filename} ---")
        self.console.append_log(f"--- Detected: {summary} ---")

    def _on_open_folder(self) -> None:
        server = self.current_server()
        if server is None:
            return
        os.makedirs(server.dir, exist_ok=True)
        if os.name == "nt":
            subprocess.Popen(["explorer", os.path.normpath(server.dir)])  # noqa: S603
        else:
            import sys
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, server.dir])  # noqa: S603

    def _on_delete(self) -> None:
        server = self.current_server()
        if server is None:
            return
        if self._running_name == server.name:
            self._toast("warning", "Server is running",
                        "Stop the server before deleting it.")
            return
        from ..cove_dialog import CoveConfirm
        if not CoveConfirm.ask(
            self.window(),
            "Delete server",
            f"Delete '{server.name}' and its whole folder?\n"
            f"{server.dir}\nThis cannot be undone.",
            ok="Delete",
        ):
            return
        self._registry.delete(server.name)
        self.refresh()
        self._log(f"Server '{server.name}' deleted.")
        self._toast("success", "Server deleted", server.name)

    def _on_edit(self) -> None:
        server = self.current_server()
        if server is None:
            return
        from ..server_editor import ServerEditorWindow

        editor = ServerEditorWindow(
            server, self._registry,
            running=self._running_name == server.name,
            parent=self.window(),
        )
        # Keep a reference so the window is not garbage-collected.
        self._editor_window = editor
        editor.show()

    def _select_by_name(self, name: str) -> None:
        for i, server in enumerate(self._servers):
            if server.name == name:
                self.list_widget.setCurrentRow(i)
                return

    # -- running the server ------------------------------------------------------
    def _resolve_java(self, server: Server | None = None) -> str:
        """Newest detected JVM, else plain ``java`` (PATH). Cached.

        A per-server custom path (set in the editor) wins over the cache.
        """
        if server is not None and server.java_path.strip():
            return server.java_path.strip()
        if self._java_path:
            return self._java_path
        try:
            found = detect_java_installations()
        except Exception:  # noqa: BLE001 - probe failures must not block start
            found = []
        self._java_path = found[0].path if found else "java"
        return self._java_path

    def _eula_accepted(self, server: Server) -> bool:
        """Check eula.txt on disk; prompt (and write it) when missing/false."""
        eula_path = os.path.join(server.dir, "eula.txt")
        try:
            with open(eula_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.strip().lower().replace(" ", "") == "eula=true":
                        return True
        except OSError:
            pass
        if server.eula_accepted:
            # The flag says yes but the file disagrees/missing: rewrite it.
            self._write_eula(server)
            return True
        from ..cove_dialog import CoveConfirm
        if not CoveConfirm.ask(
            self.window(),
            "Minecraft EULA",
            "Starting a Minecraft server means you accept the Minecraft "
            "EULA:\nhttps://aka.minecraft.net/eula\n\nAccept it for this "
            "server?",
            ok="Accept",
        ):
            return False
        self._write_eula(server)
        return True

    @staticmethod
    def _write_eula(server: Server) -> None:
        os.makedirs(server.dir, exist_ok=True)
        with open(os.path.join(server.dir, "eula.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("eula=true\n")
        server.eula_accepted = True

    def _on_start(self) -> None:
        server = self.current_server()
        if server is None or not server.jar:
            return
        if self._running_name is not None:
            self._toast("warning", "Server already running",
                        f"Stop '{self._running_name}' first.")
            return
        if not os.path.isfile(server.jar_path):
            self._toast("error", "Jar missing",
                        f"{server.jar} is not in the server folder.")
            return
        if not self._eula_accepted(server):
            return
        self._registry.save()   # persist the eula flag if it changed

        java = self._resolve_java(server)
        ram = max(1, int(server.ram_gb or 0))
        extra = list(server.extra_jvm_args or [])
        self._process.setWorkingDirectory(server.dir)
        shown = " ".join([java, f"-Xms{ram}G", f"-Xmx{ram}G",
                          *extra, "-jar", server.jar, "nogui"])
        self.console.append_log(f"> {shown}")
        self._process.start(java, [
            f"-Xms{ram}G", f"-Xmx{ram}G",
            *extra,
            "-jar", server.jar, "nogui",
        ])
        self._running_name = server.name
        self._registry.mark_started(server.name)
        self.status_label.setText(f"Starting ({server.name})...")
        self._update_controls()
        self._log(f"Server '{server.name}' starting.")

    def _on_stop(self) -> None:
        if self._running_name is None:
            return
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self.console.append_log("> stop")
            self._process.write(b"stop\n")
            self.status_label.setText("Stopping...")
            # Grace period, then pull the plug if the server hangs.
            QTimer.singleShot(10_000, self._force_kill)

    def _force_kill(self) -> None:
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self.console.append_log(
                "--- Server did not stop in time; killing it ---")
            self._process.kill()

    def _send_command(self) -> None:
        if self._running_name is None:
            return
        text = self.input.text().strip()
        if not text:
            return
        if self._process.state() == QProcess.ProcessState.NotRunning:
            self._toast("warning", "Not running",
                        "The server process already exited.")
            return
        self.console.append_log(f"> {text}")
        self._process.write(text.encode("utf-8", "replace") + b"\n")
        self.input.clear()

    def _on_process_output(self) -> None:
        data = bytes(self._process.readAllStandardOutput())
        text = data.decode("utf-8", "replace")
        for line in text.splitlines():
            if line.strip():
                self.console.append_log(line)

    def _on_process_finished(self, code: int, _status) -> None:
        name = self._running_name
        self._running_name = None
        self.console.append_log(f"--- Server stopped (exit code {code}) ---")
        self.status_label.setText("Stopped")
        self._update_controls()
        if name:
            self._log(f"Server '{name}' stopped (exit code {code}).")

    def _on_process_error(self, error) -> None:
        # FailedStart etc. — surface it; finished() may not fire on a
        # failed start, so clear the running state here too.
        message = self._process.errorString() or str(error)
        self.console.append_log(f"--- Process error: {message} ---")
        if self._process.state() == QProcess.ProcessState.NotRunning:
            self._running_name = None
            self.status_label.setText("Stopped")
            self._update_controls()
        self._toast("error", "Server failed to start", message)

    def refresh_theme(self) -> None:
        """Re-apply theme-dependent stylesheet on live theme change."""
        self.setStyleSheet(_build_qss())




