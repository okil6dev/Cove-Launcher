"""Mods page: PrismLauncher-style mod browser for Modrinth / CurseForge.

Layout
------
[nav rail] | [search bar................................................]
           | [ splitter: mod-card list | description preview          ]
           | [sort combo]              [version combo][select button]
           |                    [Review and confirm][Cancel][Help]

Backend contract preserved from the previous design: the window supplies
``get_instance`` / ``log`` / ``toast`` / ``get_cf_key`` callables and may call
:meth:`ModsPage.set_busy`, :meth:`ModsPage.on_instance_changed` and
:meth:`ModsPage.refresh_installed`.
"""

from __future__ import annotations

import concurrent.futures
import html

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import ComboBox, FluentIcon, LineEdit, PushButton

from PyQt6 import sip

from ..constants import (
    PROJECT_TYPE_PLUGIN,
    SOURCE_AI,
    SOURCE_CURSEFORGE,
    SOURCE_MODRINTH,
    SOURCE_PLUGINS,
)
from ..instances import Instance
from ..page_scroll import scrollable_page_root
from ..workers_net import ModInstallThread, ModSearchThread, ModDescriptionThread

# ---------------------------------------------------------------------------
# Slate / teal theme used by this page only (light-mode variant computed from
# the global palette in ``launcher.theme``).
# ---------------------------------------------------------------------------
def _build_qss() -> str:
    from ..theme import fg, fg_muted, fg_faint, fg_disabled

    return f"""
#modsPage {{ background: transparent; border: none; outline: none; border-width: 0; }}
#navRail  {{ background: transparent; border: none; outline: none; border-width: 0; border-right: none; border-style: none; }}
QSplitter::handle {{ background: transparent; border: none; }}
QSplitter::handle:hover {{ background: transparent; }}
QSplitter::handle:pressed {{ background: transparent; }}

QPushButton#railTab {{
    color: {fg_muted()}; background: transparent; border: none;
    padding: 12px 10px; text-align: left; font-weight: bold;
}}
QPushButton#railTab:hover {{ color: {fg()}; background: transparent; }}
QPushButton#railTab:checked {{
    color: {fg()}; background: transparent; border: none;
}}

#lineEditSearch {{
    background: transparent; color: {fg()};
    border: none; border-radius: 6px;
    padding: 8px 14px; font-size: 15px;
}}
#lineEditSearch:focus {{ border: none; }}

QListWidget#cardList {{
    background: transparent; border: none; border-radius: 6px;
    outline: none;
}}
QListWidget#cardList::item {{ border: none; }}
QListWidget#cardList::item:selected {{ background-color: transparent; }}
QListWidget#cardList::item:hover:!selected {{ background-color: transparent; }}

#cardTitle   {{ color: {fg()}; font-weight: bold; font-size: 14px; }}
#cardSummary {{ color: {fg_muted()}; font-size: 11px; }}
#cardMeta    {{ color: {fg_faint()}; font-size: 10px; }}
#iconHolder  {{ background: transparent; border-radius: 4px; }}

QTextBrowser#previewPane {{
    background: transparent; color: {fg_muted()};
    border: none; border-radius: 6px; padding: 10px;
}}

ComboBox, QPushButton {{
    background-color: transparent; color: {fg_muted()};
    border: none; border-radius: 5px; padding: 6px 12px;
}}
ComboBox:hover, QPushButton:hover {{ border: none; background: transparent; }}
QPushButton:checked {{ background-color: transparent; color: {fg()}; }}
QPushButton:disabled {{ color: {fg_disabled()}; }}
#primaryBtn {{ background-color: transparent; color: {fg()}; font-weight: bold; }}
#primaryBtn:hover {{ background-color: transparent; }}

#statusLabel, #versionLabel {{ color: {fg_muted()}; }}

QWidget {{ background: transparent; border: none; }}
QStackedWidget {{ background: transparent; border: none; }}
QComboBox::drop-down {{ background: transparent; border: none; }}
QComboBox QAbstractItemView {{ background: transparent; color: {fg_muted()}; border: none; selection-background-color: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 12px; border-radius: 6px; border: none; }}
QScrollBar::handle:vertical {{ background: transparent; border-radius: 6px; min-height: 20px; border: none; }}
QScrollBar::handle:vertical:hover {{ background: transparent; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; border-radius: 6px; border: none; }}
QScrollBar::handle:horizontal {{ background: transparent; border-radius: 6px; min-width: 20px; border: none; }}
QScrollBar::handle:horizontal:hover {{ background: transparent; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
"""


_EXTRA_QSS = """
QWidget { background: transparent; }
QWidget#navRail { background: transparent; }
"""


# ---------------------------------------------------------------------------
# Async icon loading (keeps thumbnail downloads off the GUI thread).
# ---------------------------------------------------------------------------
class IconFetcher(QObject):
    """Background worker that downloads card thumbnails concurrently using a ThreadPool."""

    loaded = pyqtSignal(str, bytes)  # url, raw image bytes

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._requested: set[str] = set()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=16)

    def fetch(self, url: str) -> None:
        if not url or url in self._requested:
            return
        self._requested.add(url)
        self._executor.submit(self._download, url)

    def _download(self, url: str) -> None:
        import requests
        try:
            resp = requests.get(
                url, timeout=10, headers={"User-Agent": "Cove/1.1"}
            )
            if resp.status_code == 200 and resp.content:
                self.loaded.emit(url, resp.content)
        except Exception:  # noqa: BLE001 - thumbnails are best-effort
            pass

    def stop(self) -> None:
        self._executor.shutdown(wait=False)


def _fmt_downloads(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


# ---------------------------------------------------------------------------
# One row in the mod list: [checkbox][icon][title + summary + meta]
# ---------------------------------------------------------------------------
class ModCard(QWidget):
    def __init__(self, row: dict, installed: bool, parent=None) -> None:
        super().__init__(parent)
        self.row = row
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        self.checkbox = QCheckBox()
        # Explicit indicator styling keeps the box visible on transparent
        # backgrounds (native unchecked indicators can be near-invisible).
        self._apply_indicator_style()
        lay.addWidget(self.checkbox)

        self.icon_label = QLabel("?")
        self.icon_label.setObjectName("iconHolder")
        self.icon_label.setFixedSize(48, 48)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.icon_label)

        text = QVBoxLayout()
        text.setSpacing(1)
        self.title_label = QLabel()
        self.title_label.setObjectName("cardTitle")
        self.summary_label = QLabel(row.get("description") or "")
        self.summary_label.setObjectName("cardSummary")
        self.meta_label = QLabel()
        self.meta_label.setObjectName("cardMeta")
        text.addWidget(self.title_label)
        text.addWidget(self.summary_label)
        text.addWidget(self.meta_label)
        lay.addLayout(text, stretch=1)

        self.set_installed(installed)

    def _apply_indicator_style(self) -> None:
        from ..theme import accent_edge, indicator_bg

        edge = accent_edge()
        self.checkbox.setStyleSheet(
            "QCheckBox::indicator{width:16px;height:16px;"
            f"border:1px solid {edge};border-radius:3px;background:{indicator_bg()};}}"
            f"QCheckBox::indicator:checked{{background:{edge};}}"
        )

    def refresh_theme(self) -> None:
        self._apply_indicator_style()

    def set_installed(self, installed: bool) -> None:
        from ..theme import accent_edge

        suffix = f"   <span style='color:{accent_edge()};'>[installed]</span>" \
            if installed else ""
        self.title_label.setText(
            html.escape(str(self.row.get("title") or "?")) + suffix
        )
        author = html.escape(str(self.row.get("author") or "?"))
        src = "Modrinth" if self.row.get("source") == SOURCE_MODRINTH else "CurseForge"
        self.meta_label.setText(
            f"{src} · by {author} · "
            f"{_fmt_downloads(self.row.get('downloads', 0))} downloads"
        )

    def set_pixmap(self, pixmap) -> None:
        self.icon_label.setText("")
        self.icon_label.setPixmap(pixmap.scaled(
            48, 48,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        ))

# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------
class ModsPage(QWidget):
    """Prism-style mod browser for the instance chosen on the Instances page."""

    SORTS = [
        ("Relevance", None),
        ("Downloads", lambda r: -int(r.get("downloads", 0) or 0)),
        ("Name (A-Z)", lambda r: str(r.get("title") or "").lower()),
    ]

    def __init__(
        self,
        get_instance,   # callable -> Instance | None
        log,            # callable(message, level)
        toast,          # callable(severity, title, content)
        get_cf_key,     # callable -> str
        get_vault=None, # callable -> Vault (AI endpoint/key storage)
        get_server=None,  # callable -> Server | None (plugin installs)
        get_servers=None,  # callable -> list[Server] (mod-target picker)
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("modsPage")
        self.setStyleSheet(_build_qss() + _EXTRA_QSS)
        self._get_instance = get_instance
        self._get_server = get_server or (lambda: None)
        self._get_servers = get_servers or (lambda: [])
        self._log_cb = log
        self._toast = toast
        self._get_cf_key = get_cf_key
        self._get_vault = get_vault or (lambda: None)

        self._results: list[dict] = []
        self._cards: dict[str, ModCard] = {}
        self._pixmaps: dict[str, object] = {}
        # Ticked project ids and their row data survive list rebuilds (new search / re-sort),
        # so a selection is not silently lost when browsing further.
        self._checked_mods: dict[str, dict] = {}
        # Also store titles as fallback for matching
        self._checked_titles: set[str] = set()
        self._is_populating = False
        self._offset = 0
        self._has_reached_end = False
        self._is_loading_more = False
        self._source = SOURCE_MODRINTH
        self._search_thread: ModSearchThread | None = None
        self._install_thread: ModInstallThread | None = None
        self._desc_thread: ModDescriptionThread | None = None
        self._install_queue: list[dict] = []
        self._install_total = 0
        self._installed_files: list[str] = []

        # Scrollable content: the AI top bar + browser rows demand ~1700px,
        # which would otherwise clamp the boot window wider than 1280x820.
        root = scrollable_page_root(self, margins=(0, 0, 0, 0), spacing=0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root.addLayout(body, stretch=1)

        # -- left navigation rail ---------------------------------------------
        rail = QWidget()
        rail.setObjectName("navRail")
        rail.setFixedWidth(120)
        rail_lay = QVBoxLayout(rail)
        rail_lay.setContentsMargins(0, 12, 0, 12)
        rail_lay.setSpacing(2)
        self.rail_modrinth = self._make_rail_tab("Modrinth", SOURCE_MODRINTH)
        self.rail_curseforge = self._make_rail_tab("CurseForge", SOURCE_CURSEFORGE)
        self.rail_ai = self._make_rail_tab("AI Installer", SOURCE_AI)
        self.rail_plugins = self._make_rail_tab("Plugins", SOURCE_PLUGINS)
        rail_lay.addWidget(self.rail_modrinth)
        rail_lay.addWidget(self.rail_curseforge)
        rail_lay.addWidget(self.rail_ai)
        # "under the AI thing": the plugin browser sits below the AI tab.
        rail_lay.addWidget(self.rail_plugins)
        rail_lay.addStretch(1)
        body.addWidget(rail)

        # -- main column --------------------------------------------------------
        main = QVBoxLayout()
        main.setContentsMargins(14, 12, 14, 10)
        main.setSpacing(10)
        body.addLayout(main, stretch=1)

        # The mod browser and the AI assistant share this column; the rail
        # switches between them. Index 0 is always the browser.
        self.view_stack = QStackedWidget()
        main.addWidget(self.view_stack, stretch=1)

        browser = QWidget()
        main_layout = QVBoxLayout(browser)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(10)
        self.view_stack.addWidget(browser)

        self.search_edit = LineEdit()
        self.search_edit.setObjectName("lineEditSearch")
        self.search_edit.setPlaceholderText("Search for mods...")
        main_layout.addWidget(self.search_edit)

        # central split view ------------------------------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet(
            "QSplitter::handle { background: transparent; }"
        )

        self.card_list = QListWidget()
        self.card_list.setObjectName("cardList")
        self.card_list.setVerticalScrollMode(
            QListWidget.ScrollMode.ScrollPerPixel
        )
        self.card_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        splitter.addWidget(self.card_list)

        self.preview = QTextBrowser()
        self.preview.setObjectName("previewPane")
        self.preview.setMinimumWidth(240)
        self.preview.setOpenExternalLinks(True)
        # Keep default scrollbars for stability (avoid resizing entire GUI)
        from ..theme import fg_faint

        self.preview.setHtml(
            f"<div style='color:{fg_faint()}'>Search for mods, then click one to "
            "read its description here.</div>"
        )
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 11)
        splitter.setStretchFactor(1, 5)
        # keep splitter reference if needed
        self.splitter = splitter
        main_layout.addWidget(splitter, stretch=1)


        # bottom control row ------------------------------------------------------
        controls = QHBoxLayout()
        self.sort_combo = ComboBox()
        for label, _key in self.SORTS:
            self.sort_combo.addItem(f"Sort by {label}")
        self.status_label = QLabel("Select an instance first.")
        self.status_label.setObjectName("statusLabel")
        controls.addWidget(self.sort_combo)
        controls.addWidget(self.status_label, stretch=1)
        # "Install to:" -- lets the user choose an instance OR a mod-capable
        # server as the mod destination, exactly like picking an instance.
        self.target_label = QLabel("Install to:")
        self.target_label.setObjectName("versionLabel")
        self.target_combo = ComboBox()
        self.target_combo.setMinimumWidth(150)
        controls.addWidget(self.target_label)
        controls.addWidget(self.target_combo)
        self.version_label = QLabel("Version selected:")
        self.version_label.setObjectName("versionLabel")
        self.version_combo = ComboBox()
        self.version_combo.addItem("-")
        self.select_button = PushButton("Select mod for download")
        self.select_button.setObjectName("primaryBtn")
        controls.addWidget(self.version_label)
        controls.addWidget(self.version_combo)
        controls.addWidget(self.select_button)
        main_layout.addLayout(controls)

        # bottom action row -----------------------------------------------------------
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.confirm_button = PushButton(FluentIcon.ACCEPT, "Review and confirm")
        self.cancel_button = PushButton(FluentIcon.CLOSE, "Cancel")
        self.help_button = PushButton(FluentIcon.QUESTION, "Help")
        for b in (self.confirm_button, self.cancel_button, self.help_button):
            actions.addWidget(b)
        main_layout.addLayout(actions)

        # -- AI assistant view (second page of the stack) ------------------------
        from .ai_mods_view import AiModsView

        self.ai_view = AiModsView(
            get_instance=self._instance,
            log=self._log,
            toast=self._toast,
            get_cf_key=self._get_cf_key,
            get_vault=self._get_vault,
            get_server=self._server,
            parent=self,
        )
        self.ai_view.installRequested.connect(self._on_ai_install_requested)
        self.view_stack.addWidget(self.ai_view)   # index 1 = AI assistant

        # wiring ------------------------------------------------------------------------
        self.search_edit.returnPressed.connect(self._on_search)
        self.rail_modrinth.clicked.connect(
            lambda: self._switch_source(SOURCE_MODRINTH))
        self.rail_curseforge.clicked.connect(
            lambda: self._switch_source(SOURCE_CURSEFORGE))
        self.rail_ai.clicked.connect(
            lambda: self._switch_source(SOURCE_AI))
        self.rail_plugins.clicked.connect(
            lambda: self._switch_source(SOURCE_PLUGINS))
        self.sort_combo.currentIndexChanged.connect(lambda _: self._populate_list())
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        self.card_list.currentRowChanged.connect(self._on_card_selected)
        self.select_button.clicked.connect(self._on_select_current)
        self.confirm_button.clicked.connect(self._on_review_confirm)
        self.cancel_button.clicked.connect(self._on_cancel)
        self.help_button.clicked.connect(self._on_help)
        self.card_list.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self._icon_fetcher = IconFetcher(self)
        self._icon_fetcher.loaded.connect(self._on_icon_loaded)
        self.on_instance_changed()

    # -- construction helpers ----------------------------------------------------------
    def _make_rail_tab(self, label: str, source: str) -> PushButton:
        btn = PushButton(label)
        btn.setObjectName("railTab")
        btn.setCheckable(True)
        btn.setChecked(source == SOURCE_MODRINTH)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def _switch_source(self, source: str) -> None:
        if source == self._source:
            return
        self._source = source
        self.rail_modrinth.setChecked(source == SOURCE_MODRINTH)
        self.rail_curseforge.setChecked(source == SOURCE_CURSEFORGE)
        self.rail_ai.setChecked(source == SOURCE_AI)
        self.rail_plugins.setChecked(source == SOURCE_PLUGINS)

        if source == SOURCE_AI:
            # The AI view owns its own prompt box, so the browser's search
            # behaviour (and its search bar) must not run here.
            self._log("Switched to the AI installer.")
            self.view_stack.setCurrentWidget(self.ai_view)
            self.ai_view.set_instance(self._instance())
            self.ai_view.set_server(self._server())
            return

        self.view_stack.setCurrentIndex(0)
        if source == SOURCE_PLUGINS:
            self.search_edit.setPlaceholderText("Search for plugins...")
            self._log("Switched to the plugin browser.")
            # Plugins always go to the selected server; hide the picker.
            self.target_label.setVisible(False)
            self.target_combo.setVisible(False)
            server = self._server()
            if server is None:
                self.status_label.setText(
                    "Select a server on the Server tab for plugins.")
                self._clear_card_list()
                return
            self.status_label.setText(f"Plugin target: {server.name}")
            if server.supports_plugins:
                self._on_search()
            else:
                self.status_label.setText(
                    f"'{server.name}' cannot run plugins.")
                self._clear_card_list()
            return

        self.search_edit.setPlaceholderText("Search for mods...")
        self._log(f"Mod source switched to {source}.")
        # Mods: show the "Install to:" picker and act on the current pick.
        self.target_label.setVisible(True)
        self.target_combo.setVisible(True)
        self.on_instance_changed()

    def _instance(self) -> Instance | None:
        return self._get_instance()

    def _server(self):
        """The server selected on the Server tab (or None)."""
        return self._get_server()

    # -- mod install target (instance OR server) --------------------------------
    # The mod browser used to hard-code the selected instance as the only
    # destination. These helpers let the user pick an instance or any
    # mod-capable server from the "Install to:" combo, and every search /
    # refresh / install path resolves its target through _browser_target()
    # so they can never drift apart.
    def _mod_capable_servers(self):
        """Every configured server that can actually receive mods."""
        from ..servers import mod_loader_for

        out = []
        try:
            servers = self._get_servers()
        except Exception:  # noqa: BLE001 - registry hiccup must not break UI
            servers = []
        for server in servers or []:
            if (getattr(server, "supports_mods", False)
                    and getattr(server, "mc_version", "")
                    and mod_loader_for(getattr(server, "server_type", ""))):
                out.append(server)
        return out

    def _preferred_server(self):
        """The mod-capable server to default to: the Server-tab selection if
        it qualifies, else the first mod-capable server in the registry."""
        selected = self._server()
        capable = self._mod_capable_servers()
        if (selected is not None and getattr(selected, "supports_mods", False)
                and getattr(selected, "mc_version", "")
                and any(s.name == selected.name for s in capable)):
            return selected
        return capable[0] if capable else None

    def _populate_target_combo(self) -> None:
        """Rebuild the "Install to:" list, keeping the current pick if valid."""
        from ..servers import mod_loader_for

        # Remember the current selection so a refresh does not yank it away.
        prev = self.target_combo.currentData()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        inst = self._instance()
        has_instance = inst is not None and getattr(
            inst, "loader_id", "") != "vanilla"
        if has_instance:
            self.target_combo.addItem(
                f"Instance: {inst.name}", userData=("instance", inst.name))
        for server in self._mod_capable_servers():
            label = (mod_loader_for(server.server_type) or server.server_type)
            self.target_combo.addItem(
                f"Server: {server.name} ({label})",
                userData=("server", server.name))
        # Restore the previous pick when it still exists, else default to the
        # instance (if any), else the preferred (Server-tab) server.
        idx = -1
        if prev is not None:
            for i in range(self.target_combo.count()):
                if self.target_combo.itemData(i) == prev:
                    idx = i
                    break
        if idx < 0:
            if has_instance:
                idx = 0
            else:
                pref = self._preferred_server()
                if pref is not None:
                    for i in range(self.target_combo.count()):
                        data = self.target_combo.itemData(i)
                        if data and data[0] == "server" and data[1] == pref.name:
                            idx = i
                            break
                if idx < 0 and self.target_combo.count():
                    idx = 0
        if idx >= 0:
            self.target_combo.setCurrentIndex(idx)
        self.target_combo.blockSignals(False)

    def _browser_target(self):
        """``(kind, obj)`` for the current "Install to:" pick.

        ``kind`` is ``"instance"`` or ``"server"``; ``obj`` is the live
        Instance/Server. Falls back to the old behaviour (instance wins,
        then the preferred mod-capable server) when the combo is empty.
        """
        data = self.target_combo.currentData()
        if data:
            kind, name = data
            if kind == "instance":
                inst = self._instance()
                if inst is not None and inst.name == name:
                    return "instance", inst
            else:
                for server in self._mod_capable_servers():
                    if server.name == name:
                        return "server", server
        # Combo empty / stale: legacy resolution.
        inst = self._instance()
        if inst is not None and getattr(inst, "loader_id", "") != "vanilla":
            return "instance", inst
        pref = self._preferred_server()
        if pref is not None:
            return "server", pref
        return None, None

    def _browser_target_dims(self, kind, obj):
        """``(dest_dir, game_version, loader)`` for a resolved target."""
        if kind == "instance":
            return obj.mods_dir, obj.mc_version, obj.loader_id
        from ..servers import mod_loader_for

        return (obj.mods_dir, obj.mc_version,
                mod_loader_for(obj.server_type) or "forge")

    def _on_target_changed(self, _index: int) -> None:
        """User picked a new destination: re-label, refresh, re-search."""
        if self._source == SOURCE_PLUGINS:
            return  # plugin browser ignores the combo (always the server)
        kind, obj = self._browser_target()
        self.version_combo.clear()
        if obj is None:
            self.status_label.setText(
                "No mod target. Pick an instance or a mod-capable server.")
            self.version_combo.addItem("-")
            self._clear_card_list()
            self.refresh_installed()
            return
        dest_dir, game_version, loader = self._browser_target_dims(kind, obj)
        self.version_combo.addItem(f"{game_version or '?'} ({loader})")
        self.status_label.setText(f"Target: {obj.name}")
        self.refresh_installed()
        self._on_search()

    # -- external contract (used by LauncherWindow) --------------------------------------
    def set_busy(self, busy: bool, is_pagination: bool = False) -> None:
        if is_pagination:
            return
        self.search_edit.setEnabled(not busy)
        self.select_button.setEnabled(not busy)
        self.confirm_button.setEnabled(not busy)

    def _clear_card_list(self) -> None:
        """
        Empty the mod list AND forget its Python wrappers.

        ``QListWidget.clear()`` destroys the embedded card widgets' C++
        objects while ``self._cards`` keeps the dead wrappers behind — any
        later refresh iterating them aborted with "RuntimeError: wrapped
        C/C++ object of type QLabel has been deleted". Always go through
        this helper so both stay in sync.
        """
        self.card_list.clear()
        self._cards.clear()

    def on_instance_changed(self) -> None:
        inst = self._instance()
        # The AI view gates its own prompt/send/install on this.
        self.ai_view.set_instance(inst)
        self.ai_view.set_server(self._server())
        self.version_combo.clear()
        if self._source == SOURCE_PLUGINS:
            server = self._server()
            if server is None:
                self.status_label.setText(
                    "Select a server on the Server tab for plugins.")
                self.version_combo.addItem("-")
                self._clear_card_list()
                self.refresh_installed()
                return
            self.version_combo.addItem(
                f"{server.mc_version or '?'} ({server.server_type})")
            self.status_label.setText(f"Plugin target: {server.name}")
            self.refresh_installed()
            if server.supports_plugins and server.mc_version:
                self._on_search()
            else:
                self._clear_card_list()
            return
        # Mod browser: rebuild the "Install to:" list, then act on the pick.
        self._populate_target_combo()
        kind, obj = self._browser_target()
        if obj is None:
            self.status_label.setText(
                "No mod target. Pick an instance or a mod-capable server.")
            self.version_combo.addItem("-")
            self._clear_card_list()
            self.refresh_installed()
            return
        dest_dir, game_version, loader = self._browser_target_dims(kind, obj)
        self.version_combo.addItem(f"{game_version or '?'} ({loader})")
        self.status_label.setText(f"Target: {obj.name}")
        self.refresh_installed()
        self._on_search()

    def refresh_theme(self) -> None:
        """Re-apply theme-dependent styling (called on live theme change)."""
        self.setStyleSheet(_build_qss() + _EXTRA_QSS)
        self.ai_view.refresh_theme()
        for card in self._cards.values():
            card.refresh_theme()

    def refresh_installed(self) -> None:
        from .. import moddb

        if self._source == SOURCE_PLUGINS:
            # "Installed" means jars sitting in the selected server's
            # plugins folder, not the instance's mods folder.
            server = self._server()
            self._installed_files = (
                moddb.list_installed_mods(server.plugins_dir)
                if server else []
            )
        else:
            # The chosen "Install to:" target (instance or mod-capable server).
            kind, obj = self._browser_target()
            self._installed_files = (
                moddb.list_installed_mods(obj.mods_dir) if obj else []
            )
        for card in self._cards.values():
            if sip.isdeleted(card):
                continue
            card.set_installed(self._is_installed(card.row))

    def _is_installed(self, row: dict) -> bool:
        slug = str(row.get("slug") or "").lower().replace("-", "")
        title = str(row.get("title") or "").lower().replace(" ", "")
        for fname in self._installed_files:
            fn = fname.lower().replace("-", "").replace("_", "")
            if slug and slug in fn:
                return True
            if title and title in fn:
                return True
        return False

    # -- search ------------------------------------------------------------------------------
    def _on_search(self) -> None:
        if self._source == SOURCE_PLUGINS:
            self._search_plugins()
            return
        kind, obj = self._browser_target()
        if obj is None:
            self._toast(
                "warning", "No mod target",
                "Pick an instance or a mod-capable server to install into.")
            return

        self._offset = 0
        self._has_reached_end = False
        self._is_loading_more = False

        _dest_dir, game_version, loader = self._browser_target_dims(kind, obj)
        query = self.search_edit.text().strip()
        self.status_label.setText("Searching...")
        self.set_busy(True)
        from ..threading_utils import retire
        retire(self._search_thread)
        self._search_gen = getattr(self, "_search_gen", 0) + 1
        self._search_thread = ModSearchThread(
            source=self._source,
            query=query,
            game_version=game_version,
            loader=loader,
            curseforge_api_key=self._get_cf_key(),
            limit=20,
            offset=0,
            generation=self._search_gen,
            parent=self,
        )
        self._search_thread.ready.connect(
            lambda gen, rows: self._on_results(self._source, rows, gen)
        )
        self._search_thread.failed.connect(self._on_search_failed)
        self._search_thread.start()

    def _search_plugins(self) -> None:
        """Plugin browser: targets the selected SERVER, never an instance."""
        from ..servers import plugin_loader_for

        server = self._server()
        if server is None:
            self._toast("warning", "No server",
                        "Select a server on the Server tab first.")
            return
        if not server.supports_plugins:
            self._toast(
                "warning",
                "Server cannot run plugins",
                f"'{server.name}' ({server.server_type}) has no plugin "
                "support. Use a Paper/Spigot or dual-loader jar.",
                6000,
            )
            return
        if not server.mc_version:
            self._toast("warning", "Unknown server version",
                        "Re-import the server jar so its MC version can be "
                        "detected.")
            return
        loader = plugin_loader_for(server.server_type) or "spigot"

        self._offset = 0
        self._has_reached_end = False
        self._is_loading_more = False

        query = self.search_edit.text().strip()
        # Deliberately does NOT name the server: the query comes from the
        # search box, and a status like "Searching plugins for '<name>'"
        # makes it look like the server name is the search term.
        self.status_label.setText("Searching plugins...")
        self.set_busy(True)
        from ..threading_utils import retire
        retire(self._search_thread)
        self._search_gen = getattr(self, "_search_gen", 0) + 1
        self._search_thread = ModSearchThread(
            source=SOURCE_PLUGINS,
            query=query,
            game_version=server.mc_version,
            loader=loader,
            curseforge_api_key="",
            limit=20,
            offset=0,
            project_type=PROJECT_TYPE_PLUGIN,
            generation=self._search_gen,
            parent=self,
        )
        self._search_thread.ready.connect(
            lambda gen, rows: self._on_results(SOURCE_PLUGINS, rows, gen)
        )
        self._search_thread.failed.connect(self._on_search_failed)
        self._search_thread.start()

        

    # -- results rendering -------------------------------------------------------------------
    def _sorted_results(self) -> list[dict]:
        idx = self.sort_combo.currentIndex()
        key = self.SORTS[idx][1] if 0 <= idx < len(self.SORTS) else None
        rows = list(self._results)
        if key is not None:
            rows.sort(key=key)
        return rows

    def _on_checkbox_toggled(self, checked: bool, row: dict) -> None:
        """Handle checkbox toggle and preserve selection."""
        if self._is_populating:
            return
        pid = str(row.get("project_id"))
        title = str(row.get("title") or "").lower()
        if checked:
            self._checked_mods[pid] = row
            self._checked_titles.add(title)
        else:
            self._checked_mods.pop(pid, None)
            self._checked_titles.discard(title)

    def _populate_list(self) -> None:
        self._is_populating = True
        try:
            self._clear_card_list()
            for row in self._sorted_results():
                pid = str(row.get("project_id"))
                title = str(row.get("title") or "").lower()
                card = ModCard(row, self._is_installed(row))
                # Restore the tick from a previous list build (search/sort change)
                # Try both project ID and title for matching
                if pid in self._checked_mods or title in self._checked_titles:
                    card.checkbox.setChecked(True)
                card.checkbox.toggled.connect(
                    lambda on, _row=row: self._on_checkbox_toggled(on, _row)
                )
                item = QListWidgetItem()
                item.setSizeHint(card.sizeHint())
                self.card_list.addItem(item)
                self.card_list.setItemWidget(item, card)
                self._cards[pid] = card
                url = str(row.get("icon_url") or "")
                if url:
                    cached = self._pixmaps.get(url)
                    if cached is not None:
                        card.set_pixmap(cached)
                    else:
                        self._icon_fetcher.fetch(url)
        finally:
            self._is_populating = False

    def _append_results(self, new_rows: list[dict]) -> None:
        self._is_populating = True
        try:
            for row in new_rows:
                pid = str(row.get("project_id"))
                title = str(row.get("title") or "").lower()
                card = ModCard(row, self._is_installed(row))
                if pid in self._checked_mods or title in self._checked_titles:
                    card.checkbox.setChecked(True)
                card.checkbox.toggled.connect(
                    lambda on, _row=row: self._on_checkbox_toggled(on, _row)
                )
                item = QListWidgetItem()
                item.setSizeHint(card.sizeHint())
                self.card_list.addItem(item)
                self.card_list.setItemWidget(item, card)
                self._cards[pid] = card
                url = str(row.get("icon_url") or "")
                if url:
                    cached = self._pixmaps.get(url)
                    if cached is not None:
                        card.set_pixmap(cached)
                    else:
                        self._icon_fetcher.fetch(url)
        finally:
            self._is_populating = False

    def _on_scroll(self, value: int) -> None:
        if self._is_loading_more or self._has_reached_end:
            return
        scrollbar = self.card_list.verticalScrollBar()
        if value > scrollbar.maximum() * 0.85:
            self._load_next_page()

    def _load_next_page(self) -> None:
        # Cooperative cancel + delete of whatever search is still in flight.
        # (A previous "improvement" checked `'retire' in globals()`, which is
        # always False for a function-local import, so every call fell through
        # to a raw quit()/wait() and skipped the cancellation protocol.)
        from ..threading_utils import retire

        retire(self._search_thread)

        if self._source == SOURCE_PLUGINS:
            # Plugin paging must follow the SERVER with the plugin project
            # type -- reusing the instance path here would silently fetch
            # mods and append them to the plugin list.
            from ..servers import plugin_loader_for

            server = self._server()
            if (server is None or not server.supports_plugins
                    or not server.mc_version):
                return
            self._is_loading_more = True
            query = self.search_edit.text().strip()
            self.status_label.setText("Loading more plugins...")
            self.set_busy(True, is_pagination=True)
            from ..threading_utils import retire
            retire(self._search_thread)
            self._search_gen = getattr(self, "_search_gen", 0) + 1
            self._search_thread = ModSearchThread(
                source=SOURCE_PLUGINS,
                query=query,
                game_version=server.mc_version,
                loader=plugin_loader_for(server.server_type) or "spigot",
                curseforge_api_key="",
                limit=20,
                offset=self._offset,
                project_type=PROJECT_TYPE_PLUGIN,
                generation=self._search_gen,
                parent=self,
            )
            self._search_thread.ready.connect(
                lambda gen, rows: self._on_results(SOURCE_PLUGINS, rows, gen)
            )
            self._search_thread.failed.connect(self._on_search_failed)
            self._search_thread.start()
            return
        kind, obj = self._browser_target()
        if obj is None:
            return
        self._is_loading_more = True
        query = self.search_edit.text().strip()
        self.status_label.setText("Loading more mods...")
        self.set_busy(True, is_pagination=True)
        retire(self._search_thread)
        self._search_gen = getattr(self, "_search_gen", 0) + 1
        _dest_dir, game_version, loader = self._browser_target_dims(kind, obj)
        self._search_thread = ModSearchThread(
            source=self._source,
            query=query,
            game_version=game_version,
            loader=loader,
            curseforge_api_key=self._get_cf_key(),
            limit=20,
            offset=self._offset,
            generation=self._search_gen,
            parent=self,
        )
        self._search_thread.ready.connect(
            lambda gen, rows: self._on_results(self._source, rows, gen)
        )
        self._search_thread.failed.connect(self._on_search_failed)
        self._search_thread.start()

    def _on_results(self, source: str, rows: list, generation: int = 0) -> None:
        # A superseded search finished late -- its rows belong to an older
        # query, so drop them instead of clobbering the current list.
        if generation and generation != getattr(self, "_search_gen", 0):
            return
        self.set_busy(False)
        self._is_loading_more = False
        
        limit = 20
        if len(rows) < limit:
            self._has_reached_end = True
            
        if self._offset == 0:
            self._results = rows
            self._populate_list()
        else:
            self._results.extend(rows)
            self._append_results(rows)
            
        self._offset += len(rows)
        self.status_label.setText(f"{len(self._results)} results.")
        self._log(f"Mod search page loaded -> {len(rows)} results (total {len(self._results)}).")

    def _on_icon_loaded(self, url: str, data: bytes) -> None:
        from PyQt6.QtGui import QPixmap

        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        self._pixmaps[url] = pix
        for card in self._cards.values():
            if sip.isdeleted(card):
                continue
            if str(card.row.get("icon_url") or "") == url:
                card.set_pixmap(pix)

    def _on_search_failed(self, *args) -> None:
        message = args[-1] if args else "Search failed."
        # Stale search failed late -- ignore it, the new search owns the UI.
        if len(args) == 2 and args[0] != getattr(self, "_search_gen", 0):
            return
        self.set_busy(False)
        self._is_loading_more = False
        self.status_label.setText("Search failed.")
        self._log(message, "ERROR")
        if "API key" in message or "403" in message or "401" in message or "x-api-key" in message:
            self._toast("error", "CurseForge", message, 8000)
        else:
            self._toast("error", "Search failed", message, 6000)

    # -- selection / preview -------------------------------------------------------------------
    def _current_row(self) -> dict | None:
        item = self.card_list.currentItem()
        if item is None:
            return None
        widget = self.card_list.itemWidget(item)
        return widget.row if isinstance(widget, ModCard) else None

    def _on_card_selected(self, _row_index: int) -> None:
        row = self._current_row()
        if row is None:
            return
        e = html.escape
        src = "Modrinth" if row.get("source") == SOURCE_MODRINTH else "CurseForge"
        slug = e(str(row.get("slug") or ""))
        if slug:
            host = ("modrinth.com/mod/" if src == "Modrinth"
                    else "curseforge.com/minecraft/mc-mods/")
            link = f"https://{host}{slug}"
        else:
            link = ""
        from ..theme import fg, fg_muted, accent_edge

        head = (
            f"<h2 style='color:{fg()};margin:4px 0;'>"
            f"{e(str(row.get('title') or '?'))}</h2>"
            f"<p style='color:{fg_muted()};'>by {e(str(row.get('author') or '?'))}"
            f" &nbsp;·&nbsp; {_fmt_downloads(row.get('downloads', 0))} downloads"
            f" &nbsp;·&nbsp; {src}</p>"
        )
        if link:
            head += f"<p><a style='color:{accent_edge()};' href='{link}'>{link}</a></p>"
        if self._is_installed(row):
            head += f"<p style='color:{accent_edge()};'><b>[installed]</b></p>"

        # Show a short summary immediately, then fetch the full description
        short_desc = e(str(row.get("description") or "")).replace("\n", "<br>")
        self.preview.setHtml(head + f"<hr>{short_desc}")

        # Fetch full description off the GUI thread and render when ready.
        from ..threading_utils import retire
        retire(self._desc_thread)
        src = str(row.get("source") or self._source)
        self._desc_thread = ModDescriptionThread(
            source=src,
            project_id=str(row.get("project_id")),
            curseforge_api_key=self._get_cf_key(),
            slug=str(row.get("slug") or ""),
            generation=getattr(self, "_search_gen", 0),
            parent=self,
        )
        self._desc_thread.ready.connect(lambda desc, head=head: self._on_full_description(desc, head))
        self._desc_thread.failed.connect(self._on_desc_failed)
        self._desc_thread.start()

    def _on_desc_failed(self, message: str) -> None:
        # keep the short summary already shown, but log and show a helpful toast
        self._log(f"Failed to fetch full description: {message}", "WARNING")
        # show a brief note in the preview so users know we attempted the full fetch
        try:
            self.preview.append("<p style='color:#d08770;'><i>Could not load full description.</i></p>")
        except Exception:
            pass
        if "API key" in message or "CurseForge" in message or "403" in message or "401" in message:
            self._toast(
                "warning",
                "CurseForge description",
                "Could not fetch the CurseForge description from the API. Falling back to the public page when possible.",
                8000,
            )

    def _on_full_description(self, body_html: str, head: str) -> None:
        """Render the pre-built description HTML.

        The worker has already fetched the description, converted markdown and
        inlined every remote image as a data URI, so this slot only calls
        ``setHtml``. It must stay cheap: it runs on the GUI thread, and doing
        the image downloads here is what froze the whole app.
        """
        # Scale images / iframes / videos to the preview width.
        css = (
            "<style>"
            "img, iframe, video { max-width: 100% !important; height: auto !important; display:block; margin:8px 0; }"
            "</style>"
        )
        body_html = css + "<div style='width:100%;'>" + body_html + "</div>"
        try:
            self._log("Full description rendered.", "DEBUG")
            self.preview.setHtml(head + f"<hr>{body_html}")
        except Exception:
            self._log("Failed to render full description into preview widget.", "WARNING")
            self.preview.setHtml(head + "<hr>" + html.escape(str(body_html)))

    # -- download selection / batch install -------------------------------------------------------
    def _checked_rows(self) -> list[dict]:
        # Return all preserved selections directly from self._checked_mods
        return list(self._checked_mods.values())

    def _on_select_current(self) -> None:
        row = self._current_row()
        if row is None:
            self._toast("warning", "Nothing selected",
                        "Click a mod in the list first.", 4000)
            return
        card = self._cards.get(str(row["project_id"]))
        if card is None or sip.isdeleted(card):
            return
        card.checkbox.setChecked(not card.checkbox.isChecked())

    def _on_review_confirm(self) -> None:
        from PyQt6.QtCore import QTimer

        from ..cove_dialog import CoveConfirm

        rows = self._checked_rows()
        if not rows:
            self._toast("warning", "No mods chosen",
                        "Tick checkboxes or press 'Select mod for download'.",
                        5000)
            return
        is_plugin = (str(rows[0].get("project_type") or "")
                     == PROJECT_TYPE_PLUGIN)
        if is_plugin:
            server = self._server()
            if server is None:
                self._toast("warning", "No server",
                            "Select a server on the Server tab first.")
                return
            if not server.supports_plugins or not server.mc_version:
                self._toast("warning", "Server cannot run plugins",
                            f"'{server.name}' has no usable plugin support.")
                return
            dest_name = server.name
        else:
            _kind, target = self._browser_target()
            if target is None:
                self._toast(
                    "warning", "No mod target",
                    "Pick an instance or a mod-capable server to install into.")
                return
            dest_name = target.name
        kind = "plugin(s)" if is_plugin else "mod(s)"
        names = "\n".join(f"• {r.get('title', '?')}" for r in rows)

        def _perform() -> None:
            # Native Cove dialog: plain QDialog, no Fluent fade animations
            # or parent event-filters, so exec() cannot wedge invisible.
            if not CoveConfirm.ask(
                self.window(),
                "Review and confirm",
                f"Download {len(rows)} {kind} into "
                f"'{dest_name}'?\n\n{names}",
                ok="Download",
                cancel="Cancel",
            ):
                return
            self._install_queue = list(rows)
            self._install_total = len(rows)
            self._run_next_install()

        QTimer.singleShot(0, _perform)

    def _on_ai_install_requested(self, row: dict) -> None:
        """Install button on an AI mod/plugin bubble.

        Rows reaching here already passed :func:`ai_client.validate_picks`,
        so the project id is real and compatible with the target. The row's
        ``project_type`` decides the destination: plugins -> the selected
        server's plugins/, mods -> the selected instance's mods/, or the
        selected mod-capable server's mods/ when no instance is selected.
        """
        if str(row.get("project_type") or "") == PROJECT_TYPE_PLUGIN:
            server = self._server()
            if server is None:
                self._toast("warning", "No server",
                            "Select a server on the Server tab first.")
                return
            if not server.supports_plugins or not server.mc_version:
                self._toast(
                    "warning", "Server cannot run plugins",
                    f"'{server.name}' cannot run plugins ("
                    f"{server.server_type}).", 6000)
                return
        else:
            inst = self._instance()
            kind, target = self.ai_view._mod_target(inst)
            if kind is None:
                hint = "Select an instance first."
                try:
                    srv = self._server()
                except Exception:
                    srv = None
                if srv is not None and getattr(srv, "supports_mods", False):
                    hint = (f"No mod target. '{srv.name}' cannot run mods "
                            f"({srv.server_type}) or has no detected version -- "
                            "re-import its jar, or pick an instance.")
                elif srv is not None and getattr(
                        srv, "supports_plugins", False):
                    hint += (f" Or switch the AI dropdown to 'Plugins only' to "
                             f"install into '{srv.name}'.")
                self._toast("warning", "No instance", hint, 6000)
                return
            if kind == "server":
                # Leave inst as-is (None/vanilla): _install_target resolves
                # the server fallback from the AI view itself.
                pass
            elif inst is not None and inst.loader_id == "vanilla":
                self._toast("warning", "Vanilla instance",
                            "Mods need a Fabric or NeoForge instance.")
                return
        self._install_queue = [dict(row)]
        self._install_total = 1
        self._run_next_install()

    def _install_target(self, row: dict) -> tuple[str, str, str] | None:
        """``(dest_dir, game_version, loader)`` for one result row.

        Plugin rows install into the selected server's ``plugins/`` folder;
        mod rows install into whatever the "Install to:" pick resolves to --
        the selected instance, or a mod-capable server chosen like an
        instance. None means the required target is missing/unsupported.
        """
        if str(row.get("project_type") or "") == PROJECT_TYPE_PLUGIN:
            from ..servers import plugin_loader_for

            server = self._server()
            if server is None or not server.supports_plugins:
                return None
            if not server.mc_version:
                return None
            return (server.plugins_dir, server.mc_version,
                    plugin_loader_for(server.server_type) or "spigot")
        # Mod row: the "Install to:" target (instance or mod-capable server).
        kind, obj = self._browser_target()
        if obj is None:
            return None
        return self._browser_target_dims(kind, obj)

    def _run_next_install(self) -> None:
        if not self._install_queue:
            return
        result = self._install_queue.pop(0)
        target = self._install_target(result)
        if target is None:
            self._toast(
                "warning", "No install target",
                f"Cannot install '{result.get('title', '?')}': select a "
                "compatible target first.", 5000)
            if self._install_queue:
                self._run_next_install()
                return
            self.set_busy(False)
            return
        target_dir, game_version, loader = target
        done = self._install_total - len(self._install_queue) - 1
        # Remember which project is in flight so its AI bubble can flip to
        # "Installed" when the download finishes.
        self._installing_project_id = str(result.get("project_id") or "")
        self.status_label.setText(
            f"Installing {result.get('title', 'mod')} "
            f"({max(1, done + 1)}/{self._install_total})..."
        )
        self.set_busy(True)
        from ..threading_utils import retire
        retire(self._install_thread)
        self._install_thread = ModInstallThread(
            source=str(result.get("source", SOURCE_MODRINTH)),
            project_id=str(result["project_id"]),
            game_version=game_version,
            loader=loader,
            mods_dir=target_dir,
            curseforge_api_key=self._get_cf_key(),
            parent=self,
        )
        self._install_thread.status.connect(self.status_label.setText)
        self._install_thread.succeeded.connect(self._on_install_ok)
        self._install_thread.failed.connect(self._on_install_failed)
        self._install_thread.start()

    def _on_install_ok(self, installed: list) -> None:
        names = ", ".join(installed) or "mod"
        self._log(f"Installed mods: {names}")
        if getattr(self, "_installing_project_id", ""):
            self.ai_view.mark_mod_installed(self._installing_project_id)
            self._installing_project_id = ""
        if self._install_queue:
            self._run_next_install()
            return
        self.set_busy(False)
        self.status_label.setText(f"Installed: {names}")
        self._toast("success", "Mods installed", names[:200], 5000)
        # Batch finished: drop every tick so a second confirm can't
        # re-install the same mods accidentally.
        self._checked_mods.clear()
        self._checked_titles.clear()
        self.refresh_installed()
        self._populate_list()   # refresh [installed] tags

    def _on_install_failed(self, message: str) -> None:
        self._log(message, "ERROR")
        self._toast("error", "Install failed", message[:200], 8000)
        if self._install_queue:
            self._run_next_install()   # continue through the batch
            return
        self.set_busy(False)
        self.status_label.setText("Installation failed.")
        self.refresh_installed()

    # -- cancel / help ------------------------------------------------------------------------------
    def _on_cancel(self) -> None:
        self._install_queue.clear()
        self._checked_mods.clear()
        self._checked_titles.clear()
        for card in self._cards.values():
            if not sip.isdeleted(card):
                card.checkbox.setChecked(False)
        self.status_label.setText("Selection cleared.")
        self._toast("info", "Cancelled", "Selection cleared.", 3000)

    def _on_help(self) -> None:
        from PyQt6.QtCore import QTimer

        from ..cove_dialog import CoveConfirm

        def _show() -> None:
            CoveConfirm.info(
                self.window(),
                "Mod downloader help",
                "1. Pick Modrinth or CurseForge on the left rail.\n"
                "2. Type a search term and press Enter.\n"
                "3. Tick the checkbox of each mod you want (or click a mod and "
                "press 'Select mod for download').\n"
                "4. Click a mod to read its description on the right.\n"
                "5. Press 'Review and confirm' to download everything ticked.",
                button="OK",
            )

        QTimer.singleShot(0, _show)

    def _log(self, message: str, level: str = "INFO") -> None:
        self._log_cb(message, level)