"""
Instance editor window.

A standalone MSFluentWindow opened from the Instances page via the Edit
button. It gives per-instance control with three navigation pages:

  * Installed Mods   -- list the .jar files in the instance's mods folder
                        with delete-per-file.
  * Settings         -- RAM, Java mode (auto/path/custom + browse/test) and
                        custom JVM arguments that are appended at launch.
  * Installation     -- read-only info card: version, loader, launch id,
                        folder path (copy + open), created/last played,
                        on-disk size and installed runtimes.

Edits are committed through the shared InstanceRegistry so the main window
sees them immediately (they take effect on the next launch).
"""

from __future__ import annotations

import os
import shutil
import subprocess

from PyQt6 import sip
from PyQt6.QtCore import QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    EditableComboBox,
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
    INTERNAL_FILES_DIR,
    JAVA_MODE_AUTO,
    JAVA_MODES,
    MIN_RAM_GB,
    MODLOADERS,
)
from .instances import (
    Instance,
    InstanceError,
    InstanceRegistry,
    build_jvm_arguments,
    dir_size_bytes,
    list_runtimes,
    validate_java_executable,
    validate_name,
)
from .system_info import get_total_ram_gb
from .pages.instances_page import _make_cube_icon
from . import moddb
from . import styles
from . import external_mods

# Each page widget gets a unique objectName so MSFluentWindow.addSubInterface
# registers a distinct navigation routeKey (the method raises if it's empty and
# silently breaks the nav rail if names collide — every page used "editorRoot").
_ROOT_QSS = """
QWidget#instancePage, QWidget#modsPage, QWidget#installPage,
QWidget#stylePage { background: transparent; border: none; }
QLabel#infoValue { color: #e6f2f2; }
QLabel#infoKey   { color: #9fb4bd; }
QListWidget#modList { background: transparent; border: none; }
QListWidget#extFolderList { background: transparent; border: none; }
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
QScrollBar:horizontal { background: transparent; height: 12px; border-radius: 6px; border: none; }
QScrollBar::handle:horizontal { background: #264f59; border-radius: 6px; min-width: 20px; border: none; }
QScrollBar::handle:horizontal:hover { background: #316371; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
"""


class _InstanceStatsThread(QThread):
    """Computes disk size + runtime markers for an instance off the GUI thread.

    ``dir_size_bytes`` stats every file in the instance tree -- measured at
    ~2.5 s on a real 10k-file install, i.e. a hard GUI freeze when it ran
    inline while building the Installation tab.
    """

    ready = pyqtSignal(str, str)  # formatted size, "; "-joined runtime markers

    def __init__(self, instance_dir: str, parent=None) -> None:
        super().__init__(parent)
        self._dir = instance_dir

    def run(self) -> None:
        try:
            size_text = _fmt_size(dir_size_bytes(self._dir))
            runtimes = "; ".join(list_runtimes(self._dir))
        except Exception:  # noqa: BLE001 - never crash the editor
            size_text, runtimes = "unavailable", "unavailable"
        self.ready.emit(size_text, runtimes)


def _fmt_size(bytes_: int) -> str:
    if bytes_ < 1024:
        return f"{bytes_} B"
    if bytes_ < 1024 ** 2:
        return f"{bytes_ / 1024:.1f} KB"
    if bytes_ < 1024 ** 3:
        return f"{bytes_ / (1024 ** 2):.1f} MB"
    return f"{bytes_ / (1024 ** 3):.2f} GB"


def _dismiss_child_modals(window: QWidget) -> None:
    """Force-close any QDialog children of *window* that are in exec().

    QFluentWidgets' MaskDialogBase.done() triggers a 100 ms fade-out
    QPropertyAnimation before it calls QDialog.done(); if the parent window
    is destroyed or hidden while that animation is running, its ``finished``
    signal never fires, leaving the dialog's local event loop spinning
    indefinitely -- causing an application-wide freeze.

    Calling QDialog.done(QDialog.DialogCode.Rejected) directly bypasses the
    animation and immediately unblocks exec().
    """
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QDialog) and widget.isModal() and widget.isVisible():
            # Check whether this dialog is logically owned by our window.
            p = widget.parent()
            while p is not None:
                if p is window:
                    QDialog.done(widget, QDialog.DialogCode.Rejected)
                    break
                p = p.parent()


# --- LiquidGlass theme --------------------------------------------------------
# The bundled LiquidGlass mod ships in launcher/internalfiles and only works on
# NeoForge 1.21.1. The Custom Styles tab exposes a checkbox that installs it
# into (or removes it from) the instance's mods folder; the checkbox state is
# always derived from the folder itself so it stays in sync with reality.
_LIQUIDGLASS_JAR_PREFIX = "liquidglassmc"
_LIQUIDGLASS_MC_VERSION = "1.21.1"
_LIQUIDGLASS_LOADER_ID = "neoforge"


def _liquidglass_source_jar() -> str | None:
    """Path of the bundled LiquidGlass jar in launcher/internalfiles, or None."""
    try:
        for name in sorted(os.listdir(INTERNAL_FILES_DIR)):
            low = name.lower()
            if low.startswith(_LIQUIDGLASS_JAR_PREFIX) and low.endswith(".jar"):
                return os.path.join(INTERNAL_FILES_DIR, name)
    except OSError:
        pass
    return None


def _liquidglass_installed(mods_dir: str) -> str | None:
    """Filename of a LiquidGlass jar inside the instance mods dir, or None."""
    try:
        for name in sorted(os.listdir(mods_dir)):
            low = name.lower()
            if low.startswith(_LIQUIDGLASS_JAR_PREFIX) and low.endswith(".jar"):
                return name
    except OSError:
        pass
    return None


class InstanceEditorWindow(MSFluentWindow):
    """Per-instance editor: Installed Mods / Settings / Installation tabs."""

    def __init__(self, instance: Instance, registry: InstanceRegistry,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{instance.name} — Edit")
        # Configurable in Settings -> Window Sizes (default 900x620).
        from .window_sizes import get_size

        self.resize(*get_size("instance_editor"))
        self._instance = instance
        self._registry = registry
        self.setStyleSheet(_ROOT_QSS)

        # Match the theme the user picked in Settings (light / dark / mica /
        # acrylic) instead of forcing an acrylic/Mica backdrop on this window.
        from . import theme

        theme.apply_theme(theme.current_theme(), self)
        self._theme_applied = False  # re-applied on show (window effect needs it)

        # Create the shared widgets once so the Instance and Settings pages can
        # both display them without duplicating state.
        self._create_widgets()

        # Background worker slots -- declared before any page build so the
        # retire() calls in those builders can safely reference them.
        self._stats_thread = None

        # Build pages (order == nav order). The new "Instance" page is first;
        # runtime settings (RAM/Java/JVM) live there too, so there is no
        # separate Settings tab anymore.
        self._build_instance_page()
        self._install_mods_page()
        self._build_installation_page()
        self._build_custom_style_page()
        self._build_footer()

        # Default to the editable Instance page (name/version immediately visible).
        self.stackedWidget.setCurrentIndex(0)
        # Center after the window is shown (QTimer.singleShot waits until the
        # event loop runs, so MSFluentWindow's deferred layout/resize lands
        # before we read width()/height()).
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._center_on_screen)

    def _center_on_screen(self) -> None:
        """Center this window over its parent (or the screen when parentless).

        Reads the size AFTER the deferred Fluent layout has settled. Calling
        this before the layout runs used to centre using a stale/default
        width+height, so the window appeared pinned to the left edge (and at
        the wrong vertical offset) instead of the middle.
        """
        try:
            from PyQt6.QtCore import QRect

            # Prefer the parent window's frame: an editor should open on top
            # of the launcher, not on some other monitor.
            host = self.parentWidget() if self.parentWidget() is not None else self
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            area = screen.availableGeometry()
            width, height = self.width(), self.height()
            if width <= 1 or height <= 1:
                # Layout has not run yet -- fall back to the requested size.
                width = self.sizeHint().width() or 900
                height = self.sizeHint().height() or 620
            frame = QRect(0, 0, width, height)
            target = (host.geometry().center() if host is not self
                      and host.width() > 1 else area.center())
            frame.moveCenter(target)
            # Keep the whole window on-screen.
            frame.moveLeft(max(area.left(),
                               min(frame.left(),
                                   area.right() - frame.width() + 1)))
            frame.moveTop(max(area.top(),
                              min(frame.top(),
                                  area.bottom() - frame.height() + 1)))
            self.setGeometry(frame)
        except Exception:
            # Centring is cosmetic: never let it abort showing the editor.
            pass

    def _recenter_when_settled(self) -> None:
        """Re-centre across several frames so the final layout cannot miss."""
        from PyQt6.QtCore import QTimer

        for delay in (0, 30, 90, 200):
            QTimer.singleShot(delay, self._center_on_screen)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        # The DWM backdrop can only be applied reliably once the window is
        # being shown, mirroring the main window's behaviour.
        super().showEvent(event)
        from . import theme

        if not self._theme_applied:
            theme.apply_theme(theme.current_theme(), self)
            self._theme_applied = True
        # Re-centre now the real geometry is known, and again over the next
        # few frames so the settled Fluent layout cannot undo it.
        if not getattr(self, "_cove_centered", False):
            self._cove_centered = True
            self._recenter_when_settled()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        # Force-close any Fluent dialog that is currently in exec().
        # MaskDialogBase.done() starts a 100 ms fade-out animation before it
        # calls QDialog.done(); if the parent window is destroyed mid-animation
        # the finished signal never fires and exec() spins forever (freeze).
        # Calling QDialog.done() directly bypasses the animation and unblocks
        # the local event loop before the window tears down.
        _dismiss_child_modals(self)
        # The Installation tab walks the instance tree on a worker thread.
        # Never let that QThread be destroyed mid-run -- Qt aborts the whole
        # process with "QThread: Destroyed while thread is still running".
        # The walk only stats files, so a bounded wait here is safe.
        stats = getattr(self, "_stats_thread", None)
        if stats is not None and stats.isRunning():
            stats.wait(3000)
        super().closeEvent(event)

    # -- shared widget factory ----------------------------------------------
    def _create_widgets(self) -> None:
        """Build the edit widgets once; reused by Instance + Settings pages."""
        inst = self._instance
        max_gb = max(MIN_RAM_GB + 1, get_total_ram_gb())
        self.ram_combo = ComboBox()
        for gb in range(MIN_RAM_GB, max_gb + 1):
            self.ram_combo.addItem(f"{gb} GB", userData=gb)
            if gb == int(inst.ram_gb or DEFAULT_RAM_GB):
                self.ram_combo.setCurrentIndex(self.ram_combo.count() - 1)
        self.java_path_edit = LineEdit()
        self.java_path_edit.setPlaceholderText(
            "Path to java.exe — leave blank to use Mojang's bundled runtime"
        )
        self.java_path_edit.setText(inst.java_path or "")
        self.java_browse = PushButton(FluentIcon.FOLDER, "Browse")
        self.java_test = PushButton(FluentIcon.INFO, "Test")
        self.java_detect = PushButton(FluentIcon.SEARCH, "Detect")
        self.java_detect.setToolTip(
            "Scan this machine for all Java installations"
        )
        self.java_result_label = CaptionLabel("")
        self.jvm_args_edit = LineEdit()
        self.jvm_args_edit.setPlaceholderText(
            "e.g. -Xmx4G -XX:+UseG1GC -Djava.awt.headless=true"
        )
        self.jvm_args_edit.setText(" ".join(inst.extra_jvm_args or []))
        # Identity widgets (Instance page)
        self.name_edit = LineEdit()
        self.name_edit.setText(inst.name)
        self.name_edit.setPlaceholderText("e.g. Survival 1.21")
        self.version_combo = EditableComboBox()
        self.version_combo.addItem(inst.mc_version, userData=inst.mc_version)
        self.version_combo.setPlaceholderText("e.g. 1.21.1")
        self.version_combo.setText(inst.mc_version or "")
        self.loader_combo = ComboBox()
        for lid, label in MODLOADERS:
            self.loader_combo.addItem(label, userData=lid)
        lidx = self.loader_combo.findData(inst.loader_id or "vanilla")
        self.loader_combo.setCurrentIndex(max(0, lidx))
        self.build_combo = EditableComboBox()
        self.build_combo.setPlaceholderText("empty = latest")
        self.build_combo.setText(inst.loader_build or "")
        # Icon picker state ------------------------------------------------
        self.on_saved = None            # window hook: refresh grid on save
        self._pending_icon_path: str | None = inst.icon_path
        self.icon_preview = QLabel()
        self.icon_preview.setObjectName("iconHolder")
        self.icon_preview.setFixedSize(56, 56)
        self.icon_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_choose = PushButton(
            FluentIcon.PHOTO if hasattr(FluentIcon, "PHOTO")
            else (FluentIcon.ALBUM if hasattr(FluentIcon, "ALBUM")
                  else FluentIcon.FOLDER),
            "Choose",
        )
        self.icon_reset = PushButton(FluentIcon.CLOSE, "Reset")

        # Banner picker state (home page hero image) --------------------------
        self._pending_banner_path: str | None = inst.banner_path
        self.banner_preview = QLabel()
        self.banner_preview.setObjectName("bannerHolder")
        self.banner_preview.setFixedSize(240, 80)
        self.banner_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner_choose = PushButton(
            FluentIcon.PHOTO if hasattr(FluentIcon, "PHOTO")
            else (FluentIcon.ALBUM if hasattr(FluentIcon, "ALBUM")
                  else FluentIcon.FOLDER),
            "Choose",
        )
        self.banner_reset = PushButton(FluentIcon.CLOSE, "Remove")

        # Custom style (title screen background) picker state ---------------
        self._pending_bg_png: str | None = inst.custom_bg_png
        self.bg_preview = QLabel()
        self.bg_preview.setObjectName("bgHolder")
        self.bg_preview.setFixedSize(240, 135)
        self.bg_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bg_choose = PushButton(
            FluentIcon.PHOTO if hasattr(FluentIcon, "PHOTO")
            else (FluentIcon.ALBUM if hasattr(FluentIcon, "ALBUM")
                  else FluentIcon.FOLDER),
            "Choose",
        )
        self.bg_reset = PushButton(FluentIcon.CLOSE, "Remove")

        self.bg_mode_combo = ComboBox()
        self.bg_mode_combo.addItem(
            "Static fill", userData=styles.STATIC_MODE
        )
        self.bg_mode_combo.addItem(
            "Panorama", userData=styles.PANORAMA_MODE
        )
        saved_mode = inst.custom_bg_mode if inst.custom_bg_mode in (
            styles.STATIC_MODE, styles.PANORAMA_MODE) else styles.STATIC_MODE
        midx = self.bg_mode_combo.findData(saved_mode)
        self.bg_mode_combo.setCurrentIndex(max(0, midx))

    def _update_icon_preview(self) -> None:
        path = self._pending_icon_path
        pm = QPixmap(56, 56)
        pm.fill(Qt.GlobalColor.transparent)
        src = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        if src.isNull():
            src = _make_cube_icon().pixmap(48, 48)
        scaled = src.scaled(
            52, 52,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(pm)
        painter.drawPixmap((56 - scaled.width()) // 2,
                           (56 - scaled.height()) // 2, scaled)
        painter.end()
        self.icon_preview.setPixmap(pm)

    def _choose_icon(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose instance icon", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All files (*)",
        )
        if not path:
            return
        # Copy into the instance folder so it survives source moves/renames.
        ext = os.path.splitext(path)[1].lower() or ".png"
        os.makedirs(self._instance.dir, exist_ok=True)
        dest = os.path.join(self._instance.dir, f"icon{ext}")
        try:
            shutil.copyfile(path, dest)
        except OSError as exc:
            self._toast("error", "Copy failed", str(exc))
            return
        self._pending_icon_path = dest
        self._update_icon_preview()

    def _reset_icon(self) -> None:
        self._pending_icon_path = None
        self._update_icon_preview()

    def _update_banner_preview(self) -> None:
        path = self._pending_banner_path
        pm = QPixmap(240, 80)
        pm.fill(Qt.GlobalColor.transparent)
        src = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        if src.isNull():
            # Dashed placeholder frame with a hint instead of an image.
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor(128, 128, 128, 120), 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRoundedRect(1, 1, 238, 78, 8, 8)
            p.setPen(QColor(128, 128, 128, 160))
            f = p.font()
            f.setPointSize(8)
            p.setFont(f)
            p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter,
                       "No banner set")
            p.end()
        else:
            scaled = src.scaled(
                240, 80,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter = QPainter(pm)
            painter.drawPixmap((240 - scaled.width()) // 2,
                               (80 - scaled.height()) // 2, scaled)
            painter.end()
        self.banner_preview.setPixmap(pm)

    def _choose_banner(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose instance banner", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All files (*)",
        )
        if not path:
            return
        # Copy into the instance folder so it survives source moves/renames.
        ext = os.path.splitext(path)[1].lower() or ".png"
        os.makedirs(self._instance.dir, exist_ok=True)
        dest = os.path.join(self._instance.dir, f"banner{ext}")
        try:
            shutil.copyfile(path, dest)
        except OSError as exc:
            self._toast("error", "Copy failed", str(exc))
            return
        self._pending_banner_path = dest
        self._update_banner_preview()

    def _reset_banner(self) -> None:
        self._pending_banner_path = None
        self._update_banner_preview()

    # ------------------------------------------------------- instance page --
    def _build_instance_page(self) -> None:
        """Editable front page: name, version, loader, build + shared settings."""
        page = QWidget()
        page.setObjectName("instancePage")
        self._instance_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(7)

        # Icon + banner pickers side by side ---------------------------------
        media_row = QHBoxLayout()
        media_row.setSpacing(12)

        icon_col = QVBoxLayout()
        icon_col.setSpacing(4)
        icon_col.addWidget(BodyLabel("Icon"))
        icon_row = QHBoxLayout()
        icon_row.addWidget(self.icon_preview)
        icon_btns = QVBoxLayout()
        icon_btns.setSpacing(4)
        icon_btns.addWidget(self.icon_choose)
        icon_btns.addWidget(self.icon_reset)
        icon_btns.addStretch(1)
        icon_row.addLayout(icon_btns, stretch=1)
        icon_col.addLayout(icon_row)
        icon_col.addStretch(1)
        media_row.addLayout(icon_col)

        banner_col = QVBoxLayout()
        banner_col.setSpacing(4)
        banner_col.addWidget(BodyLabel("Banner"))
        banner_row = QHBoxLayout()
        banner_row.addWidget(self.banner_preview)
        banner_btns = QVBoxLayout()
        banner_btns.setSpacing(4)
        banner_btns.addWidget(self.banner_choose)
        banner_btns.addWidget(self.banner_reset)
        banner_btns.addStretch(1)
        banner_row.addLayout(banner_btns, stretch=1)
        banner_col.addLayout(banner_row)
        banner_col.addStretch(1)
        media_row.addLayout(banner_col, stretch=1)
        lay.addLayout(media_row)

        # Identity: name + game version share a row ---------------------------
        ident_row = QHBoxLayout()
        ident_row.setSpacing(10)
        name_col = QVBoxLayout()
        name_col.setSpacing(4)
        name_col.addWidget(BodyLabel("Name"))
        name_col.addWidget(self.name_edit)
        ident_row.addLayout(name_col, stretch=1)
        version_col = QVBoxLayout()
        version_col.setSpacing(4)
        version_col.addWidget(BodyLabel("Game version"))
        version_col.addWidget(self.version_combo)
        ident_row.addLayout(version_col, stretch=1)
        lay.addLayout(ident_row)

        # Loader + build -------------------------------------------------------
        row = QHBoxLayout()
        row.setSpacing(10)
        col1 = QVBoxLayout()
        col1.setSpacing(4)
        col1.addWidget(BodyLabel("Mod loader"))
        col1.addWidget(self.loader_combo)
        col2 = QVBoxLayout()
        col2.setSpacing(4)
        col2.addWidget(BodyLabel("Loader build"))
        col2.addWidget(self.build_combo)
        row.addLayout(col1, stretch=1)
        row.addLayout(col2, stretch=1)
        lay.addLayout(row)

        # RAM + JVM args share a row -------------------------------------------
        perf_row = QHBoxLayout()
        perf_row.setSpacing(10)
        ram_col = QVBoxLayout()
        ram_col.setSpacing(4)
        ram_col.addWidget(BodyLabel("Allocated RAM"))
        ram_col.addWidget(self.ram_combo)
        perf_row.addLayout(ram_col)
        jvm_col = QVBoxLayout()
        jvm_col.setSpacing(4)
        jvm_col.addWidget(BodyLabel("Custom JVM arguments"))
        jvm_col.addWidget(self.jvm_args_edit)
        perf_row.addLayout(jvm_col, stretch=1)
        lay.addLayout(perf_row)

        # Java runtime -----------------------------------------------------------
        java_col = QVBoxLayout()
        java_col.setSpacing(4)
        java_col.addWidget(BodyLabel("Java runtime"))
        java_row = QHBoxLayout()
        java_row.setSpacing(6)
        java_row.addWidget(self.java_path_edit, stretch=1)
        java_row.addWidget(self.java_detect)
        java_row.addWidget(self.java_browse)
        java_row.addWidget(self.java_test)
        java_col.addLayout(java_row)
        self.java_result_label.setVisible(False)   # only when it has content
        java_col.addWidget(self.java_result_label)
        lay.addLayout(java_col)

        lay.addStretch(1)

        self.java_browse.clicked.connect(self._browse_java)
        self.java_test.clicked.connect(self._test_java)
        self.java_detect.clicked.connect(self._detect_java)
        self.icon_choose.clicked.connect(self._choose_icon)
        self.icon_reset.clicked.connect(self._reset_icon)
        self._update_icon_preview()
        self.banner_choose.clicked.connect(self._choose_banner)
        self.banner_reset.clicked.connect(self._reset_banner)
        self._update_banner_preview()
        self.addSubInterface(page, FluentIcon.EDIT, "Instance")

    # ------------------------------------------------------------------ pages --
    def _install_mods_page(self) -> None:
        page = QWidget()
        page.setObjectName("modsPage")
        self._mods_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(12)

        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("Installed mods"))
        header.addStretch(1)
        self.refresh_mods_button = ToolButton(FluentIcon.SYNC)
        self.refresh_mods_button.setToolTip("Refresh mods list")
        header.addWidget(self.refresh_mods_button)
        lay.addLayout(header)

        self.mod_list = QListWidget()
        self.mod_list.setObjectName("modList")
        self.mod_list.setIconSize(QSize(32, 32))
        self.mod_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection
        )
        lay.addWidget(self.mod_list, stretch=1)

        # -- external mods folders -------------------------------------------
        # Lets the user attach a folder of jars from elsewhere on disk. The
        # files are exposed into mods/ as if they had been installed normally
        # (see launcher/external_mods.py for why that indirection is needed).
        ext_box = QWidget()
        ext_lay = QVBoxLayout(ext_box)
        ext_lay.setContentsMargins(0, 0, 0, 0)
        ext_lay.setSpacing(5)

        ext_head = QHBoxLayout()
        ext_head.addWidget(BodyLabel("External mods folders"))
        ext_head.addStretch(1)
        ext_lay.addLayout(ext_head)

        # Sized to its contents in refresh_external_folders() and hidden while
        # empty, so an unused box doesn't leave a gap above the buttons.
        self.ext_list = QListWidget()
        self.ext_list.setObjectName("extFolderList")
        ext_lay.addWidget(self.ext_list)

        self.ext_hint = CaptionLabel(
            "Jars from these folders are used as if they were in this "
            "instance's mods folder."
        )
        self.ext_hint.setObjectName("emptyHint")
        ext_lay.addWidget(self.ext_hint)

        # Actions live below the list so they read as "apply to what's listed".
        ext_actions = QHBoxLayout()
        ext_actions.addStretch(1)
        self.ext_add_button = PushButton(FluentIcon.FOLDER, "Add folder")
        self.ext_remove_button = PushButton(FluentIcon.REMOVE, "Remove")
        self.ext_open_button = PushButton(FluentIcon.FOLDER, "Open")
        self.ext_remove_button.setEnabled(False)
        self.ext_open_button.setEnabled(False)
        ext_actions.addWidget(self.ext_add_button)
        ext_actions.addWidget(self.ext_remove_button)
        ext_actions.addWidget(self.ext_open_button)
        ext_lay.addLayout(ext_actions)

        lay.addWidget(ext_box)

        # -- sync mods with server ------------------------------------------
        # Push this instance's mods/ jars into a server's mods/ folder using
        # the same symlink -> hardlink -> copy method as Add Folder above
        # (see launcher/server_mods.py). Pick a server, Sync now.
        srv_box = QWidget()
        srv_lay = QVBoxLayout(srv_box)
        srv_lay.setContentsMargins(0, 0, 0, 0)
        srv_lay.setSpacing(5)

        srv_head = QHBoxLayout()
        srv_head.addWidget(BodyLabel("Sync mods with server"))
        srv_head.addStretch(1)
        srv_lay.addLayout(srv_head)

        srv_pick = QHBoxLayout()
        self.sync_server_combo = ComboBox()
        self.sync_server_combo.setMinimumWidth(200)
        self.sync_server_combo.setPlaceholderText("Select a server")
        srv_pick.addWidget(self.sync_server_combo, stretch=1)
        srv_lay.addLayout(srv_pick)

        self.sync_status = CaptionLabel("Not linked.")
        self.sync_status.setObjectName("emptyHint")
        self.sync_status.setWordWrap(True)
        srv_lay.addWidget(self.sync_status)

        srv_actions = QHBoxLayout()
        srv_actions.addStretch(1)
        self.sync_now_button = PushButton(FluentIcon.SYNC, "Sync now")
        self.sync_unlink_button = PushButton(FluentIcon.CLOSE, "Unlink")
        self.sync_open_button = PushButton(FluentIcon.FOLDER, "Open")
        srv_actions.addWidget(self.sync_now_button)
        srv_actions.addWidget(self.sync_unlink_button)
        srv_actions.addWidget(self.sync_open_button)
        srv_lay.addLayout(srv_actions)

        lay.addWidget(srv_box)

        path_row = QHBoxLayout()
        self.mods_path_label = CaptionLabel(self._instance.mods_dir)
        self.mods_path_label.setToolTip(self._instance.mods_dir)
        # Let the layout shrink the label (text clips; full path in tooltip).
        from PyQt6.QtWidgets import QSizePolicy
        self.mods_path_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        path_row.addWidget(self.mods_path_label, stretch=1)
        self.open_mods_button = PushButton(FluentIcon.FOLDER, "Open")
        path_row.addWidget(self.open_mods_button)
        lay.addLayout(path_row)

        self.refresh_mods()
        self.refresh_external_folders()
        self.refresh_sync_servers()
        self.open_mods_button.clicked.connect(self._open_mods_folder)
        self.refresh_mods_button.clicked.connect(self.refresh_mods)
        self.delete_mod_button = ToolButton(FluentIcon.DELETE)
        self.delete_mod_button.setToolTip("Delete selected mod")
        path_row.addWidget(self.delete_mod_button)
        self.delete_mod_button.clicked.connect(self._delete_selected_mod)
        self.mod_list.itemDoubleClicked.connect(lambda _it: None)  # reserved

        self.ext_add_button.clicked.connect(self._add_external_folder)
        self.ext_remove_button.clicked.connect(self._remove_external_folder)
        self.ext_open_button.clicked.connect(self._open_external_folder)
        self.ext_list.itemSelectionChanged.connect(
            self._on_external_folder_selected
        )
        self.sync_now_button.clicked.connect(self._on_sync_now)
        self.sync_unlink_button.clicked.connect(self._on_sync_unlink)
        self.sync_open_button.clicked.connect(self._open_synced_server)
        self.sync_server_combo.currentIndexChanged.connect(
            self._on_sync_server_changed
        )

        self.addSubInterface(page, FluentIcon.APP_FOLDER if hasattr(FluentIcon, "APP_FOLDER") else FluentIcon.FOLDER, "Installed Mods")

    def refresh_mods(self) -> None:
        self.mod_list.clear()
        files = moddb.list_installed_mods(self._instance.mods_dir)
        # Exposed external jars, so the tooltip can say where they really live
        # and what deleting them here would actually do.
        external = external_mods.managed_entries(self._instance)
        if not files:
            item = QListWidgetItem("No mods installed yet.")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.mod_list.addItem(item)
            return
        for name in files:
            # Prefer the human-friendly title from embedded metadata; keep the
            # actual jar filename as a suffix so it stays identifiable.
            title = moddb.get_mod_display_name(self._instance.mods_dir, name)
            entry = external.get(name)
            label = f"{title}   ({name})" if title else name
            if entry is not None:
                label = f"{label}   [external]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            ext_tip = ""
            if entry is not None:
                ext_tip = (f"\n\nExternal mod\nSource: {entry.source}\n"
                           f"Mode: {entry.describe()}"
                           "\n\nDeleting here only unlinks it from this "
                           "instance; the original file is kept.")
            icon_bytes = moddb.get_mod_icon_bytes(self._instance.mods_dir, name)
            if icon_bytes:
                pm = QPixmap()
                if pm.loadFromData(icon_bytes):
                    item.setIcon(QIcon(pm))
                    item.setToolTip(
                        f"{title or name}\n{name}\n"
                        "Double-click a mod on the Mods page for details."
                        + ext_tip
                    )
                else:
                    item.setToolTip(name + ext_tip)
            else:
                item.setToolTip(name + ext_tip)
            self.mod_list.addItem(item)

    def _delete_selected_mod(self) -> None:
        from PyQt6.QtCore import QTimer

        item = self.mod_list.currentItem()
        if item is None or not item.data(Qt.ItemDataRole.UserRole):
            return
        name = item.data(Qt.ItemDataRole.UserRole)

        def _perform() -> None:
            # Hide the mod list so the dialog's Fluent fade effect can't grab
            # a pixmap of it mid-repaint (which causes "QPainter not active").
            self.mod_list.setVisible(False)
            try:
                entry = external_mods.source_for(self._instance, name)
                if entry is not None:
                    # Externally managed: unlink it from this instance only.
                    # The user's file in the other folder must survive.
                    box = MessageBox(
                        "Remove external mod",
                        f"Remove '{name}' from this instance?\n\n"
                        "It will be unlinked from the mods folder. "
                        f"Your original file is kept at:\n{entry.source}",
                        self.window(),
                    )
                    if not box.exec():
                        return
                    try:
                        external_mods.remove_entry(self._instance, name)
                    except OSError as exc:  # noqa: BLE001
                        self._toast("error", "Remove failed", str(exc))
                        return
                    self._toast(
                        "success", "External mod removed",
                        "Your original file was not deleted.",
                    )
                    self.refresh_mods()
                    return
                # Parent to self.window() (top-level) so the dialog is a
                # sibling of the editor, not a child of the mod list page.
                box = MessageBox(
                    "Delete mod",
                    f"Delete '{name}' from this instance?",
                    self.window(),
                )
                if not box.exec():
                    return
                try:
                    moddb.remove_mod(self._instance.mods_dir, name)
                except Exception as exc:  # noqa: BLE001
                    self._toast("error", "Delete failed", str(exc))
                    return
                self._toast("success", "Mod deleted", name)
                self.refresh_mods()
            finally:
                # Always re-show the list, even if refresh_mods() raised.
                self.mod_list.setVisible(True)

        # Defer the whole action to the next idle tick so the click/mouse-release
        # cycle completes before the modal opens.
        QTimer.singleShot(0, _perform)

    # ------------------------------------------------------ external folders --
    def refresh_external_folders(self) -> None:
        """Rebuild the external folder list (path, jar count, link mode)."""
        if not hasattr(self, "ext_list"):
            return
        self.ext_list.blockSignals(True)
        self.ext_list.clear()
        methods = external_mods.folder_methods(self._instance)
        for folder in external_mods.normalise_folders(self._instance):
            item = QListWidgetItem(self._describe_external_folder(
                folder, methods.get(folder) or set()
            ))
            item.setToolTip(folder)
            item.setData(Qt.ItemDataRole.UserRole, folder)
            self.ext_list.addItem(item)
        self.ext_list.blockSignals(False)
        # Collapse the list when empty (and cap it at a few visible rows) so
        # the buttons below sit right under the last entry with no dead space.
        count = self.ext_list.count()
        self.ext_list.setVisible(count > 0)
        if count:
            row_h = self.ext_list.sizeHintForRow(0) or 24
            visible_rows = min(count, 3)
            self.ext_list.setFixedHeight(
                visible_rows * row_h + 2 * self.ext_list.frameWidth() + 4
            )
        else:
            self.ext_list.setFixedHeight(0)
        self.ext_hint.setVisible(count > 0)
        self._on_external_folder_selected()

    @staticmethod
    def _describe_external_folder(folder: str, methods: set) -> str:
        """One-line summary: path, how many jars, and how they are exposed."""
        if not os.path.isdir(folder):
            return f"{folder}  (missing)"
        count = len(external_mods.list_jars(folder))
        noun = "jar" if count == 1 else "jars"
        # Tell the user whether edits show up live or on the next launch.
        mode = ", ".join(sorted(
            external_mods.Entry("", "", m).describe() for m in methods
        )) or "—"
        return f"{folder}  ({count} {noun}, {mode})"

    def _on_external_folder_selected(self) -> None:
        has_selection = self.ext_list.currentItem() is not None
        self.ext_remove_button.setEnabled(has_selection)
        self.ext_open_button.setEnabled(has_selection)

    def _selected_external_folder(self) -> str:
        item = self.ext_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _add_external_folder(self) -> None:
        from PyQt6.QtCore import QTimer

        def _perform() -> None:
            folder = QFileDialog.getExistingDirectory(
                self, "Select a folder of mod jars", "",
            )
            if not folder:
                return
            try:
                # add_folder() validates, registers and syncs in one step.
                external_mods.add_folder(self._instance, folder)
            except Exception as exc:  # noqa: BLE001
                self._toast("error", "Cannot add folder", str(exc))
                return
            self.refresh_external_folders()
            self.refresh_mods()
            count = len(external_mods.list_jars(folder))
            noun = "jar" if count == 1 else "jars"
            self._toast(
                "success", "External folder added",
                f"{count} {noun} now available to this instance.",
            )

        # Same deferred pattern as the other modal actions, so the click and
        # mouse-release cycle finishes before the native dialog opens.
        QTimer.singleShot(0, _perform)

    def _remove_external_folder(self) -> None:
        from PyQt6.QtCore import QTimer

        def _perform() -> None:
            folder = self._selected_external_folder()
            if not folder:
                return
            self.ext_list.setVisible(False)
            try:
                box = MessageBox(
                    "Remove external folder",
                    f"Stop using mods from this folder?\n\n{folder}\n\n"
                    "Its jars will be unlinked from this instance. "
                    "The files in that folder are NOT deleted.",
                    self.window(),
                )
                if not box.exec():
                    return
                result = external_mods.remove_folder(self._instance, folder)
                self.refresh_external_folders()
                self.refresh_mods()
                self._toast(
                    "success", "External folder removed",
                    f"{result.removed} file(s) unlinked; originals kept.",
                )
            finally:
                # Re-show via refresh so the list stays collapsed when empty
                # (a bare setVisible(True) would leave a gap behind).
                self.ext_list.setVisible(True)
                self.refresh_external_folders()

        QTimer.singleShot(0, _perform)

    def _open_external_folder(self) -> None:
        folder = self._selected_external_folder()
        if not folder or not os.path.isdir(folder):
            self._toast("warning", "Folder unavailable",
                        "That folder no longer exists.")
            return
        if os.name == "nt":
            subprocess.Popen(["explorer", os.path.normpath(folder)])
        else:
            import sys as _sys

            opener = "open" if _sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, folder])


    def _open_mods_folder(self) -> None:
        os.makedirs(self._instance.mods_dir, exist_ok=True)
        if os.name == "nt":
            subprocess.Popen(["explorer", os.path.normpath(self._instance.mods_dir)])
        else:
            import sys as _sys

            opener = "open" if _sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, self._instance.mods_dir])

    # ------------------------------------------------- sync with server --
    def _sync_servers(self) -> list:
        """All servers from the shared registry (or a fresh one)."""
        window = self.window()
        reg = getattr(window, "server_registry", None)
        if reg is not None and hasattr(reg, "list"):
            try:
                return list(reg.list())
            except Exception:
                pass
        try:
            from .servers import ServerRegistry
            return list(ServerRegistry().list())
        except Exception:
            return []

    def _selected_sync_server(self):
        name = self.sync_server_combo.currentData()
        if not name:
            return None
        return next((s for s in self._sync_servers() if s.name == name), None)

    def refresh_sync_servers(self) -> None:
        """Refill the server picker; pre-select the already-linked server."""
        from . import server_mods
        servers = self._sync_servers()
        keep = self.sync_server_combo.currentData()
        self.sync_server_combo.blockSignals(True)
        self.sync_server_combo.clear()
        for srv in servers:
            label = srv.name
            try:
                if not srv.supports_mods:
                    label = f"{srv.name} (no mods)"
            except AttributeError:
                pass
            self.sync_server_combo.addItem(label, userData=srv.name)
        # Pre-select: explicit choice wins, else the server linked to us.
        target = keep
        if target is None:
            for srv in servers:
                try:
                    if server_mods.linked_instance(srv) == self._instance.name:
                        target = srv.name
                        break
                except Exception:
                    pass
        if target is not None:
            idx = self.sync_server_combo.findData(target)
            if idx >= 0:
                self.sync_server_combo.setCurrentIndex(idx)
        self.sync_server_combo.blockSignals(False)
        self._update_sync_status()

    def _on_sync_server_changed(self, _index: int) -> None:
        self._update_sync_status()

    def _update_sync_status(self) -> None:
        from . import server_mods
        srv = self._selected_sync_server()
        if srv is None:
            if self.sync_server_combo.count() == 0:
                self.sync_status.setText("No servers yet -- create one first.")
            else:
                self.sync_status.setText("Select a server above.")
            return
        linked = server_mods.linked_instance(srv)
        detail = server_mods.status(srv)
        if linked == self._instance.name:
            self.sync_status.setText(f"Linked: {detail}")
        elif linked:
            self.sync_status.setText(
                f"'{srv.name}' syncs from '{linked}'. {detail}")
        else:
            self.sync_status.setText(
                f"'{srv.name}' is not linked. Hit Sync now to push "
                f"{len(server_mods.list_jars(self._instance.mods_dir))} "
                f"mod(s).")

    def _on_sync_now(self) -> None:
        from . import server_mods
        srv = self._selected_sync_server()
        if srv is None:
            self._toast("warning", "No server selected",
                        "Pick a server first.")
            return
        try:
            result = server_mods.link(srv, self._instance)
        except Exception as exc:  # noqa: BLE001 -- user-facing
            self._toast("error", "Sync failed", str(exc))
            self.sync_status.setText(str(exc))
            return
        self.refresh_mods()
        self._update_sync_status()
        self._toast("success", "Mods synced",
                    f"'{self._instance.name}' -> '{srv.name}': "
                    f"{result.summary()}")

    def _on_sync_unlink(self) -> None:
        from . import server_mods
        srv = self._selected_sync_server()
        if srv is None:
            self._toast("warning", "No server selected",
                        "Pick a server first.")
            return
        result = server_mods.unlink(srv)
        self._update_sync_status()
        self._toast("info", "Server unlinked", result.summary())

    def _open_synced_server(self) -> None:
        srv = self._selected_sync_server()
        if srv is None:
            self._toast("warning", "No server selected",
                        "Pick a server first.")
            return
        try:
            os.makedirs(srv.mods_dir, exist_ok=True)
        except OSError as exc:
            self._toast("error", "Cannot open folder", str(exc))
            return
        if os.name == "nt":
            subprocess.Popen(["explorer", os.path.normpath(srv.mods_dir)])
        else:
            import sys as _sys
            opener = "open" if _sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, srv.mods_dir])

    def _ram_value(self) -> int:
        return int(self._instance.ram_gb or DEFAULT_RAM_GB)

    def _browse_java(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Java executable", "",
            "Executables (*.exe);;All files (*)" if os.name == "nt"
            else "All files (*)",
        )
        if path:
            self.java_path_edit.setText(path)

    def _detect_java(self) -> None:
        """Open the Java detection dialog and fill the path on selection."""
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

# --------------------------------------------------------------- install --
    def _build_installation_page(self) -> None:
        page = QWidget()
        page.setObjectName("installPage")
        self._install_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(10)

        lay.addWidget(SubtitleLabel("Installation"))

        inst = self._instance
        loader = "Vanilla" if inst.loader_id == "vanilla" else inst.loader_id.capitalize()
        build = inst.loader_build or "latest (auto)"
        launch = inst.launch_version_id or inst.mc_version

        # Disk size and runtimes are filled in by _InstanceStatsThread: walking
        # an instance tree means stat()ing every file (seconds on a real
        # install), which must never happen on the GUI thread.
        rows = [
            ("Name", inst.name),
            ("Game version", f"{inst.mc_version}  ({loader}, {build})"),
            ("Launch version", launch),
            ("Instance folder", inst.dir),
            ("Created", inst.created.replace("T", "  ")[:19]),
            ("Last played", (inst.last_played or "never")[:19]
             .replace("T", " ") if inst.last_played else "never"),
        ]
        for key, value in rows:
            k = QLabel(key)
            k.setObjectName("infoKey")
            v = QLabel(str(value))
            v.setObjectName("infoValue")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(k)
            lay.addWidget(v)

        # Placeholders that the worker fills in when it reports back.
        self._stats_labels: dict[str, QLabel] = {}
        for key in ("Disk size", "Runtimes"):
            k = QLabel(key)
            k.setObjectName("infoKey")
            v = QLabel("calculating…")
            v.setObjectName("infoValue")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._stats_labels[key] = v
            lay.addWidget(k)
            lay.addWidget(v)

        from .threading_utils import retire
        retire(self._stats_thread)
        self._stats_thread = _InstanceStatsThread(inst.dir, parent=self)
        self._stats_thread.ready.connect(self._on_instance_stats)
        self._stats_thread.finished.connect(self._on_stats_thread_done)
        self._stats_thread.start()

        actions = QHBoxLayout()
        self.copy_path_button = PushButton(FluentIcon.COPY, "Copy path")
        self.open_install_button = PushButton(FluentIcon.FOLDER, "Open folder")
        actions.addStretch(1)
        actions.addWidget(self.copy_path_button)
        actions.addWidget(self.open_install_button)
        lay.addLayout(actions)
        lay.addStretch(1)

        self.copy_path_button.clicked.connect(self._copy_path)
        self.open_install_button.clicked.connect(self._open_instance_folder)
        self.addSubInterface(page, FluentIcon.INFO, "Installation")

    def _on_instance_stats(self, size_text: str, runtimes: str) -> None:
        """Fill the Disk size / Runtimes rows once the worker reports back."""
        if sip.isdeleted(self):
            return
        labels = getattr(self, "_stats_labels", None)
        if not labels:
            return
        if "Disk size" in labels:
            labels["Disk size"].setText(size_text)
        if "Runtimes" in labels:
            labels["Runtimes"].setText(runtimes)

    def _on_stats_thread_done(self) -> None:
        # Drop the reference so a finished QThread isn't garbage collected
        # while it is still finishing up.
        from .threading_utils import retire
        retire(self._stats_thread)
        self._stats_thread = None

    # ------------------------------------------------------- custom styles --
    def _build_custom_style_page(self) -> None:
        """Custom Styles tab: per-instance title-screen background image."""
        page = QWidget()
        page.setObjectName("stylePage")
        self._style_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(12)

        lay.addWidget(SubtitleLabel("Custom styles"))
        desc = BodyLabel(
            "Pick an image to use as the Minecraft title-screen background. "
            "Cove installs it as a tiny built-in resource pack that "
            "replaces the menu panorama - no mods needed, and it affects "
            "this instance only."
        )
        desc.setWordWrap(True)
        lay.addWidget(desc)

        row = QHBoxLayout()
        row.addWidget(self.bg_preview)
        col = QVBoxLayout()
        col.addWidget(BodyLabel("Background style"))
        col.addWidget(self.bg_mode_combo)
        self.bg_mode_hint = CaptionLabel("")
        self.bg_mode_hint.setWordWrap(True)
        col.addWidget(self.bg_mode_hint)
        col.addSpacing(6)
        col.addWidget(self.bg_choose)
        col.addWidget(self.bg_reset)
        col.addStretch(1)
        row.addLayout(col, stretch=1)
        lay.addLayout(row)

        self.bg_status = CaptionLabel("")
        self.bg_status.setWordWrap(True)
        lay.addWidget(self.bg_status)

        # LiquidGlass theme toggle --------------------------------------------
        # Only offered on the version/loader combo the bundled mod supports;
        # the checkbox mirrors whether the jar is actually in the mods folder.
        self._lg_supported = (
            (self._instance.mc_version or "") == _LIQUIDGLASS_MC_VERSION
            and (self._instance.loader_id or "") == _LIQUIDGLASS_LOADER_ID
        )
        self.lg_section = QWidget()
        lg_lay = QVBoxLayout(self.lg_section)
        lg_lay.setContentsMargins(0, 8, 0, 0)
        lg_lay.setSpacing(4)
        self.liquidglass_checkbox = CheckBox("Enable LiquidGlass theme")
        self.lg_hint = CaptionLabel("")
        self.lg_hint.setWordWrap(True)
        lg_lay.addWidget(self.liquidglass_checkbox)
        lg_lay.addWidget(self.lg_hint)
        lay.addWidget(self.lg_section)
        if self._lg_supported:
            src = _liquidglass_source_jar()
            installed = _liquidglass_installed(self._instance.mods_dir)
            if src is None:
                # Bundled mod missing — show the row but make it unusable.
                self.liquidglass_checkbox.setEnabled(False)
                self.lg_hint.setText(
                    "LiquidGlass mod jar not found in launcher/internalfiles."
                )
            else:
                self.liquidglass_checkbox.blockSignals(True)
                self.liquidglass_checkbox.setChecked(installed is not None)
                self.liquidglass_checkbox.blockSignals(False)
        else:
            self.lg_section.setVisible(False)
        self.liquidglass_checkbox.toggled.connect(self._on_liquidglass_toggled)

        lay.addStretch(1)

        self.bg_choose.clicked.connect(self._choose_bg)
        self.bg_reset.clicked.connect(self._reset_bg)
        self.bg_mode_combo.currentIndexChanged.connect(self._update_bg_mode_hint)
        self._update_bg_preview()
        self._update_bg_status()
        self._update_bg_mode_hint()
        self.addSubInterface(page, FluentIcon.PALETTE, "Custom Styles")

    def _update_bg_mode_hint(self) -> None:
        if self.bg_mode_combo.currentData() == styles.PANORAMA_MODE:
            self.bg_mode_hint.setText(
                "Best with a wide 360° strip (e.g. an F2 panorama screenshot). "
                "The title screen slowly pans across your image."
            )
        else:
            self.bg_mode_hint.setText(
                "Any aspect ratio. Your image now fills the whole screen as "
                "a flat backdrop (overlay) instead of the rotating panorama."
            )

    def _on_liquidglass_toggled(self, checked: bool) -> None:
        """Install / remove the bundled LiquidGlass mod for this instance.

        The checkbox state is never trusted for persistence: after every
        change it is re-derived from the mods folder, so it stays checked
        exactly as long as the mod jar is present.
        """
        inst = self._instance
        try:
            if checked:
                src = _liquidglass_source_jar()
                if not src or not os.path.isfile(src):
                    raise InstanceError(
                        "LiquidGlass mod jar not found in launcher/internalfiles."
                    )
                os.makedirs(inst.mods_dir, exist_ok=True)
                dest = os.path.join(inst.mods_dir, os.path.basename(src))
                shutil.copy2(src, dest)
                self._toast(
                    "success", "LiquidGlass enabled",
                    "Added to this instance's mods folder.", 4000,
                )
            else:
                name = _liquidglass_installed(inst.mods_dir)
                if name:
                    os.remove(os.path.join(inst.mods_dir, name))
                self._toast(
                    "info", "LiquidGlass disabled",
                    "Removed from this instance's mods folder.", 4000,
                )
        except Exception as exc:  # noqa: BLE001 - never crash the editor
            self._toast("error", "LiquidGlass", str(exc)[:160], 8000)
        finally:
            # Reflect the real on-disk state (also reverts on failure).
            self.liquidglass_checkbox.blockSignals(True)
            self.liquidglass_checkbox.setChecked(
                _liquidglass_installed(inst.mods_dir) is not None
            )
            self.liquidglass_checkbox.blockSignals(False)
        # Keep the Installed Mods tab in sync when it has been built.
        if hasattr(self, "mod_list"):
            try:
                self.refresh_mods()
            except Exception:  # noqa: BLE001 - list refresh must not block
                pass

    def _update_bg_preview(self) -> None:
        pm = QPixmap(240, 135)
        pm.fill(Qt.GlobalColor.darkGray)
        path = self._pending_bg_png
        src = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        if not src.isNull():
            scaled = src.scaled(
                232, 127,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter = QPainter(pm)
            painter.drawPixmap((240 - scaled.width()) // 2,
                               (135 - scaled.height()) // 2, scaled)
            painter.end()
        else:
            pm.fill(Qt.GlobalColor.transparent)
        self.bg_preview.setPixmap(pm)

    def _update_bg_status(self) -> None:
        path = self._pending_bg_png
        if path and os.path.isfile(path):
            self.bg_status.setText(
                f"Background selected: {os.path.basename(path)}\n"
                "Installed when you press Save."
            )
        else:
            self.bg_status.setText(
                "No custom background - vanilla panorama is in use."
            )

    def _choose_bg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose title-screen background", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp);;All files (*)",
        )
        if not path:
            return
        # Normalise through QImage so only real, decodable images are kept.
        probe = QPixmap(path)
        if probe.isNull():
            self._toast("error", "Invalid image",
                        "That file could not be decoded as an image.")
            return
        inst_dir = self._instance.dir
        os.makedirs(inst_dir, exist_ok=True)
        ext = os.path.splitext(path)[1].lower() or ".png"
        dest = os.path.join(inst_dir, f"title-bg{ext}")
        try:
            shutil.copyfile(path, dest)
        except OSError as exc:
            self._toast("error", "Copy failed", str(exc))
            return
        self._pending_bg_png = dest
        self._update_bg_preview()
        self._update_bg_status()

    def _reset_bg(self) -> None:
        path = self._pending_bg_png
        self._pending_bg_png = None
        if path and os.path.realpath(path).startswith(
                os.path.realpath(self._instance.dir) + os.sep):
            try:
                os.remove(path)
            except OSError:
                pass
        self._update_bg_preview()
        self._update_bg_status()

    def _copy_path(self) -> None:
        from PyQt6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(self._instance.dir)
        self._toast("success", "Copied", self._instance.dir)

    def _open_instance_folder(self) -> None:
        os.makedirs(self._instance.dir, exist_ok=True)
        if os.name == "nt":
            subprocess.Popen(["explorer", os.path.normpath(self._instance.dir)])
        else:
            import sys as _sys
            opener = "open" if _sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, self._instance.dir])

    # ----------------------------------------------------------------- footer --
    def _build_footer(self) -> None:
        # MSFluentWindow does not expose the root layout reliably, so each
        # page appends its own Save/Cancel bar via _add_footer_bar().
        self._add_footer_bar_to_all_pages()

    def _add_footer_bar_to_all_pages(self) -> None:
        for page in (getattr(self, "_instance_page", None),
                     getattr(self, "_mods_page", None),
                     getattr(self, "_install_page", None),
                     getattr(self, "_style_page", None)):
            if page is not None:
                self._add_footer_bar(page.layout())

    def _add_footer_bar(self, page_layout) -> None:
        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = PushButton(FluentIcon.CLOSE, "Cancel")
        save = PushButton(FluentIcon.SAVE, "Save")
        save.setObjectName("primaryBtn")
        bar.addWidget(cancel)
        bar.addWidget(save)
        page_layout.addLayout(bar)
        cancel.clicked.connect(self.close)
        save.clicked.connect(self._apply_and_close)

    def _apply_and_close(self) -> None:
        inst = self._instance

        # -- identity (Instance page) ------------------------------------
        old_name = inst.name
        new_name = self.name_edit.text().strip()
        if not new_name:
            self._toast("error", "Invalid name", "Instance name cannot be empty.")
            return
        if new_name != old_name:
            # Validate + ensure uniqueness, then move the folder on disk.
            try:
                validate_name(new_name)
                if self._registry.get(new_name) is not None:
                    raise InstanceError(
                        f"An instance named '{new_name}' already exists."
                    )
            except InstanceError as exc:
                self._toast("error", "Rename failed", str(exc))
                return
            old_dir = inst.dir
            if os.path.isdir(old_dir):
                try:
                    os.rename(old_dir, os.path.join(os.path.dirname(old_dir), new_name))
                except OSError as exc:
                    self._toast("error", "Folder rename failed", str(exc))
                    return
            inst.name = new_name
            # Keep the copied icon/banner/background paths valid after the move.
            new_dir = inst.dir
            for attr_path in ("_pending_icon_path", "_pending_bg_png",
                              "_pending_banner_path"):
                p = getattr(self, attr_path, None)
                if p and os.path.realpath(p).startswith(
                        os.path.realpath(old_dir) + os.sep):
                    setattr(self, attr_path,
                            os.path.join(new_dir, os.path.basename(p)))
            for attr_path in ("icon_path", "banner_path"):
                p = getattr(inst, attr_path, None)
                if p and os.path.realpath(p).startswith(
                        os.path.realpath(old_dir) + os.sep):
                    setattr(inst, attr_path,
                            os.path.join(new_dir, os.path.basename(p)))
        new_version = (self.version_combo.text()
                       or "").strip() or inst.mc_version
        inst.mc_version = new_version
        lid = self.loader_combo.currentData()
        if lid:
            inst.loader_id = str(lid)
        raw_build = self.build_combo.text().strip()
        inst.loader_build = raw_build or None
        # Changing version/loader invalidates the installed launch version;
        # the next Play will reinstall/verify the correct target.
        if inst.launch_version_id and (
            inst.mc_version != new_version or raw_build != (inst.loader_build or "")
        ):
            pass  # kept simple: launch id is refreshed at install time

        # -- runtime settings (shared widgets) ----------------------------
        inst.ram_gb = int(self.ram_combo.currentData() or inst.ram_gb)
        inst.java_path = self.java_path_edit.text().strip() or None
        inst.extra_jvm_args = self.gather_jvm_args()
        inst.icon_path = self._pending_icon_path or None
        inst.banner_path = self._pending_banner_path or None
        inst.custom_bg_png = self._pending_bg_png or None
        inst.custom_bg_mode = (
            str(self.bg_mode_combo.currentData()) if self._pending_bg_png
            else inst.custom_bg_mode
        ) or styles.STATIC_MODE
        # Commit the external mods folders selected on the Mods tab. add/remove
        # already mutated the live object and synced the disk; re-normalising
        # here just drops blanks and de-duplicates before persisting.
        inst.external_mod_dirs = external_mods.normalise_folders(inst)

        try:
            self._registry.update(inst)
        except Exception as exc:  # noqa: BLE001
            self._toast("error", "Save failed", str(exc))
            return

        # Install/remove the custom title background right away so a launch
        # immediately after closing this editor picks it up.
        try:
            from . import styles as _styles

            if inst.custom_bg_png:
                _styles.set_title_background(inst, inst.custom_bg_png)
            else:
                _styles.clear_title_background(inst)
        except Exception as exc:  # noqa: BLE001 - styling never blocks save
            self._toast(
                "warning", "Custom style",
                f"Background not applied: {str(exc)[:160]}", 8000,
            )
        self._toast("success", "Saved",
                    f"'{inst.name}' updated." +
                    ("  (renamed)" if new_name != old_name else ""))
        if callable(self.on_saved):
            try:
                self.on_saved()
            except Exception:  # noqa: BLE001 - grid refresh must not block save
                pass
        self.close()