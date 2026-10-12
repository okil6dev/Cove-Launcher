"""
Java Downloads page — dedicated sidebar tab for installing JDK distributions.

Three flavors are available:
  • Eclipse Temurin  (OpenJDK HotSpot — stable)
  • GraalVM CE       (high-performance JIT — best FPS)
  • Microsoft OpenJDK (production-ready, backed by Microsoft)

Each flavor gets a rich card with a one-click install button and
inline progress feedback — no dialog needed.
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QScrollArea,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    ProgressBar,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    TitleLabel,
)

from ..java_downloader import JdkDownloadThread, JDKS_DIR

# ── flavor catalogue ──────────────────────────────────────────────────────────

_FLAVORS = [
    {
        "id": "temurin",
        "name": "Eclipse Temurin",
        "provider": "Eclipse Adoptium",
        "subtitle": "OpenJDK HotSpot — standard & reliable",
        "description": (
            "The most widely-used open-source Java runtime, maintained by the "
            "Eclipse Adoptium working group. Rock-solid HotSpot JVM with "
            "long-term support (LTS) releases. Recommended for most players."
        ),
        "badge": "Recommended",
        "badge_color": "#4CAF50",
        "icon": "COFFEE",
        "accent": "#4CAF50",
        "bg_from": "rgba(76,175,80,0.12)",
        "bg_to": "rgba(76,175,80,0.04)",
    },
    {
        "id": "graalvm",
        "name": "GraalVM Community",
        "provider": "Oracle / GraalVM team",
        "subtitle": "High-performance JIT — better FPS",
        "description": (
            "Oracle's open-source GraalVM Community Edition. The Graal JIT "
            "compiler replaces HotSpot C2 and can significantly improve "
            "Minecraft's frame-time consistency and throughput — great for "
            "modpacks with heavy compute loads."
        ),
        "badge": "Best FPS",
        "badge_color": "#FF9800",
        "icon": "LIGHTNING",
        "accent": "#FF9800",
        "bg_from": "rgba(255,152,0,0.12)",
        "bg_to": "rgba(255,152,0,0.04)",
    },
    {
        "id": "microsoft",
        "name": "Microsoft OpenJDK",
        "provider": "Microsoft",
        "subtitle": "Production-grade OpenJDK build",
        "description": (
            "Microsoft's own long-term supported build of OpenJDK, used "
            "internally by Azure and the official Minecraft Launcher. "
            "Rigorously tested, enterprise-grade stability with regular "
            "security patches."
        ),
        "badge": "Enterprise",
        "badge_color": "#0078D4",
        "icon": "GLOBE",
        "accent": "#0078D4",
        "bg_from": "rgba(0,120,212,0.12)",
        "bg_to": "rgba(0,120,212,0.04)",
    },
]

_VERSIONS = [25, 21, 17]  # newest LTS first

# Versions each vendor actually publish as Windows x64 zips (verified live
# against their release channels). A flavor card's Install button is disabled
# when the selected version isn't offered, instead of failing mid-download.
_VERSION_SUPPORT: dict[str, tuple[int, ...]] = {
    "temurin": (25, 21, 17),
    "graalvm": (25, 21, 17),
    # Via official aka.ms permanent links (see resolve_microsoft_url);
    # verified live for 25, 21 and 17.
    "microsoft": (25, 21, 17),
}

_ICON_MAP = {
    "COFFEE": "☕",
    "LIGHTNING": "⚡",
    "GLOBE": "🌐",
}


# ── single flavor card ────────────────────────────────────────────────────────

class _JavaFlavorCard(QFrame):
    """
    Self-contained card for one JVM flavor.

    Handles its own download thread and shows inline progress.
    Reports success via the ``on_installed`` callable.
    """

    def __init__(
        self,
        flavor: dict,
        on_installed,   # callable(flavor_id, java_exe)
        get_version,    # callable() -> int  (reads the shared version combo)
        toast,
        log,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._flavor = flavor
        self._on_installed = on_installed
        self._get_version = get_version
        self._toast = toast
        self._log = log
        self._worker: Optional[JdkDownloadThread] = None

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._build_ui()
        self._refresh_install_status()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        f = self._flavor
        fid = f["id"]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Card frame
        card = QFrame()
        card.setObjectName(f"javaCard_{fid}")
        self._card_frame = card
        self._apply_card_theme()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 16, 20, 16)
        card_layout.setSpacing(8)

        # ── Header row ────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(10)

        icon_text = _ICON_MAP.get(f["icon"], "☕")
        icon_lbl = QLabel(icon_text)
        icon_lbl.setFixedSize(38, 38)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_lbl = icon_lbl
        header.addWidget(icon_lbl)

        name_col = QVBoxLayout()
        name_col.setSpacing(1)
        name_lbl = QLabel(f["name"])
        nfont = QFont()
        nfont.setPointSize(12)
        nfont.setBold(True)
        name_lbl.setFont(nfont)
        name_col.addWidget(name_lbl)

        provider_lbl = QLabel(f["provider"])
        self._provider_lbl = provider_lbl
        name_col.addWidget(provider_lbl)
        header.addLayout(name_col, stretch=1)

        badge = QLabel(f["badge"])
        badge.setStyleSheet(
            f"color: {f['badge_color']};"
            "font-size: 10px; font-weight: bold;"
            f"border: 1px solid {f['badge_color']};"
            "border-radius: 4px; padding: 2px 8px;"
        )
        badge.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(badge)
        card_layout.addLayout(header)

        # ── Subtitle / description ────────────────────────────────────────────
        sub_lbl = QLabel(f["subtitle"])
        sub_lbl.setStyleSheet(
            f"color: {f['accent']}; font-size: 10px; font-weight: bold;"
        )
        card_layout.addWidget(sub_lbl)

        desc_lbl = QLabel(f["description"])
        desc_lbl.setWordWrap(True)
        self._desc_lbl = desc_lbl
        card_layout.addWidget(desc_lbl)
        self._apply_card_theme()  # theme-aware text colours for labels above

        # ── Status label ──────────────────────────────────────────────────────
        self._status_lbl = CaptionLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setVisible(False)
        card_layout.addWidget(self._status_lbl)

        # ── Progress bar ──────────────────────────────────────────────────────
        self._progress = ProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        card_layout.addWidget(self._progress)

        # ── Installed path label ──────────────────────────────────────────────
        self._installed_lbl = CaptionLabel("")
        self._installed_lbl.setStyleSheet("color: #4CAF50; font-size: 9px;")
        self._installed_lbl.setVisible(False)
        card_layout.addWidget(self._installed_lbl)

        # ── Button row ────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._open_folder_btn = PushButton(FluentIcon.FOLDER, "Open Folder")
        self._open_folder_btn.setVisible(False)
        self._open_folder_btn.clicked.connect(self._on_open_folder)
        btn_row.addWidget(self._open_folder_btn)

        btn_row.addStretch()

        self._unavailable_lbl = CaptionLabel(
            "Not published for this Java version yet"
        )
        self._unavailable_lbl.setStyleSheet("color: #888888; font-size: 9px;")
        self._unavailable_lbl.setVisible(False)
        btn_row.addWidget(self._unavailable_lbl)

        self._install_btn = PrimaryPushButton(FluentIcon.DOWNLOAD, "Install")
        self._install_btn.setFixedWidth(120)
        self._install_btn.clicked.connect(self._on_install)
        btn_row.addWidget(self._install_btn)

        self._delete_btn = PushButton(FluentIcon.DELETE, "Delete")
        self._delete_btn.setFixedWidth(100)
        self._delete_btn.setToolTip("Remove this installed JDK from disk")
        self._delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self._delete_btn)

        card_layout.addLayout(btn_row)
        outer.addWidget(card)

    # ── install helpers ───────────────────────────────────────────────────────

    def _install_dir(self) -> str:
        ver = self._get_version()
        return os.path.join(JDKS_DIR, f"{self._flavor['id']}-java{ver}")

    def _java_exe(self) -> str:
        return os.path.join(self._install_dir(), "bin", "java.exe")

    # ── theming ──────────────────────────────────────────────────────────────
    def _apply_card_theme(self) -> None:
        from ..theme import fg_muted, hairline, icon_tile_bg

        f = self._flavor
        self._card_frame.setStyleSheet(
            f"QFrame#{self._card_frame.objectName()} {{"
            f"  background: qlineargradient("
            f"    x1:0, y1:0, x2:1, y2:1,"
            f"    stop:0 {f['bg_from']}, stop:1 {f['bg_to']}"
            f"  );"
            f"  border: 1px solid {hairline()};"
            f"  border-left: 3px solid {f['accent']};"
            f"  border-radius: 12px;"
            f"}}"
        )
        icon = getattr(self, "_icon_lbl", None)
        if icon is not None:
            icon.setStyleSheet(
                "font-size: 22px;"
                f"background: {icon_tile_bg()};"
                "border-radius: 8px;"
            )
        provider = getattr(self, "_provider_lbl", None)
        if provider is not None:
            provider.setStyleSheet(
                f"color: {fg_muted()}; font-size: 10px;"
            )
        desc = getattr(self, "_desc_lbl", None)
        if desc is not None:
            desc.setStyleSheet(
                f"color: {fg_muted()}; font-size: 9px;"
            )

    def refresh_theme(self) -> None:
        self._apply_card_theme()

    def _refresh_install_status(self) -> None:
        installed = os.path.isfile(self._java_exe())
        self._delete_btn.setVisible(installed)
        if installed:
            self._install_btn.setText("Reinstall")
            self._installed_lbl.setText(f"Installed at: {self._java_exe()}")
            self._installed_lbl.setVisible(True)
            self._open_folder_btn.setVisible(True)
        else:
            self._install_btn.setText("Install")
            self._installed_lbl.setVisible(False)
            self._open_folder_btn.setVisible(False)

    # ── slots ─────────────────────────────────────────────────────────────────

    def flavor_id(self) -> str:
        """Public accessor used by the page for per-version support checks."""
        return str(self._flavor["id"])

    def set_version_available(self, available: bool) -> None:
        """
        Enable/disable the Install button according to whether the selected
        Java version is actually published by this vendor.
        """
        self._version_available = bool(available)
        busy = bool(self._worker and self._worker.isRunning())
        self._install_btn.setEnabled(self._version_available and not busy)
        self._unavailable_lbl.setVisible(not self._version_available)
        self._install_btn.setToolTip("" if self._version_available else (
            f"{self._flavor['name']} does not offer a Windows x64 build "
            f"for this Java version yet."
        ))

    def _on_install(self) -> None:
        if not getattr(self, "_version_available", True):
            return
        if self._worker and self._worker.isRunning():
            return
        ver = self._get_version()
        fid = self._flavor["id"]
        fname = self._flavor["name"]

        self._install_btn.setEnabled(False)
        self._delete_btn.setEnabled(False)
        self._status_lbl.setText(f"Starting download of {fname} Java {ver}…")
        self._status_lbl.setVisible(True)
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._installed_lbl.setVisible(False)
        self._open_folder_btn.setVisible(False)

        from ..threading_utils import retire
        retire(self._worker)
        self._worker = JdkDownloadThread(fid, ver, parent=self)
        self._worker.status.connect(self._on_status)
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)
        self._log(f"[Java/{self._flavor['id']}] {msg}")

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._progress.setValue(int(done * 100 / total))
        else:
            self._progress.setValue(0)

    def _on_succeeded(self, java_exe: str) -> None:
        ver = self._get_version()
        fname = self._flavor["name"]
        self._progress.setValue(100)
        self._status_lbl.setText(f"✅ {fname} Java {ver} installed!")
        self._install_btn.setText("Reinstall")
        self._install_btn.setEnabled(getattr(self, "_version_available", True))
        self._delete_btn.setEnabled(True)
        self._delete_btn.setVisible(True)
        self._installed_lbl.setText(f"Installed at: {java_exe}")
        self._installed_lbl.setVisible(True)
        self._open_folder_btn.setVisible(True)
        self._toast("success", f"{fname} installed",
                    f"Java {ver} ready at:\n{java_exe}", 6000)
        from ..threading_utils import retire
        retire(self._worker)
        self._worker = None
        self._on_installed(self._flavor["id"], java_exe)

    def _on_failed(self, error: str) -> None:
        self._status_lbl.setText(f"Failed: {error}")
        self._progress.setVisible(False)
        self._install_btn.setText("Retry")
        self._install_btn.setEnabled(getattr(self, "_version_available", True))
        self._delete_btn.setEnabled(True)
        self._delete_btn.setVisible(True)   # offer cleanup of partial install
        self._toast("error", "Download failed", error[:200], 7000)
        from ..threading_utils import retire
        retire(self._worker)
        self._worker = None

    def _on_open_folder(self) -> None:
        folder = self._install_dir()
        if os.path.isdir(folder):
            import subprocess
            subprocess.Popen(["explorer", folder])

    def _on_delete(self) -> None:
        """Remove the installed JDK folder for the selected version."""
        from PyQt6.QtCore import QTimer

        if self._worker and self._worker.isRunning():
            return
        folder = self._install_dir()
        if not os.path.isdir(folder):
            self._refresh_install_status()
            return
        ver = self._get_version()
        fname = self._flavor["name"]

        def _perform() -> None:
            from ..cove_dialog import CoveConfirm
            if not CoveConfirm.ask(
                self.window(),
                "Delete Java runtime?",
                f"Remove {fname} Java {ver} from disk?\n\n{folder}\n\n"
                "Instances pointing at this runtime will fall back to "
                "Mojang's bundled JVM on their next launch.",
                ok="Delete",
                cancel="Keep",
            ):
                return

                import shutil

                try:
                    shutil.rmtree(folder)
                except OSError as exc:
                    self._toast(
                        "error", "Delete failed",
                        f"{str(exc)[:160]}\n(The JVM may still be running.)", 8000,
                    )
                    return
                # A deleted GraalVM must vanish from cached launch-time detection.
                try:
                    from ..graalvm import detect_graalvm
                    detect_graalvm.cache_clear()
                except Exception:  # noqa: BLE001 - cache reset is best-effort
                    pass
                self._log(f"Deleted Java runtime: {folder}")
                self._toast("success", "Java removed",
                            f"{fname} Java {ver} deleted.", 5000)
                self._refresh_install_status()

        # Defer to idle so the click cycle completes before the modal opens.
        QTimer.singleShot(0, _perform)

    def on_version_changed(self) -> None:
        """Called by the parent page when the shared version combo changes."""
        if self._worker and self._worker.isRunning():
            return
        self._status_lbl.setVisible(False)
        self._progress.setVisible(False)
        self._refresh_install_status()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(3_000)
        super().closeEvent(event)


# ── page ──────────────────────────────────────────────────────────────────────

class JavaPage(QWidget):
    """
    Sidebar page that lets the user install any of the three supported
    JDK distributions with a single click.
    """

    def __init__(self, toast, log, on_installed=None, parent=None) -> None:
        """
        Parameters
        ----------
        toast       callable(severity, title, content, duration)
        log         callable(msg, level="INFO")
        on_installed  optional callback(flavor_id, java_exe) — e.g. to
                      auto-fill the Settings java path.
        """
        super().__init__(parent)
        self.setObjectName("javaPage")
        self._toast = toast
        self._log = log
        self._on_installed = on_installed or (lambda fid, exe: None)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 16)
        root.setSpacing(0)

        # ── Page heading ──────────────────────────────────────────────────────
        root.addWidget(TitleLabel("Java Downloads"))
        headline = BodyLabel(
            "Install a Java runtime directly into Cove — no system installer required. "
            "The selected JDK will be stored in your Cove data folder and "
            "made available to all instances."
        )
        headline.setWordWrap(True)
        from ..theme import fg_faint
        self._headline = headline
        root.addWidget(headline)
        root.addSpacing(14)

        # ── Version selector (shared across all cards) ────────────────────────
        ver_row = QHBoxLayout()
        ver_lbl = SubtitleLabel("Java version:")
        ver_lbl.setStyleSheet("font-size: 13px;")
        ver_row.addWidget(ver_lbl)
        self._ver_combo = ComboBox()
        for v in _VERSIONS:
            if v == _VERSIONS[0]:
                label = f"Java {v} (LTS — Recommended)"
            else:
                label = f"Java {v} (LTS)"
            self._ver_combo.addItem(label, userData=v)
        self._ver_combo.setCurrentIndex(0)
        self._ver_combo.setFixedWidth(220)
        self._ver_combo.currentIndexChanged.connect(self._on_version_changed)
        ver_row.addWidget(self._ver_combo)
        ver_row.addStretch()
        root.addLayout(ver_row)
        root.addSpacing(16)

        # ── Scrollable card area ──────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")

        cards_container = QWidget()
        cards_container.setStyleSheet("background: transparent;")
        cards_layout = QVBoxLayout(cards_container)
        cards_layout.setContentsMargins(0, 0, 8, 0)
        cards_layout.setSpacing(12)

        self._cards: list[_JavaFlavorCard] = []
        for flavor in _FLAVORS:
            card = _JavaFlavorCard(
                flavor,
                on_installed=self._on_card_installed,
                get_version=lambda: int(self._ver_combo.currentData() or 21),
                toast=self._toast,
                log=self._log,
                parent=cards_container,
            )
            cards_layout.addWidget(card)
            self._cards.append(card)

        cards_layout.addStretch()
        scroll.setWidget(cards_container)
        root.addWidget(scroll, stretch=1)

        # ── Footer note ───────────────────────────────────────────────────────
        root.addSpacing(8)
        note = CaptionLabel(f"JDKs are stored in: {JDKS_DIR}")
        self._note = note
        root.addWidget(note)

        # The combo's initial index never fires currentIndexChanged, so push
        # the initial availability state once now that all cards exist.
        self._on_version_changed()

    # ── slots ─────────────────────────────────────────────────────────────────

    def refresh_theme(self) -> None:
        """Re-apply theme-dependent styling (called on live theme change)."""
        from ..theme import fg_faint

        self._headline.setStyleSheet(f"color: {fg_faint()}; margin-bottom: 8px;")
        self._note.setStyleSheet(f"color: {fg_faint()};")
        for card in self._cards:
            card.refresh_theme()

    def _on_version_changed(self) -> None:
        ver = int(self._ver_combo.currentData() or 0)
        for card in self._cards:
            supported = ver in _VERSION_SUPPORT.get(card.flavor_id(), ())
            card.set_version_available(supported)
            card.on_version_changed()

    def _on_card_installed(self, flavor_id: str, java_exe: str) -> None:
        self._on_installed(flavor_id, java_exe)
