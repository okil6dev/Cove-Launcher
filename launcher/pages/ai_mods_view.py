"""
"AI Installer" tab for the Mods page.

Chat with an AI about the mods you want; it searches Modrinth and CurseForge
for you and offers the matches as installable cards.

Layout
------
    top     [model combo] [gear]                 <- endpoint / key settings
    middle  transcript: speech bubbles + mod bubbles
    bottom  [ message box ................ ] [Send]

Gating
------
Nothing here works without a selected instance -- the prompt box, the Send
button and every Install button are disabled, because a mod cannot be
installed anywhere else. :meth:`set_instance` re-evaluates all of it.
"""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox,
    EditableComboBox,
    PushButton,
    SubtitleLabel,
    ToolButton,
    FluentIcon,
)

from .. import ai_client
from ..constants import PROJECT_TYPE_MOD, PROJECT_TYPE_PLUGIN
from ..workers_net import AiModsThread
from .mods_page import IconFetcher


def _build_qss() -> str:
    """Page stylesheet, driven by the active theme.

    The launcher can be light, dark, mica or acrylic, so every colour comes
    from :mod:`launcher.theme` rather than being hardcoded. Hardcoding dark
    values washed the whole page out in light mode.
    """
    from ..theme import fg, fg_faint, fg_muted, hairline

    text = fg()
    muted = fg_muted()
    faint = fg_faint()
    line = hairline()
    return f"""
QWidget#aiPage, QWidget#aiTopBar {{ background: transparent; border: none; }}
QScrollArea#aiScroll {{ background: transparent; border: none; }}

QLabel#aiBubble {{
    background: {line};
    border: 1px solid {line};
    border-radius: 10px;
    padding: 8px 12px;
    color: {text};
}}
QLabel#aiBubbleMe {{
    background: rgba(63, 167, 167, 0.22);
    border: 1px solid rgba(63, 167, 167, 0.45);
    border-radius: 10px;
    padding: 8px 12px;
    color: {text};
}}
QWidget#aiModBubble {{
    background: {line};
    border: 1px solid {line};
    border-radius: 12px;
}}
QLabel#aiModName {{ color: {text}; font-weight: 600; }}
QLabel#aiModMeta {{ color: {muted}; font-size: 11px; }}
QLabel#aiHint   {{ color: {faint}; font-size: 12px; }}
QLabel#aiIcon   {{ color: {muted}; font-size: 20px; }}

QLineEdit#aiInput {{
    background: transparent;
    border: 1px solid {line};
    border-radius: 6px;
    padding: 7px 10px;
    color: {text};
    selection-background-color: #3fa7a7;
}}
QLineEdit#aiInput:focus {{ border: 1px solid #3fa7a7; }}
"""


MOD_CARD_WIDTH = 190
_MOD_CARD_SPACING = 10


class ModBubble(QWidget):
    """One suggested mod: icon, name, why, and an Install button."""

    installRequested = pyqtSignal(dict)

    def __init__(self, row: dict, parent=None) -> None:
        super().__init__(parent)
        self.row = row
        self.setObjectName("aiModBubble")
        self.setFixedWidth(MOD_CARD_WIDTH)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self.icon = QLabel("?")
        self.icon.setObjectName("aiIcon")
        self.icon.setFixedSize(56, 56)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignCenter)

        self.name = QLabel(str(row.get("title") or "?"))
        self.name.setObjectName("aiModName")
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setWordWrap(True)
        lay.addWidget(self.name)

        source = ("Modrinth" if row.get("source") == "modrinth"
                  else "CurseForge")
        self.meta = QLabel(source)
        self.meta.setObjectName("aiModMeta")
        self.meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.meta)

        reason = str(row.get("ai_reason") or "").strip()
        if reason:
            lbl = QLabel(reason)
            lbl.setObjectName("aiModMeta")
            lbl.setWordWrap(True)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(lbl)

        self.button = PushButton(FluentIcon.DOWNLOAD, "Install")
        self.button.clicked.connect(self._on_install)
        lay.addWidget(self.button)

        self._installed = False

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.icon.setText("")
        self.icon.setPixmap(pixmap.scaled(
            56, 56,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def set_installable(self, enabled: bool) -> None:
        """Grey out Install when there is no instance to install into."""
        self.button.setEnabled(enabled and not self._installed)

    def mark_installed(self) -> None:
        self._installed = True
        self.button.setText("Installed")
        self.button.setEnabled(False)

    def _on_install(self) -> None:
        if not self._installed:
            self.installRequested.emit(self.row)


class AiModsView(QWidget):
    """The AI Installer tab: chat transcript + prompt box."""

    installRequested = pyqtSignal(dict)     # a validated mod row
    instanceNeeded = pyqtSignal()           # no instance selected

    def __init__(self, get_instance, log, toast, get_cf_key,
                 get_vault, get_server=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("aiPage")
        self.setStyleSheet(_build_qss())

        self._get_instance = get_instance
        self._get_server = get_server or (lambda: None)
        self._log = log
        self._toast = toast
        self._get_cf_key = get_cf_key
        self._get_vault = get_vault

        self._history: list[dict] = []
        self._thread: AiModsThread | None = None
        self._bubbles: list[ModBubble] = []
        self._instance = None
        self._server = None
        self._config = ai_client.load_config(self._get_vault())
        # ModBubble.set_pixmap() existed but nothing ever called it, so every AI
        # suggestion showed a "?" forever. Reuse the browse page's fetcher.
        self._icon_fetcher = IconFetcher(self)
        self._icon_fetcher.loaded.connect(self._on_icon_loaded)
        self._icon_cache: dict[str, QPixmap] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        root.addWidget(self._build_top_bar())

        # -- transcript --------------------------------------------------------
        self.scroll = QScrollArea()
        self.scroll.setObjectName("aiScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area = QWidget()
        self.scroll_area.setObjectName("aiPage")
        self.transcript = QVBoxLayout(self.scroll_area)
        self.transcript.setContentsMargins(4, 4, 4, 4)
        self.transcript.setSpacing(8)
        # Leading stretch, so a short conversation sits against the prompt box
        # (chat style) instead of floating at the top of a tall empty area.
        # Bubbles are inserted at index 0, before this stretch.
        self.transcript.addStretch(1)
        self.scroll.setWidget(self.scroll_area)
        root.addWidget(self.scroll, stretch=1)

        self.status = QLabel("")
        self.status.setObjectName("aiHint")
        root.addWidget(self.status)

        # -- prompt ------------------------------------------------------------
        row = QHBoxLayout()
        row.setSpacing(8)
        self.input = QLineEdit()
        self.input.setObjectName("aiInput")
        self.input.setPlaceholderText(
            "Select an instance to use the AI installer")
        self.send_button = PushButton(FluentIcon.SEND, "Send")
        self.send_button.setEnabled(False)
        row.addWidget(self.input, stretch=1)
        row.addWidget(self.send_button)
        root.addLayout(row)

        self.input.returnPressed.connect(self._on_send)
        self.send_button.clicked.connect(self._on_send)
        self.set_busy(False)
        # Start from the real selection, not a hardcoded None, so the gating
        # is correct even before on_instance_changed() runs.
        self.set_instance(self._get_instance())
        self.set_server(self._get_server())

    # -- construction ---------------------------------------------------------
    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("aiTopBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # "+" adds an endpoint. This is the only way to get started, which is
        # why the model box stays disabled until one exists.
        self.add_button = ToolButton(FluentIcon.ADD)
        self.add_button.setToolTip("Add an AI endpoint")
        self.add_button.clicked.connect(self._open_add_endpoint)

        self.endpoint_combo = ComboBox()
        self.endpoint_combo.setMinimumWidth(120)
        # Cap it: qfluentwidgets' ComboBox is a QPushButton that sizes to its
        # longest item, so a raw endpoint URL would stretch this (and its drop
        # menu) across the window and starve the model box beside it.
        self.endpoint_combo.setMaximumWidth(220)
        self.endpoint_combo.setToolTip("Which AI endpoint to use")
        self.endpoint_combo.currentIndexChanged.connect(
            self._on_endpoint_changed)

        self.model_combo = EditableComboBox()
        self.model_combo.setMinimumWidth(220)
        self.model_combo.setMaximumWidth(560)
        # Never let a server with 300 models grow an endless dropdown; scrolls
        # past ~12 visible rows instead.
        self.model_combo.setMaxVisibleItems(12)
        self._reload_models()
        self._reload_endpoints()
        # Both signals matter: picking from the dropdown emits
        # currentTextChanged, while *typing* a model name (or programmatic
        # setText) only emits textChanged. Listening to just one means a typed
        # model is never saved.
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        self.model_combo.textChanged.connect(self._on_model_changed)
        # Commit on Enter / leaving the box, not on every keystroke.
        self.model_combo.editingFinished.connect(self._commit_model)

        self.gear_button = ToolButton(FluentIcon.SETTING)
        self.gear_button.setToolTip("AI settings")
        self.gear_button.clicked.connect(self._open_settings)

        # Strict project-type filter. This is not cosmetic: it decides which
        # catalogue the launcher fetches (API facets) and what the prompt is
        # allowed to recommend, so a plugin can never be suggested when the
        # user asked for mods, and vice versa.
        self.type_combo = ComboBox()
        self.type_combo.addItem("Mods only", userData=PROJECT_TYPE_MOD)
        self.type_combo.addItem("Plugins only", userData=PROJECT_TYPE_PLUGIN)
        self.type_combo.setToolTip(
            "Strictly force what the AI looks for: mods or plugins.\n"
            "Plugins install into the selected server's plugins folder.")
        self.type_combo.setMinimumWidth(110)
        self.type_combo.setMaximumWidth(130)
        wanted = (PROJECT_TYPE_PLUGIN
                  if getattr(self._config, "project_type", "")
                  == PROJECT_TYPE_PLUGIN else PROJECT_TYPE_MOD)
        self.type_combo.setCurrentIndex(
            1 if wanted == PROJECT_TYPE_PLUGIN else 0)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)

        self.cancel_button = ToolButton(FluentIcon.SYNC)
        self.cancel_button.setToolTip("Cancel this request")
        self.cancel_button.clicked.connect(self._on_cancel)
        self.cancel_button.setVisible(False)

        lay.addWidget(self.add_button)
        lay.addWidget(self.endpoint_combo)
        lay.addWidget(SubtitleLabel("Model"))
        lay.addWidget(self.model_combo, stretch=1)
        lay.addWidget(self.type_combo)
        lay.addWidget(self.cancel_button)
        lay.addWidget(self.gear_button)
        return bar

    def _active_endpoint(self):
        """The saved Endpoint the current url/key point at, if any."""
        for ep in self._config.endpoints:
            if ep.url == self._config.endpoint:
                return ep
        return None

    @staticmethod
    def _short(text: str, limit: int = 30) -> str:
        """A compact label for a long endpoint URL.

        ComboBox paints its text at full length, so a raw URL would force the
        widget (and therefore its drop menu) to be hundreds of pixels wide.
        """
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit - 1] + "…"

    def _reload_endpoints(self) -> None:
        self.endpoint_combo.blockSignals(True)
        self.endpoint_combo.clear()
        for ep in self._config.endpoints:
            self.endpoint_combo.addItem(self._short(ep.name))
        if not self._config.endpoints:
            self.endpoint_combo.addItem("No endpoint yet")
        active = 0
        for i, ep in enumerate(self._config.endpoints):
            if ep.url == self._config.endpoint:
                active = i
                break
        self.endpoint_combo.setCurrentIndex(active)
        self.endpoint_combo.setEnabled(bool(self._config.endpoints))
        self.endpoint_combo.setToolTip(self._config.endpoint or
                                       "No endpoint yet")
        self.endpoint_combo.blockSignals(False)

    def _reload_models(self) -> None:
        """Rebuild the model list from the active endpoint, or go empty."""
        ep = self._active_endpoint()
        current = self._config.model
        models = list(ep.models) if ep else []
        # A typed model stays usable even when the server did not report it:
        # some hosts hide models from /models but still serve them.
        if current and current not in models:
            models.insert(0, current)
        for m in self._config.recent_models:
            if m and m not in models:
                models.append(m)
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for m in models:
            if m:
                self.model_combo.addItem(m)
        # setCurrentText() silently does nothing on an EditableComboBox when the
        # text is not already one of its items; setText() always works.
        self.model_combo.setText(current)
        self.model_combo.blockSignals(False)
        self.model_combo.setEnabled(bool(self._config.has_endpoint))

    def _on_endpoint_changed(self, index: int) -> None:
        """Switching endpoint swaps the key and repopulates the model list."""
        if not (0 <= index < len(self._config.endpoints)):
            return
        ep = self._config.endpoints[index]
        if (ep.url == self._config.endpoint
                and ep.api_key == self._config.api_key):
            return
        self._config.endpoint = ep.url
        self._config.api_key = ep.api_key
        if ep.models and (not self._config.model
                          or self._config.model not in ep.models):
            self._config.model = ep.models[0]
        self._persist(reload_models=True)
        self._refresh_gates()

    # -- gating ---------------------------------------------------------------
    def refresh_theme(self) -> None:
        """Re-apply palette colours after a live theme switch."""
        self.setStyleSheet(_build_qss())

    def set_instance(self, inst) -> None:
        """Re-apply the "needs a target" rules.
        Called by the Mods page whenever the selection changes: the prompt box,
        Send and every Install button all depend on having somewhere to install
        into (an instance for mods, a server for plugins).
        """
        self._instance = inst
        self._refresh_gates()

    def set_server(self, server) -> None:
        """Same as set_instance(), for the server used by Plugins mode."""
        self._server = server
        self._refresh_gates()

    def project_type(self) -> str:
        """The strict mode currently forced by the dropdown."""
        data = self.type_combo.currentData()
        return (PROJECT_TYPE_PLUGIN if data == PROJECT_TYPE_PLUGIN
                else PROJECT_TYPE_MOD)

    def _on_type_changed(self, _index: int) -> None:
        """Persist the strict mode and re-gate: plugins need a server."""
        self._config.project_type = self.project_type()
        try:
            self._persist()
        except Exception:  # noqa: BLE001 - a failed save must not break the UI
            pass
        self._refresh_gates()

    def _plugin_target_ok(self) -> bool:
        """A selected server that can actually receive plugins."""
        server = self._server
        return (server is not None
                and bool(getattr(server, "supports_plugins", False))
                and bool(getattr(server, "mc_version", "")))

    def _mod_server_ok(self):
        """The selected server, when it can actually receive *mods*.

        Dual-loader (and plain modded) servers have a real ``mods/`` folder,
        so Mods mode does not force the user back to an instance when one
        is selected. Returns the server or None.
        """
        server = self._server
        if (server is not None
                and bool(getattr(server, "supports_mods", False))
                and bool(getattr(server, "mc_version", ""))):
            return server
        return None

    def _mod_target(self, inst):
        """``(kind, target)`` for Mods mode: instance wins, server fallback.

        Returns ``("instance", inst)``, ``("server", server)`` or
        ``(None, None)``. An instance is preferred when one is selected
        (and not vanilla); otherwise a mod-capable server is used.
        """
        if (inst is not None
                and getattr(inst, "loader_id", "") != "vanilla"):
            return "instance", inst
        server = self._mod_server_ok()
        if server is not None:
            return "server", server
        return None, None

    def _can_chat(self, inst) -> bool:
        """A usable target *and* an endpoint to talk to."""
        if not self._config.has_endpoint:
            return False
        if self.project_type() == PROJECT_TYPE_PLUGIN:
            return self._plugin_target_ok()
        kind, _target = self._mod_target(inst)
        return kind is not None

    def _can_install(self) -> bool:
        """Installing needs somewhere to install into -- but not an endpoint."""
        if self.project_type() == PROJECT_TYPE_PLUGIN:
            return self._plugin_target_ok()
        kind, _target = self._mod_target(self._instance)
        return kind is not None

    def _refresh_gates(self, busy=None) -> None:
        """One place that decides what the user can do right now.

        ``busy`` defaults to the live request state, but set_busy() passes its
        own value explicitly -- otherwise the flag would be recomputed as False
        and immediately undone.

        Uses the stored selection rather than re-calling get_instance(), so
        set_instance(None) really does apply None rather than whatever the page
        currently reports.
        """
        inst = self._instance
        if busy is None:
            busy = self._thread is not None and self._thread.isRunning()
        usable = self._can_chat(inst) and not busy

        plugin_mode = self.project_type() == PROJECT_TYPE_PLUGIN
        if not self._config.has_endpoint:
            self.input.setPlaceholderText("Add an AI endpoint to get started")
        elif plugin_mode:
            server = self._server
            if server is None:
                self.input.setPlaceholderText(
                    "Select a server on the Server tab to look for plugins")
            elif not getattr(server, "supports_plugins", False):
                self.input.setPlaceholderText(
                    f"'{server.name}' cannot run plugins")
            elif not getattr(server, "mc_version", ""):
                self.input.setPlaceholderText(
                    "Re-import the server jar to detect its version")
            else:
                self.input.setPlaceholderText(
                    f"Ask for plugins for '{server.name}'...")
        elif inst is None:
            server = self._mod_server_ok()
            if server is not None:
                self.input.setPlaceholderText(
                    f"Ask for mods for '{server.name}'...")
            else:
                self.input.setPlaceholderText(
                    "Select an instance to use the AI installer")
        elif getattr(inst, "loader_id", "") == "vanilla":
            server = self._mod_server_ok()
            if server is not None:
                self.input.setPlaceholderText(
                    f"Ask for mods for '{server.name}'...")
            else:
                self.input.setPlaceholderText(
                    "Mods need a Fabric or NeoForge instance")
        else:
            self.input.setPlaceholderText(f"Ask for mods for '{inst.name}'...")

        self.input.setEnabled(usable)
        self.send_button.setEnabled(usable)
        # Installing needs a target, but not an endpoint -- the mod cards
        # stay live even with no AI configured.
        installable = self._can_install()
        for bubble in self._bubbles:
            bubble.set_installable(installable)

    def set_busy(self, busy: bool) -> None:
        self.cancel_button.setVisible(busy)
        self._refresh_gates(busy=busy)

    # -- chat -----------------------------------------------------------------
    def _add_bubble(self, text: str, mine: bool) -> None:
        if not text:
            return
        lbl = QLabel(text)
        lbl.setObjectName("aiBubbleMe" if mine else "aiBubble")
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        # Qt will happily shrink a wrapping label to a few characters per line,
        # so the bubble width is computed from the text itself: as wide as it
        # needs, capped at 520.
        plain = lbl.text().replace("<br>", " ")
        fm = lbl.fontMetrics()
        width = fm.boundingRect(0, 0, 520, 0,
                                int(Qt.TextFlag.TextWordWrap), plain).width()
        lbl.setFixedWidth(min(520, max(120, width + 8)))
        lbl.setSizePolicy(QSizePolicy.Policy.Fixed,
                          QSizePolicy.Policy.Minimum)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(0 if mine else 1)
        row.addWidget(lbl, stretch=0)
        row.addStretch(1 if mine else 0)
        # Append after existing content but before the trailing stretch.
        self.transcript.insertLayout(self.transcript.count() - 1, row)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _add_mod_bubbles(self, rows: list[dict]) -> None:
        installable = self._can_install()
        strip = QWidget()
        # A grid, not a single row: cards were laid out in one row of fixed
        # 190px cards, so past the third or fourth they ran off the right edge
        # and were unreachable -- the transcript has no horizontal scrollbar.
        grid = QGridLayout(strip)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        columns = self._card_columns()
        for index, row in enumerate(rows):
            bubble = ModBubble(row)
            bubble.installRequested.connect(self.installRequested.emit)
            bubble.set_installable(installable)
            self._bubbles.append(bubble)
            grid.addWidget(bubble, index // columns, index % columns)
            self._load_bubble_icon(bubble, row)
        # Center the block of cards.
        grid.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        strip.setSizePolicy(QSizePolicy.Policy.Preferred,
                            QSizePolicy.Policy.Minimum)
        # The strip is created and filled after the page has been laid out, so
        # Qt does not lay it out on its own; without this every card sits at
        # (0,0) stacked on top of the others.
        grid.activate()
        self.transcript.insertWidget(self.transcript.count() - 1, strip)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _installed_files(self) -> list[str]:
        """Real jar filenames already in the current target's folder.

        Mods mode reads the instance's mods folder, or the selected
        mod-capable server's mods folder when no instance applies; Plugins
        mode reads the server's plugins folder. The AI is told these so it
        stops claiming things are already installed when they are not.
        """
        if self.project_type() == PROJECT_TYPE_PLUGIN:
            server = self._server
            folder = (getattr(server, "plugins_dir", "")
                      if server is not None else "")
        else:
            _kind, target = self._mod_target(self._instance)
            folder = (getattr(target, "mods_dir", "") or ""
                      if target is not None else "")
        if not folder:
            return []
        try:
            from .. import moddb

            return list(moddb.list_installed_mods(folder))
        except Exception as exc:  # noqa: BLE001 - never block a prompt
            self._log(f"Could not read installed files: {exc}", "WARNING")
            return []

    def _load_bubble_icon(self, bubble: ModBubble, row: dict) -> None:
        """Show the real thumbnail, from cache when we already have it."""
        url = str(row.get("icon_url") or "").strip()
        if not url:
            return
        cached = self._icon_cache.get(url)
        if cached is not None:
            bubble.set_pixmap(cached)
        else:
            self._icon_fetcher.fetch(url)

    def _on_icon_loaded(self, url: str, data: bytes) -> None:
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        self._icon_cache[url] = pix
        for bubble in list(self._bubbles):
            if str(bubble.row.get("icon_url") or "") != url:
                continue
            try:
                bubble.set_pixmap(pix)
            except RuntimeError:
                pass    # bubble was deleted while the download was in flight

    def _card_columns(self) -> int:
        """How many mod cards fit across the panel right now.

        Measured from the scroll viewport so it tracks the real available
        width; falls back to 3 when the view has not been laid out yet.
        """
        width = self.scroll.viewport().width()
        if width <= 0:
            width = self.width()
        if width <= 0:
            return 3
        per_card = MOD_CARD_WIDTH + _MOD_CARD_SPACING
        return max(1, min(6, (width + _MOD_CARD_SPACING) // per_card))

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # -- sending --------------------------------------------------------------
    def _on_send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        plugin_mode = self.project_type() == PROJECT_TYPE_PLUGIN
        if plugin_mode:
            server = self._get_server()
            if server is None or not getattr(server, "supports_plugins", False):
                self.instanceNeeded.emit()
                self._toast("warning", "No plugin target",
                            "Select a server on the Server tab first.")
                return
            if not getattr(server, "mc_version", ""):
                self._toast("warning", "Unknown server version",
                            "Re-import the server jar so its MC version can "
                            "be detected.")
                return
            inst = None
        else:
            server = None
            kind, target = self._mod_target(self._get_instance())
            if kind is None:
                self.instanceNeeded.emit()
                selected = self._get_server()
                picked = None
                try:
                    picked = self._get_instance()
                except Exception:
                    picked = None
                if (picked is not None
                        and getattr(picked, "loader_id", "") == "vanilla"):
                    self._toast("warning", "Vanilla instance",
                                "Mods need a Fabric or NeoForge instance.")
                    return
                if (selected is not None
                        and not getattr(selected, "supports_mods", False)):
                    self._toast(
                        "warning", "Server cannot run mods",
                        f"'{selected.name}' cannot run mods "
                        f"({getattr(selected, 'server_type', '?')}). "
                        "Use a dual-loader or modded server, or pick an "
                        "instance.", 6000)
                    return
                if selected is not None and not getattr(
                        selected, "mc_version", ""):
                    self._toast("warning", "Unknown server version",
                                "Re-import the server jar so its MC version "
                                "can be detected.")
                    return
                hint = "Select an instance first."
                if selected is not None and getattr(
                        selected, "supports_plugins", False):
                    hint += (f" Or switch the dropdown to 'Plugins only' to use "
                             f"'{selected.name}'.")
                self._toast("warning", "No instance", hint, 6000)
                return
            if kind == "server":
                inst = None
                server = target
            else:
                inst = target
            if (inst is not None
                    and getattr(inst, "loader_id", "") == "vanilla"):
                self._toast("warning", "Vanilla instance",
                            "Mods need a Fabric or NeoForge instance.")
                return
        if not self._config.has_endpoint:
            self._toast("warning", "No endpoint",
                        "Add an AI endpoint first (the + button).")
            return
        if self._thread is not None and self._thread.isRunning():
            return
        from ..threading_utils import retire
        retire(self._thread)

        self._add_bubble(html.escape(text), mine=True)
        self._history.append({"role": "user", "content": text})
        self.input.clear()
        self.status.setText("Thinking...")
        self.set_busy(True)

        if plugin_mode:
            from ..servers import plugin_loader_for

            kwargs = dict(
                game_version=server.mc_version,
                loader=plugin_loader_for(server.server_type) or "spigot",
                instance_label=(
                    f"server {server.name} "
                    f"({server.server_type}, {server.mc_version})"),
                curseforge_api_key="",   # plugin mode is Modrinth-only
            )
        else:
            if server is not None:
                # Mods for a dual-loader/modded server: search the mod
                # catalogue with the server's version + mod loader, and label
                # the prompt so the model knows it is a server target.
                from ..servers import mod_loader_for

                kwargs = dict(
                    game_version=server.mc_version,
                    loader=mod_loader_for(server.server_type) or "forge",
                    instance_label=(
                        f"server {server.name} "
                        f"({server.server_type}, {server.mc_version})"),
                    curseforge_api_key=self._get_cf_key(),
                )
            else:
                kwargs = dict(
                    game_version=inst.mc_version,
                    loader=inst.loader_id,
                    instance_label=(
                        f"{inst.name} ({inst.mc_version}, {inst.loader_id})"),
                    curseforge_api_key=self._get_cf_key(),
                )

        self._thread = AiModsThread(
            config=self._config,
            user_text=text,
            history=self._history[:-1],   # the prompt is appended by the thread
            installed=self._installed_files(),
            project_type=self.project_type(),
            parent=self,
            **kwargs,
        )
        self._thread.status.connect(self.status.setText)
        self._thread.ready.connect(self._on_reply)
        self._thread.failed.connect(self._on_failed)
        self._thread.start()

    def _on_cancel(self) -> None:
        from ..threading_utils import retire
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.cancel()
            self.status.setText("Cancelling...")
            return
        # A finished-but-unreaped handle still pins the slot -- drop it so
        # the next prompt is not refused by the isRunning() gate above.
        retire(thread)
        self._thread = None
        self.set_busy(False)
        self.status.setText("")

    def _on_reply(self, reply: str, rows: list) -> None:
        from ..threading_utils import retire
        retire(self._thread)
        if reply:
            self._history.append({"role": "assistant", "content": reply})
            self._add_bubble(html.escape(reply).replace("\n", "<br>"),
                             mine=False)
        if rows:
            self._add_mod_bubbles(rows)
            noun = ("plugin(s)"
                    if self.project_type() == PROJECT_TYPE_PLUGIN
                    else "mod(s)")
            self.status.setText(f"Found {len(rows)} {noun}.")
        else:
            self.status.setText("")
        self._thread = None
        self.set_busy(False)

    def _on_failed(self, message: str) -> None:
        from ..threading_utils import retire
        retire(self._thread)
        self._log(f"AI: {message}", "ERROR")
        self._add_bubble(html.escape(message), mine=False)
        self.status.setText("")
        self._thread = None
        self.set_busy(False)
        self._toast("error", "AI request failed", message[:200], 8000)

    def mark_mod_installed(self, project_id: str) -> None:
        """Grey out the bubble for a mod that just finished installing."""
        for bubble in self._bubbles:
            if str(bubble.row.get("project_id")) == str(project_id):
                bubble.mark_installed()

    def clear_chat(self) -> None:
        self._history.clear()
        self._bubbles.clear()
        while self.transcript.count() > 1:
            item = self.transcript.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                del item
        self.status.setText("")

    # -- endpoints ------------------------------------------------------------
    def _open_add_endpoint(self) -> None:
        # Defer so the click/mouse-release cycle completes before the modal
        # opens (same pattern as the rest of the app).
        QTimer.singleShot(0, self._show_add_endpoint)

    def _show_add_endpoint(self) -> None:
        from .ai_add_endpoint import AddEndpointDialog

        dlg = AddEndpointDialog(self.window())
        if dlg.exec():
            ep = dlg.result_endpoint()
            # Replace an entry for the same URL instead of duplicating it, so
            # repeatedly re-adding localhost:11434 does not pile up rows.
            for i, existing in enumerate(self._config.endpoints):
                if existing.url == ep.url:
                    self._config.endpoints[i] = ep
                    break
            else:
                self._config.endpoints.insert(0, ep)
            self._config.endpoint = ep.url
            self._config.api_key = ep.api_key
            if ep.models:
                self._config.model = ep.models[0]
            self._persist()
            self._reload_endpoints()
            self._reload_models()
            self._refresh_gates()
            self.status.setText(
                f"Using {ep.name} with model '{self._config.model}'.")

    def _on_model_changed(self, text: str) -> None:
        """Track the model as it is typed, without rebuilding the dropdown.

        Persisting here would be wrong: saving on every keystroke records each
        half-typed string as a "recent model", and the reload it triggers wipes
        the list the user is mid-way through choosing from. Commit happens on
        editingFinished instead (Enter or leaving the box).
        """
        model = (text or "").strip()
        if model:
            self._config.model = model

    def _commit_model(self) -> None:
        """Save the model once the user stops editing it.

        Note that ``EditableComboBox._onReturnPressed`` *adds* whatever is typed
        as a new item in the list. That is fine -- a hand-typed model should be
        selectable afterwards -- but it means the item list must be rebuilt
        afterwards, otherwise the reload in :meth:`_persist` wipes it again.
        """
        model = self.model_combo.text().strip()
        if model and model != self._config.model:
            self._config.model = model
        elif not model:
            # Box emptied: fall back to the first known model rather than
            # leaving a blank model that can never work.
            ep = self._active_endpoint()
            if ep and ep.models:
                self._config.model = ep.models[0]
        self._persist(reload_models=True)

    def _persist(self, reload_models: bool = False) -> None:
        try:
            ai_client.save_config(self._get_vault(), self._config)
            if reload_models:
                self._reload_models()
        except ai_client.AiError as exc:
            self._toast("error", "Could not save AI settings", str(exc))
        except Exception as exc:  # noqa: BLE001
            self._log(f"AI settings save failed: {exc}", "ERROR")

    def _open_settings(self) -> None:
        # Defer so the click/mouse-release cycle completes before the modal
        # opens (same pattern as the rest of the app).
        QTimer.singleShot(0, self._show_settings)

    def _show_settings(self) -> None:
        from .ai_settings_dialog import AiSettingsDialog

        vault = self._get_vault()
        cfg = ai_client.load_config(vault)
        cfg.model = self._config.model or cfg.model
        dlg = AiSettingsDialog(cfg, self.window())
        if dlg.exec():
            new_cfg = dlg.result_config()
            self._config = new_cfg
            self._persist()
            self._reload_endpoints()
            self._reload_models()
            self._refresh_gates()
            self._toast("success", "AI settings saved",
                        f"{new_cfg.endpoint} · {new_cfg.model}")


# __AIVIEW6__
