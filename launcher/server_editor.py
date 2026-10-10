"""
Server editor window.

A standalone MSFluentWindow opened from the Servers page via the Edit
button. It gives per-server control with four navigation pages:

  * Settings         -- rename, RAM, Java mode (auto/custom + browse/test),
                         custom JVM arguments, EULA state.
  * Server Properties -- full key/value editor for server.properties that
                         preserves comments/ordering on save. Shown with a
                         "run once to generate" hint until the file exists.
  * Worlds           -- list detected world folders (level.dat markers with
                         a safe unconfirmed fallback) with size/date, plus
                         open-folder / rename / delete (blocked while
                         the server is running).
  * Files            -- per-file lists of the mods/ and plugins/ folders
                         (only the sections the server type supports) with
                         delete-per-file and open-folder shortcuts.

Edits are committed through the shared ServerRegistry so the main window
sees them immediately (they take effect on the next start).
"""

from __future__ import annotations

import os
import shutil
import subprocess

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    MSFluentWindow,
    PushButton,
    SubtitleLabel,
    ToolButton,
)

from .constants import (
    DEFAULT_RAM_GB,
    MIN_RAM_GB,
)
from .instances import validate_java_executable
from .servers import (
    Server,
    ServerError,
    ServerRegistry,
    delete_server_file,
    list_server_files,
    list_worlds,
    read_properties,
    rename_world,
    write_properties,
)
from .system_info import get_total_ram_gb

# Each page widget gets a unique objectName so MSFluentWindow.addSubInterface
# registers a distinct navigation routeKey.
_ROOT_QSS = """
QWidget#serverSettingsPage, QWidget#serverPropsPage, QWidget#serverWorldsPage,
QWidget#serverFilesPage { background: transparent; border: none; }
QLabel#infoValue { color: #e6f2f2; }
QLabel#infoKey   { color: #9fb4bd; }
QListWidget { background: transparent; border: none; }
QLabel#emptyHint   { color: #7d939d; font-size: 12px; }
QWidget { background: transparent; border: none; }
QComboBox, QLineEdit, QPushButton { background: transparent; color: #cfd8dc; border: none; border-radius: 5px; padding: 6px 12px; }
QComboBox:hover, QPushButton:hover { border: none; }
QComboBox::drop-down { background: transparent; border: none; }
QComboBox QAbstractItemView { background: transparent; color: #cfd8dc; border: none; selection-background-color: #264f59; }
QScrollBar:vertical { background: transparent; width: 12px; border-radius: 6px; border: none; }
QScrollBar::handle:vertical { background: #264f59; border-radius: 6px; min-height: 20px; border: none; }
QScrollBar::handle:vertical:hover { background: #316371; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


def _fmt_size(num: int) -> str:
    num = max(0, int(num))
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


class ServerEditorWindow(MSFluentWindow):
    """Per-server editor: Settings / Server Properties / Worlds / Files."""

    def __init__(self, server: Server, registry: ServerRegistry,
                 running: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._server = server
        self._registry = registry
        # Fresh copy of the properties file so Save only touches edited keys.
        self._props_raw: list[str] = []
        # World-name -> WorldInfo for the rename/delete actions.
        self._worlds: dict[str, object] = {}
        # Whether the server process is currently running (locks renames).
        self._running = bool(running)

        self.setObjectName("serverEditor")
        self.setWindowTitle(f"Edit server — {server.name}")
        # Configurable in Settings -> Window Sizes (default 900x620).
        from .window_sizes import get_size

        self.resize(*get_size("server_editor"))
        self.setStyleSheet(_ROOT_QSS)
        self._center_on_screen()

        self._build_settings_page()
        self._build_props_page()
        self._build_worlds_page()
        self._build_files_page()
        self._refresh_all()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        # Centre once the real geometry is known (the deferred Fluent layout
        # lands after __init__), then re-centre over the next few frames so
        # the settled layout cannot undo it.
        if not getattr(self, "_cove_centered", False):
            self._cove_centered = True
            self._recenter_when_settled()

    # -- window ------------------------------------------------------------
    def _center_on_screen(self) -> None:
        """Center this window over its parent (or the screen when parentless).

        Uses the size measured AFTER the deferred Fluent layout has settled.
        The old version ran from ``__init__`` before the pages were built and
        used ``self.rect()``, so it centred on a stale size and the window
        appeared pinned toward the left instead of the middle.
        """
        try:
            from PyQt6.QtCore import QRect

            host = (self.parentWidget() if self.parentWidget() is not None
                    else self)
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            area = screen.availableGeometry()
            width, height = self.width(), self.height()
            if width <= 1 or height <= 1:
                width = self.sizeHint().width() or 900
                height = self.sizeHint().height() or 620
            frame = QRect(0, 0, width, height)
            target = (host.geometry().center() if host is not self
                      and host.width() > 1 else area.center())
            frame.moveCenter(target)
            frame.moveLeft(max(area.left(),
                               min(frame.left(),
                                   area.right() - frame.width() + 1)))
            frame.moveTop(max(area.top(),
                              min(frame.top(),
                                  area.bottom() - frame.height() + 1)))
            self.setGeometry(frame)
        except Exception:
            pass  # centring is cosmetic

    def _recenter_when_settled(self) -> None:
        """Re-centre over several frames so the settled layout cannot miss."""
        from PyQt6.QtCore import QTimer

        for delay in (0, 30, 90, 200):
            QTimer.singleShot(delay, self._center_on_screen)

    def _toast(self, severity: str, title: str, content: str = "",
               duration: int = 4000) -> None:
        show = {
            "success": InfoBar.success,
            "warning": InfoBar.warning,
            "error": InfoBar.error,
            "info": InfoBar.info,
        }[severity]
        show(title=title, content=str(content or "")[:200],
             orient=Qt.Orientation.Horizontal, isClosable=True,
             position=InfoBarPosition.TOP_RIGHT, duration=duration,
             parent=self)

    # -- settings ----------------------------------------------------------
    def _build_settings_page(self) -> None:
        page = QWidget()
        page.setObjectName("serverSettingsPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 20, 28, 16)
        lay.setSpacing(12)

        lay.addWidget(SubtitleLabel("Settings"))

        # Name row: same rename-through-registry flow as instances.
        name_row = QHBoxLayout()
        name_row.addWidget(BodyLabel("Name"))
        self.name_edit = LineEdit()
        self.name_edit.setText(self._server.name)
        self.name_edit.setClearButtonEnabled(True)
        name_row.addWidget(self.name_edit, 1)
        self.rename_button = PushButton("Rename")
        self.rename_button.clicked.connect(self._on_rename)
        name_row.addWidget(self.rename_button)
        lay.addLayout(name_row)

        # RAM row: capped at physical memory, floored at MIN_RAM_GB.
        ram_row = QHBoxLayout()
        ram_row.addWidget(BodyLabel("Memory (RAM)"))
        self.ram_combo = ComboBox()
        top = max(MIN_RAM_GB, get_total_ram_gb())
        for gb in range(MIN_RAM_GB, top + 1):
            self.ram_combo.addItem(f"{gb} GB", userData=gb)
        current = int(self._server.ram_gb or DEFAULT_RAM_GB)
        idx = self.ram_combo.findData(current)
        self.ram_combo.setCurrentIndex(max(0, idx))
        ram_row.addWidget(self.ram_combo, 1)
        lay.addLayout(ram_row)

        # Java row: blank = auto-detect (cached newest), else custom path.
        java_row = QHBoxLayout()
        java_row.addWidget(BodyLabel("Java"))
        self.java_path_edit = LineEdit()
        self.java_path_edit.setText(self._server.java_path or "")
        self.java_path_edit.setPlaceholderText("Auto-detect (recommended)")
        self.java_path_edit.setClearButtonEnabled(True)
        java_row.addWidget(self.java_path_edit, 1)
        browse = ToolButton(FluentIcon.FOLDER)
        browse.setToolTip("Browse for a Java executable")
        browse.clicked.connect(self._browse_java)
        java_row.addWidget(browse)
        detect = ToolButton(FluentIcon.SEARCH)
        detect.setToolTip("Auto-detect installed Java versions")
        detect.clicked.connect(self._detect_java)
        java_row.addWidget(detect)
        test = PushButton("Test")
        test.clicked.connect(self._test_java)
        java_row.addWidget(test)
        lay.addLayout(java_row)
        self.java_result_label = CaptionLabel("")
        self.java_result_label.setVisible(False)
        self.java_result_label.setWordWrap(True)
        lay.addWidget(self.java_result_label)

        # JVM args row: split with shlex on save, same as instances.
        jvm_row = QHBoxLayout()
        jvm_row.addWidget(BodyLabel("JVM args"))
        self.jvm_args_edit = LineEdit()
        self.jvm_args_edit.setText(" ".join(self._server.extra_jvm_args or []))
        self.jvm_args_edit.setPlaceholderText("e.g. -XX:+UseG1GC")
        self.jvm_args_edit.setClearButtonEnabled(True)
        jvm_row.addWidget(self.jvm_args_edit, 1)
        lay.addLayout(jvm_row)

        info = CaptionLabel(
            f"Type: {self._server.server_type}"
            f"   ·   MC: {self._server.mc_version or 'unknown'}"
            f"   ·   Jar: {self._server.jar or 'none'}"
            f"   ·   EULA: {'accepted' if self._server.eula_accepted else 'pending'}"
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        lay.addStretch(1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel")
        cancel.clicked.connect(self.close)
        footer.addWidget(cancel)
        save = PushButton("Save")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._on_save_settings)
        footer.addWidget(save)
        lay.addLayout(footer)

        self.addSubInterface(page, FluentIcon.SETTING, "Settings")

    def _ram_value(self) -> int:
        data = self.ram_combo.currentData()
        return int(data) if data else DEFAULT_RAM_GB

    def _browse_java(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Java executable", "",
            "Executables (*.exe);;All files (*)" if os.name == "nt"
            else "All files (*)",
        )
        if path:
            self.java_path_edit.setText(path)

    def _detect_java(self) -> None:
        from .java_detect_dialog import JavaDetectDialog
        dlg = JavaDetectDialog(self.window())
        if dlg.exec() and dlg.selected_path:
            self.java_path_edit.setText(dlg.selected_path)
            self.java_result_label.setText(f"Selected: {dlg.selected_path}")
            self.java_result_label.setVisible(True)

    def _test_java(self) -> None:
        path = self.java_path_edit.text().strip() or shutil.which("java") or ""
        ok, detail = validate_java_executable(path) if path else (False, "No path set")
        self.java_result_label.setText(("OK — " if ok else "FAILED — ") + detail)
        self.java_result_label.setVisible(True)
        self._toast("success" if ok else "error",
                    "Java test", "Java is valid." if ok else detail)

    def gather_jvm_args(self) -> list[str]:
        raw = self.jvm_args_edit.text().strip()
        import shlex
        return shlex.split(raw) if raw else []

    def _on_rename(self) -> None:
        if self._running:
            self._toast("warning", "Server running",
                        "Stop the server before renaming it.")
            return
        new_name = self.name_edit.text().strip()
        try:
            renamed = self._registry.rename(self._server.name, new_name)
        except ServerError as exc:
            self._toast("error", "Rename failed", str(exc))
            return
        self._server = renamed
        self.setWindowTitle(f"Edit server — {renamed.name}")
        self._toast("success", "Renamed", f"Server is now '{renamed.name}'.")

    def _on_save_settings(self) -> None:
        self._server.ram_gb = self._ram_value()
        self._server.java_path = self.java_path_edit.text().strip()
        self._server.extra_jvm_args = self.gather_jvm_args()
        try:
            self._registry.update(self._server)
        except KeyError:
            self._toast("error", "Save failed", "Server no longer exists.")
            return
        self._toast("success", "Saved", "Server settings updated.")
        self.close()

    # -- server.properties -----------------------------------------------
    def _build_props_page(self) -> None:
        page = QWidget()
        page.setObjectName("serverPropsPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 20, 28, 16)
        lay.setSpacing(10)

        lay.addWidget(SubtitleLabel("Server Properties"))

        # Until the first run generates server.properties there is nothing
        # to edit -- show a hint instead of an empty list.
        self.props_hint = BodyLabel(
            "server.properties does not exist yet.\n"
            "Start the server once to generate it, then edit values here.")
        self.props_hint.setObjectName("emptyHint")
        self.props_hint.setWordWrap(True)
        lay.addWidget(self.props_hint)

        # One row per key: label + editable value. Comments/blank lines in
        # the file are preserved on save (they simply have no row here).
        self.props_list = QListWidget()
        self.props_list.setObjectName("propsList")
        lay.addWidget(self.props_list, 1)
        # Keep widget refs so Save can read them back by key.
        self._prop_edits: dict[str, LineEdit] = {}

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        open_file = PushButton("Open file")
        open_file.clicked.connect(self._open_properties_file)
        btn_row.addWidget(open_file)
        save_props = PushButton("Save properties")
        save_props.setObjectName("primaryButton")
        save_props.clicked.connect(self._on_save_properties)
        btn_row.addWidget(save_props)
        lay.addLayout(btn_row)

        self.addSubInterface(page, FluentIcon.DOCUMENT, "Server Properties")

    def _refresh_props(self) -> None:
        entries, raw = read_properties(self._server)
        self._props_raw = raw
        self._prop_edits = {}
        self.props_list.clear()
        has_file = bool(raw)
        self.props_hint.setVisible(not has_file)
        self.props_list.setVisible(has_file)
        for entry in entries:
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(4, 2, 4, 2)
            key_label = BodyLabel(entry.key)
            key_label.setMinimumWidth(220)
            row_lay.addWidget(key_label)
            edit = LineEdit()
            edit.setText(entry.value)
            edit.setClearButtonEnabled(True)
            row_lay.addWidget(edit, 1)
            item = QListWidgetItem(self.props_list)
            item.setSizeHint(QSize(0, 40))
            self.props_list.addItem(item)
            self.props_list.setItemWidget(item, row)
            self._prop_edits[entry.key] = edit

    def _on_save_properties(self) -> None:
        if not self._props_raw:
            self._toast("info", "Nothing to save",
                        "Start the server once to generate server.properties.")
            return
        values = {k: e.text() for k, e in self._prop_edits.items()}
        try:
            write_properties(self._server, self._props_raw, values)
        except OSError as exc:
            self._toast("error", "Save failed", str(exc))
            return
        self._refresh_props()
        self._toast("success", "Saved", "server.properties updated.")

    def _open_properties_file(self) -> None:
        path = os.path.join(self._server.dir, "server.properties")
        if not os.path.isfile(path):
            self._toast("info", "No file yet",
                        "Start the server once to generate it.")
            return
        if os.name == "nt":
            os.startfile(path)  # noqa: S606 -- local file, user action
        else:
            import sys as _sys
            opener = "open" if _sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, path])

    # -- worlds ------------------------------------------------------------
    def _build_worlds_page(self) -> None:
        page = QWidget()
        page.setObjectName("serverWorldsPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 20, 28, 16)
        lay.setSpacing(10)

        lay.addWidget(SubtitleLabel("Worlds"))

        self.worlds_hint = BodyLabel(
            "No worlds yet. Start the server once to generate the default world.")
        self.worlds_hint.setObjectName("emptyHint")
        self.worlds_hint.setWordWrap(True)
        lay.addWidget(self.worlds_hint)

        self.worlds_list = QListWidget()
        self.worlds_list.setObjectName("worldsList")
        lay.addWidget(self.worlds_list, 1)

        btn_row = QHBoxLayout()
        self.world_open_button = PushButton("Open folder")
        self.world_open_button.clicked.connect(self._on_world_open)
        btn_row.addWidget(self.world_open_button)
        self.world_rename_button = PushButton("Rename")
        self.world_rename_button.clicked.connect(self._on_world_rename)
        btn_row.addWidget(self.world_rename_button)
        self.world_delete_button = PushButton("Delete")
        self.world_delete_button.clicked.connect(self._on_world_delete)
        btn_row.addWidget(self.world_delete_button)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.addSubInterface(page, FluentIcon.GAME, "Worlds")

    def _refresh_worlds(self) -> None:
        self._worlds = {w.name: w for w in list_worlds(self._server)}
        self.worlds_list.clear()
        self.worlds_hint.setVisible(not self._worlds)
        self.worlds_list.setVisible(bool(self._worlds))
        for name in sorted(self._worlds):
            w = self._worlds[name]
            stamp = w.last_modified.split("T")[0] if w.last_modified else "?"
            tag = "" if w.confirmed else " (folder?)"
            item = QListWidgetItem(
                f"{name}{tag}  ·  {_fmt_size(w.size_bytes)}  ·  {stamp}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.worlds_list.addItem(item)

    def _selected_world(self) -> str | None:
        item = self.worlds_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_world_open(self) -> None:
        name = self._selected_world()
        if name is None:
            return
        path = self._worlds[name].path
        if os.name == "nt":
            subprocess.Popen(["explorer", os.path.normpath(path)])
        else:
            import sys as _sys
            opener = "open" if _sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, path])

    def _on_world_rename(self) -> None:
        name = self._selected_world()
        if name is None:
            return
        if self._running:
            self._toast("warning", "Server running",
                        "Stop the server before renaming a world.")
            return
        dlg = _RenameWorldDialog(name, self)
        if not dlg.exec() or not dlg.result_name:
            return
        try:
            rename_world(self._server, name, dlg.result_name)
        except ServerError as exc:
            self._toast("error", "Rename failed", str(exc))
            return
        self._refresh_worlds()
        self._toast("success", "Renamed", f"World is now '{dlg.result_name}'.")

    def _on_world_delete(self) -> None:
        name = self._selected_world()
        if name is None:
            return
        if self._running:
            self._toast("warning", "Server running",
                        "Stop the server before deleting a world.")
            return
        box = MessageBox("Delete world?",
                         f"Permanently delete the world '{name}'?\n"
                         "This cannot be undone.", self)
        if not box.exec():
            return
        import shutil as _shutil
        try:
            _shutil.rmtree(self._worlds[name].path, ignore_errors=False)
        except OSError as exc:
            self._toast("error", "Delete failed", str(exc))
            return
        self._refresh_worlds()
        self._toast("success", "Deleted", f"World '{name}' removed.")

    # -- files (mods/ + plugins/) ----------------------------------------
    def _build_files_page(self) -> None:
        page = QWidget()
        page.setObjectName("serverFilesPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 20, 28, 16)
        lay.setSpacing(10)

        lay.addWidget(SubtitleLabel("Files"))

        # Only the sections the server type supports are shown: vanilla
        # shows neither, pure modloaders only mods/, Bukkit only plugins/,
        # dual loaders both.
        self.mods_section = QWidget()
        mods_lay = QVBoxLayout(self.mods_section)
        mods_lay.setContentsMargins(0, 0, 0, 0)
        mods_lay.addWidget(BodyLabel("Mods"))
        self.mods_list = QListWidget()
        self.mods_list.setObjectName("modsList")
        mods_lay.addWidget(self.mods_list)
        mods_btns = QHBoxLayout()
        open_mods = PushButton("Open mods folder")
        open_mods.clicked.connect(lambda: self._open_content_folder("mods"))
        mods_btns.addWidget(open_mods)
        del_mod = PushButton("Delete selected")
        del_mod.clicked.connect(lambda: self._on_delete_content("mods"))
        mods_btns.addWidget(del_mod)
        mods_btns.addStretch(1)
        mods_lay.addLayout(mods_btns)
        lay.addWidget(self.mods_section)

        self.plugins_section = QWidget()
        plugins_lay = QVBoxLayout(self.plugins_section)
        plugins_lay.setContentsMargins(0, 0, 0, 0)
        plugins_lay.addWidget(BodyLabel("Plugins"))
        self.plugins_list = QListWidget()
        self.plugins_list.setObjectName("pluginsList")
        plugins_lay.addWidget(self.plugins_list)
        plugins_btns = QHBoxLayout()
        open_plugins = PushButton("Open plugins folder")
        open_plugins.clicked.connect(lambda: self._open_content_folder("plugins"))
        plugins_btns.addWidget(open_plugins)
        del_plugin = PushButton("Delete selected")
        del_plugin.clicked.connect(lambda: self._on_delete_content("plugins"))
        plugins_btns.addWidget(del_plugin)
        plugins_btns.addStretch(1)
        plugins_lay.addLayout(plugins_btns)
        lay.addWidget(self.plugins_section)

        self.files_hint = BodyLabel(
            "This server type supports neither mods nor plugins.\n"
            "Use the Worlds tab to manage world folders.")
        self.files_hint.setObjectName("emptyHint")
        self.files_hint.setWordWrap(True)
        lay.addWidget(self.files_hint)

        self.addSubInterface(page, FluentIcon.FOLDER, "Files")

    def _refresh_files(self) -> None:
        show_mods = bool(self._server.supports_mods)
        show_plugins = bool(self._server.supports_plugins)
        self.mods_section.setVisible(show_mods)
        self.plugins_section.setVisible(show_plugins)
        self.files_hint.setVisible(not (show_mods or show_plugins))
        if show_mods:
            self.mods_list.clear()
            for name in list_server_files(self._server, "mods"):
                self.mods_list.addItem(QListWidgetItem(name))
        if show_plugins:
            self.plugins_list.clear()
            for name in list_server_files(self._server, "plugins"):
                self.plugins_list.addItem(QListWidgetItem(name))

    def _open_content_folder(self, subdir: str) -> None:
        folder = os.path.join(self._server.dir, subdir)
        os.makedirs(folder, exist_ok=True)
        if os.name == "nt":
            subprocess.Popen(["explorer", os.path.normpath(folder)])
        else:
            import sys as _sys
            opener = "open" if _sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, folder])

    def _on_delete_content(self, subdir: str) -> None:
        lst = self.mods_list if subdir == "mods" else self.plugins_list
        item = lst.currentItem()
        if item is None:
            return
        name = item.text()
        box = MessageBox("Delete file?",
                         f"Permanently delete '{name}' from {subdir}/?\n"
                         "This cannot be undone.", self)
        if not box.exec():
            return
        try:
            delete_server_file(self._server, subdir, name)
        except ServerError as exc:
            self._toast("error", "Delete failed", str(exc))
            return
        self._refresh_files()
        self._toast("success", "Deleted", f"'{name}' removed.")

    # -- refresh -----------------------------------------------------------
    def _refresh_all(self) -> None:
        self._refresh_props()
        self._refresh_worlds()
        self._refresh_files()

    def refresh_server(self, server: Server, running: bool) -> None:
        """Point the editor at a fresh record (e.g. after an outside rename)."""
        self._server = server
        self._running = bool(running)
        self.name_edit.setText(server.name)
        self.setWindowTitle(f"Edit server — {server.name}")
        self._refresh_all()


class _RenameWorldDialog(MessageBox):
    """Small prompt for a new world-folder name (validates on accept)."""

    def __init__(self, current: str, parent=None) -> None:
        super().__init__("Rename world", "", parent)
        self.result_name: str | None = None
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = LineEdit()
        self.edit.setText(current)
        self.edit.setClearButtonEnabled(True)
        lay.addWidget(BodyLabel(f"New name for '{current}':"))
        lay.addWidget(self.edit)
        self.viewLayout.addWidget(body)
        self.yesButton.setText("Rename")
        try:
            self.yesButton.clicked.disconnect()
        except Exception:
            pass
        self.yesButton.clicked.connect(self._on_accept)

    def _on_accept(self) -> None:
        from .servers import validate_name
        try:
            validate_name(self.edit.text())
        except ServerError:
            self.edit.setToolTip("Invalid name.")
            return
        self.result_name = self.edit.text().strip()
        self.accept()
