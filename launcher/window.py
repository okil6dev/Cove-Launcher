"""
Main window for Cove Launcher (PyQt-Fluent-Widgets).

``LauncherWindow`` is a slim coordinator: it owns the encrypted vault, the
account store and the instance registry, wires the navigation pages together
and drives the install -> launch pipeline in background threads.
"""

from __future__ import annotations

from collections import deque
import time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from qfluentwidgets import (
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MSFluentWindow,
)

from .accounts import AccountStore
from .constants import APP_NAME, APP_VERSION, ICON_FILE, MIN_SUPPORTED_RELEASE, JAVA_MODE_GRAALVM
from .instances import InstanceRegistry, build_jvm_arguments, resolve_java
from .servers import ServerRegistry
from .security import Vault, VaultError
from .system_info import get_total_ram_gb


def _dismiss_child_modals(window) -> None:
    """Bypass the Fluent 100 ms fade-out and immediately unblock any exec().

    MaskDialogBase.done() triggers a QPropertyAnimation before it calls
    QDialog.done().  If the parent window is destroyed mid-animation the
    finished signal never fires, leaving exec() spinning forever (freeze).
    """
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QDialog) and widget.isModal() and widget.isVisible():
            p = widget.parent()
            while p is not None:
                if p is window:
                    QDialog.done(widget, QDialog.DialogCode.Rejected)
                    break
                p = p.parent()


from .workers import InstallerThread, LaunchThread, LoaderVersionsThread, VersionListThread

from minecraft_launcher_lib import mrpack as mll_mrpack

def _cove_splash_diag(msg: str) -> None:
    """TEMP diagnostics: append splash/window geometry to a log for debugging."""
    try:
        import os as _os
        import time as _time

        base = _os.environ.get("APPDATA") or _os.getcwd()
        path = _os.path.join(base, "Cove", "splash-debug.log")
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (_time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


class _LoadingSplash(QWidget):
    """
    Spinning loading screen shown while the launcher boots.

    Covers the whole window with an opaque themed background, a spinner,
    the app name and a subtle status line.  Call ``finish()`` to drop it.
    """

    def __init__(self, parent: QWidget) -> None:
        from PyQt6.QtGui import QPixmap

        from qfluentwidgets import BodyLabel, IndeterminateProgressRing, TitleLabel

        from .constants import ICON_PNG, asset_path

        super().__init__(parent)
        # Do NOT auto-fill: on Mica/Acrylic the window composites the desktop
        # backdrop via DWM and the palette pre-fill would paint an opaque block
        # over it. paintEvent draws the full background instead.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAutoFillBackground(False)
        # Let the DWM backdrop (wallpaper) show through on Mica/Acrylic: without
        # a translucent background attribute Qt may composite this child widget
        # opaquely over the transparent window, hiding the backdrop. paintEvent
        # still paints an opaque fill on plain light/dark, so this is a no-op
        # for those themes.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(2)

        icon_label = QLabel()
        icon_pm = QPixmap(asset_path(ICON_PNG))
        if not icon_pm.isNull():
            icon_label.setPixmap(icon_pm.scaled(
                96, 96,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(icon_label)

        self.title = TitleLabel(APP_NAME)
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.title)

        self.status = BodyLabel("Warming up the lava…")
        self.status.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(6)
        lay.addWidget(self.status)

        lay.addSpacing(18)
        self.spinner = IndeterminateProgressRing(self)
        self.spinner.setFixedSize(46, 46)
        spinner_row = QHBoxLayout()
        spinner_row.addStretch(1)
        spinner_row.addWidget(self.spinner)
        spinner_row.addStretch(1)
        lay.addLayout(spinner_row)
        lay.addStretch(3)
        self.setGeometry(0, 0, parent.width() or 1000, parent.height() or 720)
        _cove_splash_diag(
            "CREATE win=%dx%d splash=%dx%d | winFrame=%s winRect=%s "
            "sizeHint=%s dpr=%s logicalDpi=%s"
            % (parent.width(), parent.height(),
               self.width(), self.height(),
               (parent.frameGeometry().width(), parent.frameGeometry().height()),
               (parent.rect().width(), parent.rect().height()),
               (parent.sizeHint().width(), parent.sizeHint().height()),
               parent.devicePixelRatio(),
               parent.logicalDpiX()))
        # Glue to the window: the event filter re-fits the splash on every
        # geometry/layout change, so it always matches the window exactly.
        self._install_cove_autosize()

        lay.addStretch(3)
        self.setGeometry(0, 0, parent.width() or 1000, parent.height() or 720)
        # Glue to the window: the event filter re-fits the splash on every
        # geometry/layout change, so it always matches the window exactly.
        self._install_cove_autosize()

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def finish(self) -> None:
        """Stop the spinner and remove the splash."""
        self.hide()
        self.deleteLater()

    def resize_to_parent(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(0, 0, parent.width(), parent.height())

    def _cove_snap(self) -> None:
        """Force the splash to the parent window's *current* size.

        Called from the event filter installed below, so it re-fits on every
        window geometry/layout change -- including the settling a frameless
        Windows window does after show() (DWM shadow, title-bar and stylesheet
        driven layout passes) and any theme switch that alters the central
        widget's margins. That settling is invisible to an offscreen Qt run,
        which is why the splash could look the wrong size on real Windows even
        though both widgets report the same numbers there.
        """
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            if self.width() != parent.width() or self.height() != parent.height():
                _cove_splash_diag(
                    "SNAP win=%dx%d splash=%dx%d -> %dx%d"
                    % (parent.width(), parent.height(),
                       self.width(), self.height(),
                       parent.width(), parent.height()))
                self.setGeometry(0, 0, parent.width(), parent.height())
        except RuntimeError:
            pass  # widget already destroyed

    def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
        """Keep the splash glued to the window for as long as it is visible."""
        if obj is self.parentWidget():
            try:
                from PyQt6.QtCore import QEvent

                kind = event.type()
                if kind in (
                    QEvent.Type.Resize,
                    QEvent.Type.Move,
                    QEvent.Type.Show,
                    QEvent.Type.LayoutRequest,
                    QEvent.Type.Polish,
                    QEvent.Type.WindowStateChange,
                ):
                    self._cove_snap()
            except RuntimeError:
                return False
        return super().eventFilter(obj, event)

    def _install_cove_autosize(self) -> None:
        """Watch the parent window so the splash can never end up mis-sized."""
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            parent.installEventFilter(self)
        except Exception:
            pass
        self._cove_snap()

    def _cove_snap_deferred(self) -> None:
        """Queue a re-fit without recursing into the current paint pass."""
        if getattr(self, "_cove_snap_pending", False):
            return

        self._cove_snap_pending = True
        try:
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(0, self._cove_snap_apply)
        except Exception:
            self._cove_snap_pending = False
            self._cove_snap()

    def _cove_snap_apply(self) -> None:
        self._cove_snap_pending = False
        self._cove_snap()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Re-fit to the parent's live size on EVERY paint. This is the one
        # moment that cannot be stale: Qt is about to rasterise self.rect(),
        # so whatever the window measures right now is exactly what must be
        # covered. Relying on Resize/LayoutRequest events alone is not enough
        # -- a frameless Windows window settles its real geometry through DWM
        # and stylesheet-driven layout passes that do not always surface as a
        # Qt Resize on the parent, which is how the splash ended up the wrong
        # size on screen while both widgets still reported matching numbers.
        #
        # Deferred on purpose: calling setGeometry() from inside our own
        # paintEvent would recurse, so queue the change and paint the current
        # (already-correct or about-to-be-corrected) rect now.
        self._cove_snap_deferred()
        from .theme import current_theme, is_dark_now, uses_backdrop

        dark = is_dark_now()
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        if uses_backdrop(current_theme()):
            # Mica / Acrylic: the window is transparent so the DWM backdrop
            # (desktop wallpaper) shows through.  Paint only a light tone-
            # matched scrim to keep the icon + text legible over it, matching
            # how the main window stays transparent on these themes.
            painter.setBrush(QColor(20, 24, 28, 110) if dark
                             else QColor(255, 255, 255, 120))
        else:
            # Plain light/dark: fully opaque, exactly as before.
            painter.setBrush(QColor(32, 32, 32) if dark
                             else QColor(247, 249, 252))
        painter.drawRect(self.rect())
        painter.end()
        super().paintEvent(event)


class LauncherWindow(MSFluentWindow):
    """Fluent shell: Instances / Mods / Accounts / Settings / Console."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Cove Launcher")

        # Before anything can open a modal: Fluent dialogs can otherwise wedge
        # exec() permanently (see launcher/fluent_dialogs.py).
        from .fluent_dialogs import apply_fluent_dialog_fix

        apply_fluent_dialog_fix()

        from PyQt6.QtGui import QIcon

        from .constants import ICON_FILE, asset_path
        self.setWindowIcon(QIcon(asset_path(ICON_FILE)))
        # Starting size comes from Settings -> Window Sizes (default 1280x820);
        # get_size() falls back to that default if the vault is unreadable.
        from .window_sizes import get_size

        main_w, main_h = get_size("main")
        self.resize(main_w, main_h)

        # HARD CAP on the boot size (verified against a minimal Qt repro):
        # Qt clamps a top-level widget UP to its layout's totalMinimumSize --
        # overridable minimumSizeHint does NOT stop this, but an explicit
        # minimum does: with setMinimumSize(main_w, main_h) the layout minimum
        # is ignored for the clamp and the window stays exactly at the
        # configured size even when a page demands 2800px. Without this the
        # window silently grew to 2249px right after the boot splash appeared
        # ("the splash is not the same size as the window"). Content pages are
        # separately made to fit the content area (window width - 72px nav
        # rail); this is the guarantee no page can resize the boot window.
        self.setMinimumSize(main_w, main_h)

        # Center window on screen
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._center_on_screen)

        # Core services -------------------------------------------------------
        try:
            self._vault: Vault | None = Vault()
        except VaultError as exc:
            # Without the vault nothing secret works; still allow offline use.
            self._vault = None
            self._vault_error = str(exc)
        else:
            self._vault_error = ""

        # Theme: saved (or default) light / dark / mica / acrylic selection.
        self._theme_key: str = self._saved_theme()
        from .theme import apply_theme
        apply_theme(self._theme_key, self)
        self._theme_applied = False  # backdrop needs re-apply after show

        self.accounts = (
            AccountStore(self._vault) if self._vault is not None else None
        )
        self.registry = InstanceRegistry()
        self.server_registry = ServerRegistry()
        self._installer: InstallerThread | None = None
        self._launcher: LaunchThread | None = None
        self._version_thread: VersionListThread | None = None
        self._loader_fetch: LoaderVersionsThread | None = None
        self._dialog_loader_fetch: LoaderVersionsThread | None = None
        self._pending_dialog = None
        self._import_target: tuple[str, str] | None = None  # (path, instance_name)
        self._recent_game_output: deque[str] = deque(maxlen=60)
        self._versions: list[str] = []

        self._build_pages()
        self._initial_logs()
        self._setup_cool_extras()

    # ------------------------------------------------------------------ cool --
    def _setup_cool_extras(self) -> None:
        """Splash screen, Ctrl+K palette, shortcuts and page transitions."""
        from functools import partial

        from PyQt6.QtGui import QKeySequence, QShortcut

        # Boot splash: spinning loader covering the window while pages
        # finish initialising.
        try:
            self._splash = _LoadingSplash(self)
            self._splash.show()
        except Exception:
            # Splash is cosmetic: never let it abort window construction.
            self._splash = None
        if self._splash is not None:
            # The splash is a child of this window, so on Mica/Acrylic the
            # nav sidebar, page stack and title bar paint *behind* its
            # translucent scrim. Hide that launcher chrome while the splash
            # is up so only the themed backdrop plus the splash icon/spinner
            # are visible; it is restored the instant the splash finishes.
            self._hide_splash_chrome()

        # Smooth fade-in when switching pages.
        self.stackedWidget.currentChanged.connect(self._fade_current_page)

        # Global shortcuts: Ctrl+K palette, Ctrl+1..6 page navigation.
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self._open_command_palette)
        for i, page in enumerate([
            self.instances_page,
            self.mods_page,
            self.accounts_page,
            self.skins_page,
            self.settings_page,
            self.java_page,
        ]):
            if page is not None:
                QShortcut(
                    QKeySequence(f"Ctrl+{i + 1}"), self,
                    activated=partial(self.switchTo, page),
                )

    def _hide_splash_chrome(self) -> None:
        """Hide the launcher chrome (nav, page stack, title bar) while the
        splash is up.

        The splash is a *child* of this window, so on Mica/Acrylic the nav
        sidebar, page stack and title bar paint *behind* its translucent
        scrim -- the launcher shows through the "loading" screen. Hiding them
        leaves only the themed backdrop plus the splash icon/spinner visible.
        They are restored the instant the splash finishes.

        Defensive: tolerates a missing or already-deleted widget so boot can
        never be aborted by this cosmetic step.
        """
        self._splash_chrome_hidden = True
        for attr in ("navigationInterface", "stackedWidget", "titleBar"):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            try:
                widget.hide()
            except RuntimeError:
                pass  # C++ object already gone

    def _restore_splash_chrome(self) -> None:
        """Bring the launcher chrome back once the splash has finished."""
        if not getattr(self, "_splash_chrome_hidden", False):
            return
        for attr in ("navigationInterface", "stackedWidget", "titleBar"):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            try:
                widget.show()
            except RuntimeError:
                pass  # C++ object already gone
        self._splash_chrome_hidden = False

    def _fade_current_page(self, index: int) -> None:
        # No-op now: the slide animation itself is neutered to zero distance
        # (see _build_pages), so there is no sliding paint to smear and no
        # stranded pages.  Kept as the slot.  No QSS / colours touched.
        try:
            widget = self.stackedWidget.widget(index)
        except Exception:
            return
        if widget is None:
            return
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        for attr in ("_cove_fade_effect", "_cove_fade_anim"):
            try:
                setattr(widget, attr, None)
            except Exception:
                pass
        try:
            widget.update()
        except Exception:
            pass

    def _open_command_palette(self) -> None:
        """Build + show the Ctrl+K command palette."""
        from .command_palette import show_palette

        show_palette(self, self._palette_commands())

    def _palette_commands(self) -> list:
        """All actions offered by the Ctrl+K command palette."""
        from .command_palette import Command
        from .theme import THEME_OPTIONS

        commands: list[Command] = [
            Command(f"Go to {label}", "navigation", "page go jump", cb)
            for label, cb in (
                ("Instances", lambda: self.switchTo(self.instances_page)),
                ("Mods", lambda: self.switchTo(self.mods_page)),
                ("Java Downloads", lambda: self.switchTo(self.java_page)),
                ("Console", lambda: self.switchTo(
                    self.console_page.console.parentWidget())),
            )
        ]
        if self.accounts_page is not None:
            commands.append(Command(
                "Go to Accounts", "navigation", "page account login",
                lambda: self.switchTo(self.accounts_page)))
        if self.skins_page is not None:
            commands.append(Command(
                "Go to Skins", "navigation", "page skin cape editor upload",
                lambda: self.switchTo(self.skins_page)))
        if self.settings_page is not None:
            commands.append(Command(
                "Go to Settings", "navigation", "page options config",
                lambda: self.switchTo(self.settings_page)))

        # Launch any instance directly.
        for inst in self.registry.list():
            commands.append(Command(
                f"Launch '{inst.name}'",
                f"Minecraft {inst.mc_version}",
                "play start run game launch",
                lambda name=inst.name: self._on_play(name),
            ))

        # Theme switching.
        for theme_label, theme_key in THEME_OPTIONS:
            commands.append(Command(
                f"Theme: {theme_label}", "appearance", "theme light dark mica acrylic",
                lambda k=theme_key: self.apply_theme_setting(k),
            ))

        # Misc actions.
        commands.append(Command(
            "New instance…", "action", "create install add",
            self._on_new_instance))
        commands.append(Command(
            "Import modpack…", "action", "mrpack zip import",
            self._on_import_pack))
        return commands

    # ------------------------------------------------------------------ setup --
    def _build_pages(self) -> None:
        from .pages.accounts_page import AccountsPage
        from .pages.console_page import ConsolePage
        from .pages.instances_page import InstancesPage
        from .pages.java_page import JavaPage
        from .pages.mods_page import ModsPage
        from .pages.server_page import ServerPage
        from .pages.settings_page import SettingsPage
        from .pages.skins_page import SkinsPage

        self.instances_page = InstancesPage(self)
        self.console_page = ConsolePage(self)
        self.server_page = ServerPage(
            registry=self.server_registry,
            log=lambda msg, level="INFO": self.log(msg, level),
            toast=self._toast,
            parent=self,
        )

        self.mods_page = ModsPage(
            get_instance=self.instances_page.current_instance,
            log=lambda msg, level="INFO": self.log(msg, level),
            toast=self._toast,
            get_cf_key=self._curseforge_key,
            get_vault=lambda: self._vault,
            get_server=self.server_page.current_server,
            get_servers=lambda: self.server_registry.list(),
            parent=self,
        )

        self.accounts_page = (
            AccountsPage(
                store=self.accounts,
                get_client_id=self.azure_client_id,
                get_ely_client_id=self.elyby_client_id,
                log=lambda msg, level="INFO": self.log(msg, level),
                toast=self._toast,
                append_raw=lambda text: self.console_page.console.append_log(text),
                parent=self,
            )
            if self.accounts is not None else None
        )

        self.skins_page = SkinsPage(
            store=self.accounts,
            get_instance=self.instances_page.current_instance,
            log=lambda msg, level="INFO": self.log(msg, level),
            toast=self._toast,
            parent=self,
        )

        self.settings_page = SettingsPage(
            vault=self._vault,
            log=lambda msg, level="INFO": self.log(msg, level),
            toast=self._toast,
            parent=self,
        ) if self._vault is not None else None

        self.addSubInterface(self.instances_page, FluentIcon.HOME, "Instances")
        self.addSubInterface(self.mods_page, FluentIcon.DEVELOPER_TOOLS, "Mods")
        self.addSubInterface(self.server_page, FluentIcon.IOT, "Server")
        if self.accounts_page is not None:
            self.addSubInterface(
                self.accounts_page, FluentIcon.PEOPLE, "Accounts"
            )
        self.addSubInterface(self.skins_page, FluentIcon.GAME, "Skins")
        if self.settings_page is not None:
            self.addSubInterface(
                self.settings_page, FluentIcon.SETTING, "Settings"
            )

        self.java_page = JavaPage(
            toast=self._toast,
            log=lambda msg, level="INFO": self.log(msg, level),
            on_installed=self._on_java_installed,
            parent=self,
        )
        self.addSubInterface(
            self.java_page, FluentIcon.DOWNLOAD, "Java Downloads"
        )

        self.addSubInterface(
            self.console_page.console.parentWidget(),
            FluentIcon.COMMAND_PROMPT,
            "Console",
        )

        # Wiring ---------------------------------------------------------------
        # Disable PopUpAniStackedWidget's sliding pop-up animation so tab switching
        # is instant and clean, preventing visual smearing and drawing artifacts
        # on transparent/themed pages.
        try:
            self.stackedWidget.setAnimationEnabled(False)
            view = self.stackedWidget.view
            for _info in list(getattr(view, "aniInfos", [])):
                try:
                    _info.deltaX = 0
                    _info.deltaY = 0
                except Exception:
                    pass
        except Exception:
            pass
        self.instances_page.playRequested.connect(self._on_play)
        self.instances_page.newRequested.connect(self._on_new_instance)
        self.instances_page.importRequested.connect(self._on_import_pack)
        self.instances_page.editRequested.connect(self._on_edit_instance)
        self.instances_page.deleteRequested.connect(self._on_delete_instance)
        self.instances_page.openFolderRequested.connect(self._on_open_folder)
        self.instances_page.selectionChanged.connect(
            lambda _inst: self.mods_page.on_instance_changed()
        )
        self.server_page.selectionChanged.connect(
            lambda _srv: self.mods_page.on_instance_changed()
        )

        # Watchdog: a worker whose network call hangs (minecraft-launcher-lib
        # installs have no timeout) would otherwise keep _is_busy() True
        # forever and silently lock out every other action.
        self._worker_last_activity = time.monotonic()
        from PyQt6.QtCore import QTimer

        self._watchdog = QTimer(self)
        self._watchdog.timeout.connect(self._check_worker_stalled)
        self._watchdog.start(15_000)

    def _initial_logs(self) -> None:
        self.log(f"Cove Launcher {APP_VERSION} starting.")
        self.log("Vault unlocked (DPAPI + AES-256-GCM)." if self._vault
                 else f"VAULT ERROR: {self._vault_error}", "WARN" if not self._vault else "INFO")
        self.log(f"{len(self.registry.list())} instance(s) loaded.")
        self.instances_page.refresh(self.registry.list())
        # Kick off async version list for the new-instance dialog.
        self._version_thread = VersionListThread(self)
        self._version_thread.ready.connect(self._on_versions_ready)
        self._version_thread.failed.connect(
            lambda msg: self.log(msg, "ERROR")
        )
        self._version_thread.start()

    # ------------------------------------------------------------- utilities --
    @property
    def vault(self):
        return self._vault

    def log(self, message: str, level: str = "INFO") -> None:
        self.console_page.log(message, level)

    def azure_client_id(self) -> str:
        from .constants import DEFAULT_AZURE_CLIENT_ID
        return DEFAULT_AZURE_CLIENT_ID

    def elyby_client_id(self) -> str:
        """Ely.by OAuth client id; a vault override beats the built-in default."""
        from .constants import ELY_CLIENT_ID

        if self._vault is not None:
            override = str(self._vault.get("ely_client_id", "") or "").strip()
            if override:
                return override
        return ELY_CLIENT_ID

    def _curseforge_key(self) -> str:
        if self._vault is None:
            return ""
        return str(self._vault.get("curseforge_api_key", "") or "")

    def system_java(self) -> str | None:
        if self.settings_page is not None:
            found = self.settings_page.system_java()
            if found:
                return found
        import shutil

        return shutil.which("java")

    def _on_java_installed(self, flavor_id: str, java_exe: str) -> None:
        """Auto-fill the Settings page java path after a Java Downloads install."""
        if self.settings_page is not None:
            self.settings_page.java_edit.setText(java_exe)
            self.log(
                f"Java Downloads: {flavor_id} installed → path auto-filled in Settings.",
                "INFO",
            )

    def _toast(self, severity: str, title: str, content: str,
               duration: int = 5000) -> None:
        show = {
            "success": InfoBar.success,
            "warning": InfoBar.warning,
            "error": InfoBar.error,
            "info": InfoBar.info,
        }[severity]
        show(
            title=title,
            content=content[:300],
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=duration,
            parent=self,
        )

    def _show_box(self, title: str, content: str) -> None:
        from .cove_dialog import CoveConfirm
        CoveConfirm.info(self, title, content)

    def _center_on_screen(self) -> None:
        """Center the window on the primary screen."""
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        qr = screen.availableGeometry()
        self.move(
            qr.left() + (qr.width() - self.width()) // 2,
            qr.top() + (qr.height() - self.height()) // 2,
        )

    def apply_window_sizes(self, sizes) -> None:
        """Live-resize this window after Settings -> Window Sizes is applied.

        Only the *main* window can be resized while it is open; the instance
        and server editors read their own entry from the same mapping when
        they open, so they need no live handling here.

        The configured size is re-clamped against the available screen area:
        the vault bounds are absolute (see launcher.window_sizes) and must
        never leave the window hanging off the desktop.
        """
        from PyQt6.QtWidgets import QApplication

        from .window_sizes import clamp_size

        w, h = clamp_size("main", *tuple(sizes.get("main") or (1280, 820)))
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            w = min(w, area.width())
            h = min(h, area.height())
        self.setMinimumSize(w, h)
        self.resize(w, h)
        # The user may have moved the window: re-centering keeps it on-screen
        # after growing (or shrinking) instead of clipping off an edge.
        self._center_on_screen()
        try:
            # Re-run the label clamp: a shrunk window may need fresh wraps.
            self._cove_clamp_page_mins()
        except Exception:
            pass
        self.log(f"Main window size set to {w}x{h}.")

    # ----------------------------------------------------------------- theme --
    def _saved_theme(self) -> str:
        """Theme key stored in the vault, or the default."""
        from .theme import DEFAULT_THEME, normalize

        if self._vault is None:
            return DEFAULT_THEME
        try:
            return normalize(str(self._vault.read().get("theme", "") or ""))
        except Exception:
            return DEFAULT_THEME

    @property
    def theme_key(self) -> str:
        """Currently applied theme key."""
        return self._theme_key

    def apply_theme_setting(self, key: str) -> None:
        """Apply *key* (light/dark/mica_light/mica_dark/acrylic) live + persist."""
        from .theme import apply_theme, normalize

        self._theme_key = normalize(key)
        apply_theme(self._theme_key, self)
        self._theme_applied = True
        # Persist so the choice survives restarts (e.g. changed via Ctrl+K).
        if self._vault is not None:
            try:
                data = self._vault.read()
                data["theme"] = self._theme_key
                self._vault.write(data)
            except Exception:
                pass
        # Keep the Settings combo in sync.
        settings = getattr(self, "settings_page", None)
        if settings is not None and hasattr(settings, "theme_combo"):
            index = settings.theme_combo.findData(self._theme_key)
            if index >= 0 and settings.theme_combo.currentIndex() != index:
                settings.theme_combo.blockSignals(True)
                settings.theme_combo.setCurrentIndex(index)
                settings.theme_combo.blockSignals(False)
                # Re-apply page styles for the swatch border etc.
                if hasattr(settings, "refresh_theme"):
                    settings.refresh_theme()
        # Let pages re-apply their theme-dependent stylesheets.
        for page in (
            getattr(self, "instances_page", None),
            getattr(self, "mods_page", None),
            getattr(self, "server_page", None),
            getattr(self, "accounts_page", None),
            getattr(self, "java_page", None),
            getattr(self, "settings_page", None),
            getattr(self, "skins_page", None),
        ):
            if page is not None and hasattr(page, "refresh_theme"):
                try:
                    page.refresh_theme()
                except Exception:
                    pass
        # Keep an open instance-editor window on the same theme.
        editor = getattr(self, "_editor_window", None)
        if editor is not None and editor.isVisible():
            try:
                apply_theme(self._theme_key, editor)
                editor._theme_applied = True
            except Exception:
                pass

    def _cove_clamp_page_mins(self) -> None:
        """Wrap pathological labels so no page demands more than the content
        area (window width - 72px nav rail: 1208px at the default 1280).

        Some pages build rows of single-line BodyLabels whose unwrapped
        minimumSizeHint is 675-2176px wide (the Settings page alone asked for
        2176). Qt honours a layout's totalMinimumSize by clamping the top-level
        window UP to it, so the window grew to ~2249px right after the splash
        appeared -- the splash matched 900x620 and then the entire window
        lurched wider, which was the "startup screen is not the same size as
        the window" bug.

        Wrapping makes the text reflow instead of demanding width. The window
        itself is separately capped via setMinimumSize in __init__ with the
        size from Settings -> Window Sizes; this keeps the CONTENT inside that
        cap so nothing is clipped.
        """
        # Derived, never hard-coded, so it tracks Settings -> Window Sizes.
        budget = max(828, self.minimumWidth() - 72)
        from PyQt6.QtWidgets import QLabel

        for lbl in self.findChildren(QLabel):
            try:
                if lbl.wordWrap():
                    continue
                if lbl.minimumSizeHint().width() <= budget:
                    continue
                lbl.setWordWrap(True)
            except Exception:
                continue

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Re-apply the backdrop once the window is visible (DWM quirk)."""
        super().showEvent(event)
        # Wrap pathological labels so no page outgrows the content area
        # (window width - 72px nav rail).
        # Pages build lazily, so this runs here (at show time), before the
        # settle-poller below activates the layout. The window itself is
        # hard-capped at its configured size via setMinimumSize in __init__;
        # this keeps the content inside the cap so nothing gets clipped.
        try:
            self._cove_clamp_page_mins()
        except Exception:
            pass
        _sp = getattr(self, "_splash", None)  # cove: splash post-show snap
        if _sp is not None:
            try:
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(0, _sp.resize_to_parent)
            except Exception:
                pass
        if not self._theme_applied:
            self._theme_applied = True
            from . import theme

            # Use the module-level current theme: it may have been changed
            # (e.g. from Settings) after this window was constructed.
            theme.apply_theme(theme.current_theme(), self)
        # Drop the boot splash only once the window has actually SETTLED at its
        # final size. A fixed timer cannot work: the Fluent window reaches its
        # real geometry through deferred layout passes (and, on Windows, DWM),
        # so a blind delay can drop the splash early -- the splash is then one
        # size and the main app another, which is exactly the mismatch being
        # reported.
        splash = getattr(self, "_splash", None)
        if splash is not None:
            from PyQt6.QtCore import QTimer

            state = {
                "started": time.monotonic(),
                "stable": 0,
                "done": False,
            }
            MAX_WAIT = 6.0   # never trap the user behind a stuck splash
            MIN_SHOW = 0.9   # keep the spinner legible for a moment

            def _finish_splash():
                state["done"] = True
                _cove_splash_diag(
                    "FINISH win=%dx%d splash=%dx%d (waited %.2fs)"
                    % (self.width(), self.height(),
                       splash.width(), splash.height(),
                       time.monotonic() - state["started"]))
                # Final snap: the splash must cover EXACTLY the window about to
                # be revealed, so the reveal shows no size jump at all.
                splash.resize_to_parent()
                splash.set_status("Ready!")
                # Restore the hidden launcher chrome right as the splash drops,
                # so the reveal shows the real UI, not an empty frame.
                self._restore_splash_chrome()
                # Detach from resizeEvent only once the splash is truly gone.
                self._splash = None
                QTimer.singleShot(250, splash.finish)

            def _poll():
                if state["done"]:
                    return
                try:
                    self.layout().activate()
                    size = (self.width(), self.height())
                except RuntimeError:
                    return
                if size == (0, 0):
                    return
                if size == state.get("last_size"):
                    state["stable"] += 1
                else:
                    state["stable"] = 0
                    state["last_size"] = size
                # Keep the splash glued to the window while we wait.
                splash.resize_to_parent()
                waited = time.monotonic() - state["started"]
                if waited > MAX_WAIT:
                    _cove_splash_diag("TIMEOUT after %.1fs" % waited)
                    _finish_splash()
                elif waited >= MIN_SHOW and state["stable"] >= 3:
                    _finish_splash()

            state["last_size"] = (self.width(), self.height())
            _poll_timer = QTimer(self)
            _poll_timer.setInterval(60)
            _poll_timer.timeout.connect(_poll)
            _poll_timer.start()
            # Hold a reference so the timer is not garbage collected.
            self._cove_splash_poll = _poll_timer
            QTimer.singleShot(0, _poll)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        splash = getattr(self, "_splash", None)
        if splash is not None:
            splash.resize_to_parent()

    # ------------------------------------------------------- instance actions --
    def _is_busy(self) -> bool:
        installer_running = (
            self._installer is not None and self._installer.isRunning()
        )
        launcher_running = (
            self._launcher is not None and self._launcher.isRunning()
        )
        return bool(installer_running or launcher_running)

    def _busy_reason(self) -> str:
        """Describe what is currently blocking other actions, or ''.

        These actions used to ``return`` silently when busy, which looked
        exactly like a dead button. Every guard now reports this instead.
        """
        if self._installer is not None and self._installer.isRunning():
            return "an install/download is still running"
        if self._launcher is not None and self._launcher.isRunning():
            return "the game is still running"
        return ""

    def _warn_busy(self, action: str) -> None:
        """Explain why *action* was refused rather than doing nothing at all."""
        reason = self._busy_reason()
        if not reason:
            return
        message = (
            f"Can't {action} because {reason}. Wait for it to finish, or "
            "close the launcher and try again."
        )
        self.log(message, "WARN")
        self._toast("warning", "Still busy", message, 6000)

    # A stalled network call inside minecraft-launcher-lib never returns, so
    # the worker thread stays "running" forever. Five minutes with no status
    # or progress update at all is a genuine stall, not a slow download.
    WORKER_STALL_SECONDS = 300.0

    def _touch_worker(self) -> None:
        """Record that a worker is making progress."""
        self._worker_last_activity = time.monotonic()

    def _clear_worker(self, attr: str, worker) -> None:
        """Drop *attr* only if it still points at *worker*.

        Identity check matters: a newer worker may already have replaced it.
        """
        if getattr(self, attr, None) is worker:
            setattr(self, attr, None)

    def _check_worker_stalled(self) -> None:
        """Recover from a hung worker so the launcher can't lock itself."""
        if not self._is_busy():
            return
        idle = time.monotonic() - self._worker_last_activity
        if idle < self.WORKER_STALL_SECONDS:
            return

        worker = (self._installer if self._installer is not None
                  and self._installer.isRunning() else self._launcher)
        self.log(
            f"No download/install progress for {int(idle)}s — assuming the "
            "worker is hung and unlocking the launcher.", "WARN",
        )
        self._toast(
            "warning", "Task appears stuck",
            f"Nothing has progressed for {int(idle // 60)} minute(s). "
            "The task was stopped so you can continue.",
            10_000,
        )
        if worker is not None:
            # Deliberately NOT worker.terminate(): terminating a thread blocked
            # in a C-level socket read can abort the whole process (verified on
            # this codebase). Dropping the reference unlocks the UI; the worst
            # case is one lingering thread, far better than a dead launcher.
            self._clear_worker("_installer" if worker is self._installer
                               else "_launcher", worker)
        self._set_busy(False, "Previous task stopped (it had stalled).")
        self._touch_worker()

    def _set_busy(self, busy: bool, status_text: str | None = None) -> None:
        self._touch_worker()
        self.instances_page.set_busy(busy, status_text)
        if status_text is not None and not busy:
            self.status_reset_timer()

    def status_reset_timer(self) -> None:
        pass  # placeholder; status persists until next action

    def _on_versions_ready(self, version_ids: list) -> None:
        self._versions = list(version_ids)
        self.log(f"Fetched {len(version_ids)} supported releases.")

    def _on_new_instance(self) -> None:
        if self._is_busy():
            self._warn_busy("create an instance")
            return
        from .dialogs import NewInstanceDialog

        dialog = NewInstanceDialog(self._versions or [MIN_SUPPORTED_RELEASE],
                                   get_total_ram_gb(), self)
        # Fetch loader builds when the dialog changes loader/version.
        dialog.on_fetch_builds = lambda loader_id, mc_version: (
            self._fetch_dialog_builds(dialog, loader_id, mc_version)
        )
        self._pending_dialog = dialog
        if dialog.exec():
            values = dialog.result_values()
            try:
                instance = self.registry.create(**values)
            except Exception as exc:
                self.log(str(exc), "ERROR")
                self._toast("error", "Create failed", str(exc), 8000)
                return
            self.registry.save()
            self.instances_page.refresh(self.registry.list())
            self.log(f"Instance '{instance.name}' created.")
            self._start_install(instance)

    def _fetch_dialog_builds(self, dialog, loader_id: str, mc_version: str) -> None:
        if loader_id == "vanilla":
            return
        self._dialog_loader_fetch = LoaderVersionsThread(loader_id, mc_version, self)
        self._dialog_loader_fetch.ready.connect(dialog.set_builds)
        self._dialog_loader_fetch.failed.connect(
            lambda msg: self.log(msg, "WARN")
        )
        self._dialog_loader_fetch.start()

    def _on_edit_instance(self, name: str) -> None:
        """Open the standalone instance editor window for *name*."""
        if not name or self._is_busy():
            return
        instance = self.registry.get(name)
        if instance is None:
            return
        from .instance_editor import InstanceEditorWindow

        self._editor_window = InstanceEditorWindow(instance, self.registry, self)
        self._editor_window.on_saved = lambda: self.instances_page.refresh(self.registry.list())
        self._editor_window.show()

    def _on_delete_instance(self, name: str) -> None:
        from PyQt6.QtCore import QTimer

        if not name or self._is_busy():
            return

        def _perform() -> None:
            from .cove_dialog import CoveConfirm
            confirmed = CoveConfirm.ask(
                self.window(),
                "Delete instance",
                f"Delete instance '{name}' and all its files?\nThis cannot be undone.",
                ok="Delete",
            )
            if not confirmed:
                return
            self.registry.delete(name)
            self.instances_page.refresh(self.registry.list())
            self.log(f"Instance '{name}' deleted.")

        QTimer.singleShot(0, _perform)


    def _on_open_folder(self, name: str) -> None:
        import os
        import subprocess

        inst = self.registry.get(name)
        if inst is None:
            return
        os.makedirs(inst.dir, exist_ok=True)
        subprocess.Popen(["explorer", os.path.normpath(inst.dir)])  # noqa: S603,S607

    def _on_import_pack(self) -> None:
        if self._is_busy():
            self._warn_busy("import a modpack")
            return
        from PyQt6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Select modpack", "", "Modpacks (*.mrpack *.zip)"
        )
        if not path:
            return
        from .dialogs import ImportNameDialog

        suggested = (
            path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            .rsplit(".", 1)[0][:64] or "Modpack"
        )
        dialog = ImportNameDialog(suggested, self)
        if not dialog.exec():
            return
        name = dialog.name_edit.text().strip()

        try:
            info = mll_mrpack.get_mrpack_information(path)
            launch_version = mll_mrpack.get_mrpack_launch_version(path)
        except Exception as exc:
            self.log(str(exc), "ERROR")
            self._toast("error", "Import failed", str(exc)[:200], 8000)
            return

        try:
            instance = self.registry.register_existing(
                name, launch_version_id=launch_version,
                mc_version=str(info.get("minecraftVersion", "")),
            )
        except Exception as exc:
            self.log(str(exc), "ERROR")
            self._toast("error", "Import failed", str(exc), 8000)
            return

        self._import_target = (path, name)
        self._set_busy(True, f"Importing modpack '{info.get('name', name)}'...")
        self.log(f"Installing modpack {path} into '{name}'...")
        from .workers_net import MrpackInstallThread

        self._installer = MrpackInstallThread(path, instance.dir, parent=self)
        self._installer.status.connect(self.instances_page.status_label.setText)
        self._installer.progress_max.connect(self.instances_page.progress_bar.setMaximum)
        self._installer.progress_value.connect(self.instances_page.progress_bar.setValue)
        self._installer.succeeded.connect(lambda vid: self._on_install_succeeded(instance.name, vid))
        self._installer.failed.connect(self._on_worker_failed)
        self._installer.start()

    # ------------------------------------------------------ play/launch flow --
    def _start_install(self, instance) -> None:
        """Download/install an instance's game files without launching.

        Used right after creating a new instance so the heavy download
        happens immediately and Play afterwards is instant.
        """
        if self._is_busy():
            self._warn_busy("start installing")
            return
        target_base = instance.launch_version_id or instance.mc_version
        self._set_busy(True, f"Installing '{instance.name}' ({target_base})...")
        self.instances_page.progress_bar.setRange(0, 100)
        self.instances_page.progress_bar.setValue(0)
        self._prefetch_instance = instance.name
        self.log(
            f"Installing {instance.mc_version} "
            f"[{instance.loader_id}] for instance '{instance.name}'..."
        )
        self._installer = InstallerThread(
            mc_version=instance.mc_version,
            minecraft_directory=instance.dir,
            modloader_id=instance.loader_id,
            loader_version=instance.loader_build,
            java_executable=self.system_java(),
            parent=self,
        )
        self._installer.status.connect(self.instances_page.status_label.setText)
        self._installer.status.connect(lambda _t: self._touch_worker())
        self._installer.progress_max.connect(
            self.instances_page.progress_bar.setMaximum
        )
        self._installer.progress_value.connect(
            self.instances_page.progress_bar.setValue
        )
        self._installer.progress_value.connect(
            lambda _v: self._touch_worker()
        )
        self._installer.succeeded.connect(
            lambda vid, name=instance.name: self._on_prefetch_succeeded(name, vid)
        )
        self._installer.failed.connect(self._on_worker_failed)
        self._installer.finished.connect(
            lambda w=self._installer: self._clear_worker("_installer", w)
        )
        self._installer.start()

    def _on_prefetch_succeeded(self, instance_name: str,
                               installed_version: str) -> None:
        instance = self.registry.get(instance_name)
        if instance is not None and instance.launch_version_id != installed_version:
            instance.launch_version_id = installed_version
            self.registry.update(instance)
            self.instances_page.refresh(self.registry.list())
        self._set_busy(False)
        self.instances_page.status_label.setText("Ready to play.")
        self.log(f"Instance '{instance_name}' ready (version '{installed_version}').")
        self._toast("success", "Instance ready",
                    f"'{instance_name}' finished installing.", 5000)

    def _on_play(self, instance_name: str) -> None:
        if self._is_busy():
            self._warn_busy("play")
            return
        instance = self.registry.get(instance_name)
        if instance is None:
            return

        system_java = self.system_java()
        if not system_java:
            self._show_box(
                "Java runtime missing",
                "No Java runtime was found.\n\nSet one in Settings (System "
                "Java) or install Java on your PATH.",
            )
            return
        if self.accounts is None:
            self._show_box("Vault error", self._vault_error)
            return
        account = self.accounts.current()

        target = instance.launch_version_id or instance.mc_version
        self._set_busy(True, f"Preparing '{instance.name}' ({target})...")
        self.instances_page.progress_bar.setRange(0, 100)
        self.instances_page.progress_bar.setValue(0)
        self._launching_instance = instance
        self.log(f"Installing/verifying {target} for instance '{instance.name}'...")

        if instance.launch_version_id:
            # Modpack / modded id already resolved -> verify & repair only.
            self._installer = InstallerThread(
                mc_version=target,
                minecraft_directory=instance.dir,
                modloader_id="vanilla",
                java_executable=system_java,
                parent=self,
            )
        else:
            self._installer = InstallerThread(
                mc_version=instance.mc_version,
                minecraft_directory=instance.dir,
                modloader_id=instance.loader_id,
                loader_version=instance.loader_build,
                java_executable=system_java,
                parent=self,
            )
        self._installer.status.connect(self.instances_page.status_label.setText)
        self._installer.progress_max.connect(
            self.instances_page.progress_bar.setMaximum
        )
        self._installer.progress_value.connect(
            self.instances_page.progress_bar.setValue
        )
        self._installer.succeeded.connect(
            lambda vid, name=instance.name: self._on_install_succeeded(name, vid)
        )
        self._installer.failed.connect(self._on_worker_failed)
        self._installer.start()

    def _on_install_succeeded(self, instance_name: str, installed_version: str) -> None:
        instance = self.registry.get(instance_name)
        if instance is not None and instance.launch_version_id != installed_version:
            instance.launch_version_id = installed_version
            self.registry.update(instance)
            self.instances_page.refresh(self.registry.list())
        self.log(f"Installation finished: version id '{installed_version}'.")
        self._start_game(instance_name, installed_version)

    def _start_game(self, instance_name: str, version_id: str) -> None:
        instance = self.registry.get(instance_name)
        if instance is None:
            return
        override_java, fallback_java = resolve_java(instance, self.system_java())
        account = self.accounts.current() if self.accounts is not None else None
        username = account.name if account else "Player"

        # Log which JVM will actually be forced, avoiding a misleading
        # "GraalVM detected" line when the user browsed a non-GraalVM java.
        forced = override_java or ""
        if "graalvm" in forced.lower():
            from .graalvm import detect_graalvm
            result = detect_graalvm()
            ver = result[1] if result else "unknown"
            self.log(f"⚡ GraalVM detected: {forced}  ({ver})")
            self.log("⚡ GraalVM JIT flags injected — expect better FPS & lower frame times.")
        elif instance.java_mode == JAVA_MODE_GRAALVM and not override_java:
            self.log(
                "⚠ GraalVM not found on this system — "
                "using Mojang's bundled runtime for this launch."
            )

        # build_jvm_arguments injects GraalVM-tuned flags automatically when
        # the instance is set to GraalVM mode.
        ram_mb = max(512, int(instance.ram_gb) * 1024)
        jvm_args = build_jvm_arguments(instance, ram_mb)
        # Strip the leading -Xmx/-Xms since LaunchThread re-adds them from ram_gb;
        # only pass the extra flags that follow.
        extra_args = jvm_args[2:]  # skip -Xmx and -Xms

        # Re-sync this instance's custom title-background resource pack
        # (idempotent; regenerates when the source image changed).
        try:
            from .styles import ensure_applied

            ensure_applied(instance)
        except Exception as exc:  # noqa: BLE001 - styling must not block play
            self.log(f"Custom title background skipped: {exc}", "ERROR")

        self.log(
            f"Launching {version_id} in '{instance.name}' as "
            f"'{username}' with {instance.ram_gb} GB RAM..."
        )
        self.instances_page.status_label.setText("Launching game...")
        bar = self.instances_page.progress_bar
        bar.setValue(bar.maximum())

        self._launcher = LaunchThread(
            version_id=version_id,
            minecraft_directory=instance.dir,
            ram_gb=instance.ram_gb,
            java_executable=fallback_java,
            username=username,
            account_store=self.accounts,
            account=account,
            extra_jvm_args=extra_args,
            instance=instance,
            parent=self,
        )
        if override_java:
            # Explicit user choice (GraalVM / System PATH / Custom) -> force
            # that JVM via executablePath so it also beats Mojang's bundled
            # runtime for modern versions (NeoForge / 1.21+ etc).
            self._launcher._java_override = override_java
        self._launcher.game_started.connect(self._on_game_started)
        self._launcher.game_output.connect(self._on_game_output)
        self._launcher.status.connect(lambda msg: self.log(msg))
        self._launcher.game_exited.connect(
            lambda code, name=instance.name: self._on_game_exited(name, code)
        )
        self._launcher.failed.connect(self._on_worker_failed)
        self._launcher.finished.connect(
            lambda w=self._launcher: self._clear_worker("_launcher", w)
        )
        self._launcher.start()

    def _on_game_started(self, command_line: list) -> None:
        from PyQt6.QtCore import QTimer

        preview = " ".join(command_line[:4])
        suffix = " ..." if len(command_line) > 4 else ""
        self.log(f"Process started: {preview}{suffix}")
        self.instances_page.status_label.setText("Game running.")
        self._recent_game_output.clear()
        # Playtime tracking: tick a live session counter every second.
        self._play_started = time.monotonic()
        self._playtime_timer = QTimer(self)
        self._playtime_timer.timeout.connect(self._tick_playtime)
        self._playtime_timer.start(1000)
        self._toast("success", "Game launched",
                    "Minecraft is starting. See Console for output.")

    def _tick_playtime(self) -> None:
        if self._play_started is not None:
            self.instances_page.set_session(int(time.monotonic() - self._play_started))

    def _stop_playtime(self) -> int | None:
        """Stop the live counter and return the session length in seconds."""
        timer = getattr(self, "_playtime_timer", None)
        if timer is not None:
            timer.stop()
            self._playtime_timer = None
        started = getattr(self, "_play_started", None)
        self._play_started = None
        self.instances_page.set_session(None)
        if started is None:
            return None
        return int(time.monotonic() - started)

    def _on_game_output(self, line: str) -> None:
        self.console_page.console.append_log(line)
        self._recent_game_output.append(line)

    def _on_game_exited(self, instance_name: str, exit_code: int) -> None:
        self.log(f"Game exited with code {exit_code}.")
        if exit_code != 0:
            diagnosis = self._diagnose_bad_exit()
            if diagnosis:
                self._show_box("Launch failed - Java problem", diagnosis)
            else:
                self._toast(
                    "warning", "Game exited",
                    f"Minecraft exited with code {exit_code}. Check Console.",
                    8000,
                )
        else:
            self._toast("info", "Game closed", "Minecraft exited normally.")
        session = self._stop_playtime()
        if session is not None and session > 0:
            self.registry.add_playtime(instance_name, session)
            self.registry.mark_played(instance_name)
            mins = max(1, session // 60)
            self.log(f"Playtime added: {mins} min to '{instance_name}'.")
        self.instances_page.refresh(self.registry.list())
        self._set_busy(False, f"Game exited (code {exit_code}).")
        bar = self.instances_page.progress_bar
        bar.setValue(0)
        self._launcher = None

    def _diagnose_bad_exit(self) -> str | None:
        output = "\n".join(self._recent_game_output)
        jvm_markers = (
            "Unrecognized option",
            "Could not create the Java Virtual Machine",
            "UnsupportedClassVersionError",
            "has been compiled by a more recent version of the Java Runtime",
        )
        if any(marker in output for marker in jvm_markers):
            return (
                "The Java Virtual Machine rejected the launch. The selected "
                "Java runtime is likely too old for this Minecraft version.\n\n"
                "Tip: keep the instance's Java mode on 'Auto (Mojang runtime)' "
                "so the launcher uses the bundled matching JRE."
            )
        return None

    # ------------------------------------------------------- error handling --
    def _on_worker_failed(self, message: str) -> None:
        from .accounts import AuthError  # noqa: F401 - documented failure class

        self.log(message, "ERROR")
        self._stop_playtime()  # safety: no-op unless a session was ticking
        self._set_busy(False, "Failed.")
        bar = self.instances_page.progress_bar
        bar.setValue(0)
        self._toast("error", "Launcher error", message[:250], 10000)

    # ------------------------------------------------------------ lifecycle --
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Dismiss any Cove modal before the window tears down.
        _dismiss_child_modals(self)

        game_running = self._launcher is not None and self._launcher.isRunning()
        if game_running:
            from .cove_dialog import CoveConfirm
            confirmed = CoveConfirm.ask(
                self,
                "Game still running",
                "Minecraft is still running.\n\nClose the launcher anyway? "
                "(The game keeps running.)",
                ok="Close anyway",
            )
            if not confirmed:
                event.ignore()
                return
            self._launcher = None

        installer = self._installer
        if installer is not None and installer.isRunning():
            if hasattr(installer, "cancel"):
                installer.cancel()
            installer.wait(3000)
        event.accept()








# ---- cove-splash-snap: boot splash always matches the main window size ----
def _cove_snap_splash_to_window(_splash):
    try:
        _p = _splash.parent()
        if _p is not None and _splash.size() != _p.size():
            _splash.resize(_p.size())
    except Exception:
        pass

try:
    _CoveSplashCls = _LoadingSplash
    if not getattr(_CoveSplashCls, '_cove_snap', False):
        _cove_orig_paint = _CoveSplashCls.paintEvent
        def _cove_splash_paint(self, *a, **k):
            _cove_snap_splash_to_window(self)
            return _cove_orig_paint(self, *a, **k)
        _CoveSplashCls.paintEvent = _cove_splash_paint
        _CoveSplashCls._cove_snap = True
except Exception:
    pass

# --- splash live-size hardening (auto) ---
def _cove_install_splash_live_sizing():
    """Re-snap the boot splash to the main window live size on every repaint."""
    try:
        from PyQt6 import sip
        from PyQt6.QtWidgets import QWidget
    except Exception:
        return
    cls = globals().get("_LoadingSplash")
    if cls is None or getattr(cls, '_cove_live_sized', False):
        return
    orig = getattr(cls, 'paintEvent', None) or QWidget.paintEvent

    def paintEvent(self, e):
        try:
            win = self.parent()
            if win is not None and not sip.isdeleted(win):
                w, h = int(win.width()), int(win.height())
                if w > 0 and h > 0 and (self.width() != w or self.height() != h):
                    self.resize(w, h)
        except Exception:
            pass
        return orig(self, e)

    cls.paintEvent = paintEvent
    cls._cove_live_sized = True


_cove_install_splash_live_sizing()


# --- cove splash size-snap (injected) ---
def _cove_install_splash_size_snap():
    cls = globals().get('_LoadingSplash')
    if cls is None:
        return
    orig = getattr(cls, 'paintEvent', None)
    if getattr(orig, '_cove_snap', False):
        return
    def paintEvent(self, event):
        try:
            win = self.window()
            if win is not None and win is not self:
                w, h = win.width(), win.height()
                if w > 0 and h > 0 and (self.width() != w or self.height() != h):
                    self.resize(w, h)
        except Exception:
            pass
        if orig is not None:
            orig(self, event)
    paintEvent._cove_snap = True
    cls.paintEvent = paintEvent

_cove_install_splash_size_snap()


# --- Cove: lock the boot splash to the main window LIVE size ---------------
# A frameless window settles to its true geometry slightly after show() on
# Windows (DWM shadow + title-bar adjust), so a splash sized once at
# construction freezes at the wrong size and never matches the main window.
# This (1) installs an event filter on the parent window so every window
# resize re-snaps the splash, and (2) re-snaps on every splash repaint. Both
# are guarded so a same-size update never triggers a resize loop. Fully
# decoupled from the window own _splash reference. Idempotent + safe.
if "_LoadingSplash" in globals() and not getattr(_LoadingSplash, "_cove_size_locked", False):
    _cove_splash_init_orig = _LoadingSplash.__init__
    _cove_splash_paint_orig = _LoadingSplash.paintEvent

    def _cove_splash_init(self, *a, **k):
        _cove_splash_init_orig(self, *a, **k)
        try:
            p = self.parent()
            if p is not None:
                p.installEventFilter(self)
        except Exception:
            pass

    def _cove_splash_event_filter(self, obj, ev):
        try:
            from PyQt6.QtCore import QEvent
            if obj is self.parent() and ev.type() == QEvent.Type.Resize:
                p = self.parent()
                if p is not None and (self.width() != p.width() or self.height() != p.height()):
                    self.resize(p.width(), p.height())
        except Exception:
            pass
        return False

    def _cove_splash_paint(self, ev):
        try:
            p = self.parent()
            if p is not None and (self.width() != p.width() or self.height() != p.height()):
                self.resize(p.width(), p.height())
        except Exception:
            pass
        return _cove_splash_paint_orig(self, ev)

    _LoadingSplash.__init__ = _cove_splash_init
    _LoadingSplash.eventFilter = _cove_splash_event_filter
    _LoadingSplash.paintEvent = _cove_splash_paint
    _LoadingSplash._cove_size_locked = True


# === Cove boot-splash live-size sync (auto-added) ==========================
# A frameless window settles to its true geometry slightly AFTER show() on
# Windows (DWM shadow + title-bar adjust), so a splash sized once at
# construction freezes at the wrong size. This locks the splash to the main
# window live size, independent of any _splash reference the window drops.
def _cove_splash_size_sync_install():
    try:
        from PyQt6.QtCore import QEvent, QTimer
        from PyQt6.QtWidgets import QApplication
    except Exception:
        return False
    splash_cls = globals().get("_LoadingSplash")
    if splash_cls is None or getattr(splash_cls, "_cove_size_sync", False):
        return False

    def _snap(self):
        try:
            target = self.parent()
            if target is None:
                for w in QApplication.topLevelWidgets():
                    if type(w).__name__ == "LauncherWindow":
                        target = w
                        break
            if target is None:
                return
            pw, ph = int(target.width()), int(target.height())
            if pw <= 0 or ph <= 0:
                return
            if self.parent() is target:
                if (self.width() != pw or self.height() != ph
                        or self.x() != 0 or self.y() != 0):
                    self.setGeometry(0, 0, pw, ph)
            else:
                if self.width() != pw or self.height() != ph:
                    self.resize(pw, ph)
        except Exception:
            pass

    _orig_show = splash_cls.showEvent
    _orig_filter = splash_cls.eventFilter

    def _show(self, e):
        try:
            _orig_show(self, e)
        except Exception:
            pass
        try:
            p = self.parent()
            if p is not None and not getattr(self, "_cove_filter_on", False):
                p.installEventFilter(self)
                self._cove_filter_on = True
        except Exception:
            pass
        _snap(self)
        for _d in (0, 30, 90, 200, 400, 700):
            try:
                QTimer.singleShot(_d, lambda s=self: _snap(s))
            except Exception:
                pass

    def _event_filter(self, obj, e):
        try:
            if obj is self.parent() and e.type() == QEvent.Type.Resize:
                _snap(self)
        except Exception:
            pass
        try:
            return _orig_filter(self, obj, e)
        except Exception:
            return False

    splash_cls.showEvent = _show
    splash_cls.eventFilter = _event_filter
    splash_cls._cove_size_sync = True
    return True

try:
    _cove_splash_size_sync_install()
except Exception:
    pass
