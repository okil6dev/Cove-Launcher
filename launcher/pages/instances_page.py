"""Instances page: vertical instance list, banner card, play + management."""

from __future__ import annotations

import os

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QListView,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    PrimaryPushButton,
    SubtitleLabel,
    ToolButton,
)

from ..instances import Instance


def _make_cube_icon(size: int = 96) -> QIcon:
    """Draw an isometric grass-block cube (Minecraft-style default icon)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = size / 96.0
    cx, top_y = 48 * s, 14 * s
    half_w, half_h = 30 * s, 15 * s   # top-diamond half extents
    depth = 42 * s                     # side-face height

    top_pt = QPointF(cx, top_y)
    right_pt = QPointF(cx + half_w, top_y + half_h)
    bottom_pt = QPointF(cx, top_y + 2 * half_h)
    left_pt = QPointF(cx - half_w, top_y + half_h)

    def poly(*pts: QPointF) -> QPolygonF:
        return QPolygonF(list(pts))

    outline = QPen(QColor(35, 35, 38), max(1.0, 2 * s))
    outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(outline)

    # Left (shaded) and right (lit) dirt faces
    left_face = poly(left_pt, bottom_pt,
                     QPointF(bottom_pt.x(), bottom_pt.y() + depth),
                     QPointF(left_pt.x(), left_pt.y() + depth))
    right_face = poly(right_pt, bottom_pt,
                      QPointF(bottom_pt.x(), bottom_pt.y() + depth),
                      QPointF(right_pt.x(), right_pt.y() + depth))
    p.setBrush(QColor(0x6f, 0x4d, 0x2a))          # shaded dirt
    p.drawPolygon(left_face)
    p.setBrush(QColor(0x8a, 0x60, 0x36))          # lit dirt
    p.drawPolygon(right_face)

    # Grass overhang strips along the top of each side face
    band = 10 * s
    p.setBrush(QColor(0x5f, 0x99, 0x3a))
    p.drawPolygon(poly(
        left_pt, bottom_pt,
        QPointF(bottom_pt.x(), bottom_pt.y() + band),
        QPointF(left_pt.x(), left_pt.y() + band)))
    p.setBrush(QColor(0x74, 0xb4, 0x47))
    p.drawPolygon(poly(
        right_pt, bottom_pt,
        QPointF(bottom_pt.x(), bottom_pt.y() + band),
        QPointF(right_pt.x(), right_pt.y() + band)))

    # Top grass face
    p.setBrush(QColor(0x8e, 0xcc, 0x52))
    p.drawPolygon(poly(top_pt, right_pt, bottom_pt, left_pt))

    # Subtle highlight sheen on the top face
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 26))
    p.drawPolygon(poly(top_pt, right_pt,
                       QPointF(cx, top_y + half_h),
                       QPointF(cx - half_w * 0.45, top_y + half_h * 0.62)))

    p.end()
    return QIcon(pm)


def _fmt_duration(seconds: int) -> str:
    """Human-friendly playtime: '3h 05m', '42m', '0m'."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _render_banner(
    banner_path: str | None,
    width: int,
    height: int,
    cube: QIcon,
    dark: bool,
) -> QPixmap:
    """Center-cropped banner image, or a neutral placeholder when unset."""
    pm = QPixmap(width, height)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, width, height), 10, 10)
    p.setClipPath(clip)

    src = QPixmap(banner_path) if banner_path and os.path.isfile(banner_path) else QPixmap()
    if not src.isNull():
        # Fill the whole area, cropping the overflow (16:9-ish cover fit).
        scaled = src.scaled(
            width, height,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        p.drawPixmap((width - scaled.width()) // 2,
                     (height - scaled.height()) // 2, scaled)
        # Soft top-to-bottom scrim so overlaid text stays readable.
        scrim = QPixmap(width, height)
        scrim.fill(Qt.GlobalColor.transparent)
        sp = QPainter(scrim)
        grad = QLinearGradient(0, 0, 0, height)
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(1.0, QColor(0, 0, 0, 140))
        sp.fillRect(0, 0, width, height, grad)
        sp.end()
        p.drawPixmap(0, 0, scrim)
    else:
        # Placeholder: soft wash + centered dimmed cube.
        wash = QColor(255, 255, 255, 14) if dark else QColor(0, 0, 0, 10)
        p.fillRect(0, 0, width, height, wash)
        side = max(48, int(64 * height / 180.0))
        icon_px = cube.pixmap(side, side)
        p.setOpacity(0.35)
        p.drawPixmap((width - icon_px.width()) // 2,
                     (height - icon_px.height()) // 2, icon_px)
        p.setOpacity(1.0)

    p.end()
    return pm


class InstancesPage(QWidget):
    """Home page: instance list (left) + banner/Play card (right) + toolbar."""

    playRequested = pyqtSignal(str)      # instance name
    newRequested = pyqtSignal()
    importRequested = pyqtSignal()
    editRequested = pyqtSignal(str)      # instance name (edit button)
    deleteRequested = pyqtSignal(str)
    openFolderRequested = pyqtSignal(str)
    selectionChanged = pyqtSignal(object)  # Instance | None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("instancesPage")
        self._instances: list[Instance] = []
        self._cube_icon = _make_cube_icon()

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(14)

        # Header card: title + count pill + management toolbar ---------------
        header = QFrame()
        header.setObjectName("instancesHeader")
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(16, 12, 12, 12)
        header_lay.setSpacing(10)

        title = SubtitleLabel("Instances")
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
        self.new_button.setToolTip("Create a new instance")
        self.import_button = ToolButton(FluentIcon.DOWNLOAD)
        self.import_button.setToolTip("Import a .mrpack / modpack zip")
        self.folder_button = ToolButton(FluentIcon.FOLDER)
        self.folder_button.setToolTip("Open instance folder")
        self.edit_button = ToolButton(FluentIcon.EDIT)
        self.edit_button.setToolTip("Edit instance settings")
        self.edit_button.setEnabled(False)
        self.delete_button = ToolButton(FluentIcon.DELETE)
        self.delete_button.setToolTip("Delete selected instance")
        for button in (
            self.new_button,
            self.import_button,
            self.edit_button,
            self.folder_button,
            self.delete_button,
        ):
            header_lay.addWidget(button)
        root.addWidget(header)

        # Body: vertical instance list (left) + banner/play card (right) --------
        body = QHBoxLayout()
        body.setSpacing(14)

        # Left column: instances listed top to bottom.
        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.ListMode)
        self.list_widget.setFlow(QListView.Flow.TopToBottom)
        self.list_widget.setIconSize(QSize(40, 40))
        self.list_widget.setGridSize(QSize(240, 60))
        self.list_widget.setSpacing(4)
        self.list_widget.setViewportMargins(4, 4, 4, 4)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setWordWrap(False)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setMinimumWidth(256)
        self.list_widget.setMaximumWidth(300)
        self.list_widget.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._apply_list_theme()
        body.addWidget(self.list_widget)

        # Empty state ----------------------------------------------------------
        self.empty_label = BodyLabel(
            "No instances yet.\nCreate one with  +  or import a modpack."
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color: rgba(128,128,128,0.75); font-size: 15px;")
        body.addWidget(self.empty_label, stretch=1)

        # Right card: banner area + centered Play + status row ------------------
        play_card = QFrame()
        play_card.setObjectName("playCard")
        play_lay = QVBoxLayout(play_card)
        play_lay.setContentsMargins(16, 12, 16, 14)
        play_lay.setSpacing(10)

        self.banner_label = QLabel()
        self.banner_label.setObjectName("bannerHolder")
        self.banner_label.setMinimumHeight(150)
        self.banner_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        play_lay.addWidget(self.banner_label, stretch=1)

        self.play_button = PrimaryPushButton(FluentIcon.PLAY, "PLAY")
        self.play_button.setFixedHeight(52)
        font = self.play_button.font()
        font.setPointSize(14)
        font.setBold(True)
        self.play_button.setFont(font)
        play_lay.addWidget(self.play_button)

        self.status_label = BodyLabel("Ready.")
        self.status_label.setStyleSheet("color: rgba(128,128,128,0.9);")
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        self.playtime_label = BodyLabel("")
        self.playtime_label.setStyleSheet("color: rgba(128,128,128,0.9);")
        status_row.addWidget(self.playtime_label)
        play_lay.addLayout(status_row)

        from qfluentwidgets import ProgressBar

        self.progress_bar = ProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)   # shown only while busy
        play_lay.addWidget(self.progress_bar)

        body.addWidget(play_card, stretch=1)
        root.addLayout(body, stretch=1)

        self._banner_pixmap: QPixmap | None = None

        self._apply_card_theme()
        self._set_empty_state(True)
        self._session_seconds: int | None = None  # live session (window-driven)

        # Wiring ---------------------------------------------------------------
        self.play_button.clicked.connect(self._emit_play)
        self.list_widget.itemDoubleClicked.connect(lambda _: self._emit_play())
        self.list_widget.itemSelectionChanged.connect(self._on_selection)
        self.new_button.clicked.connect(self.newRequested.emit)
        self.import_button.clicked.connect(self.importRequested.emit)
        self.folder_button.clicked.connect(
            lambda: self.openFolderRequested.emit(self.current_name() or "")
        )
        self.edit_button.clicked.connect(
            lambda: self.editRequested.emit(self.current_name() or "")
        )
        self.delete_button.clicked.connect(
            lambda: self.deleteRequested.emit(self.current_name() or "")
        )

    # -- helpers -----------------------------------------------------------------
    def _update_banner(self) -> None:
        """Render the selected instance's banner (or placeholder) + name tag."""
        from ..theme import is_dark_now

        width = max(self.banner_label.width(), 200)
        height = max(self.banner_label.height(), 120)
        inst = self.current_instance()
        pm = _render_banner(
            inst.banner_path if inst else None,
            width, height, self._cube_icon, is_dark_now(),
        )
        # Instance name + version bottom-left, over the scrim.
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor(255, 255, 255, 235) if inst else QColor(128, 128, 128, 160))
        font = self.banner_label.font()
        font.setPointSize(13)
        font.setBold(True)
        p.setFont(font)
        p.drawText(14, height - 14, inst.name if inst else "No instance selected")
        p.end()
        self._banner_pixmap = pm
        self.banner_label.setPixmap(pm)

    def current_name(self) -> str | None:
        item = self.list_widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def current_instance(self) -> Instance | None:
        name = self.current_name()
        for inst in self._instances:
            if inst.name == name:
                return inst
        return None

    # -- slots ---------------------------------------------------------------------
    def _icon_for(self, inst: Instance) -> QIcon:
        """Custom image when set and loadable; cube fallback otherwise."""
        if inst.icon_path and os.path.isfile(inst.icon_path):
            pm = QPixmap(inst.icon_path)
            if not pm.isNull():
                return QIcon(pm)
        return self._cube_icon

    def _apply_list_theme(self) -> None:
        from ..theme import item_sel_bg, item_hover_bg

        self.list_widget.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { border-radius: 10px; }"
            f"QListWidget::item:selected {{ background: {item_sel_bg()}; }}"
            f"QListWidget::item:hover:!selected {{ background: {item_hover_bg()}; }}"
        )

    def _apply_card_theme(self) -> None:
        """Style the header/footer cards for the current theme."""
        from ..theme import hairline, is_dark_now

        card_bg = "rgba(255, 255, 255, 0.05)" if is_dark_now() else "rgba(255, 255, 255, 0.6)"
        qss = (
            f"QFrame#instancesHeader, QFrame#playCard {{ background: {card_bg};"
            f" border: 1px solid {hairline()}; border-radius: 12px; }}"
            "QFrame#instancesHeader QLabel, QFrame#playCard QLabel"
            " { background: transparent; border: none; }"
        )
        for w in self.findChildren(QFrame):
            if w.objectName() in ("instancesHeader", "playCard"):
                w.setStyleSheet(qss)

    def refresh_theme(self) -> None:
        """Re-apply theme-dependent styling (called on live theme change)."""
        self._apply_list_theme()
        self._apply_card_theme()
        self._update_banner()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        # Keep the banner filling the card as the window resizes.
        if getattr(self, "banner_label", None) is not None:
            self._update_banner()

    def refresh(self, instances: list[Instance]) -> None:
        self._instances = list(instances)
        selected = self.current_name()
        self.list_widget.clear()
        for inst in self._instances:
            item = QListWidgetItem(self._icon_for(inst), inst.name)
            item.setData(Qt.ItemDataRole.UserRole, inst.name)
            subtitle = inst.mc_version + (
                f" · {inst.loader_id}" if inst.loader_id != "vanilla" else ""
            )
            played = int(getattr(inst, "playtime_seconds", 0) or 0)
            if played:
                subtitle += f" · {_fmt_duration(played)}"
            item.setToolTip(f"{inst.name}\n{subtitle}")
            self.list_widget.addItem(item)
        # Restore selection so the banner/playtime keep tracking the instance.
        # When there is no remembered selection, select the first row: without
        # a current item nothing is "selected", so _on_selection never fires
        # and the Edit button stays disabled forever (greyed out with
        # instances plainly listed above it).
        row = 0
        if selected is not None:
            matches = self.list_widget.findItems(
                selected, Qt.MatchFlag.MatchExactly)
            if matches:
                row = self.list_widget.row(matches[0])
        if self._instances:
            self.list_widget.setCurrentRow(row)
        self.count_label.setText(str(len(self._instances)))
        self._set_empty_state(not self._instances)
        # Refresh the Edit/Folder/Delete enablement from the real selection.
        # _on_selection only fires on a *change*, so a refresh that lands on the
        # same row would otherwise leave the buttons stale.
        self._sync_edit_enabled()
        self._update_playtime_label()
        self._update_banner()

    def _set_empty_state(self, empty: bool) -> None:
        self.empty_label.setVisible(empty)
        self.list_widget.setVisible(not empty)

    def set_busy(self, busy: bool, status_text: str | None = None) -> None:
        self.play_button.setEnabled(not busy and bool(self._instances))
        self.progress_bar.setVisible(busy)
        for widget in (
            self.new_button,
            self.import_button,
            self.delete_button,
            self.folder_button,
            self.list_widget,
        ):
            widget.setEnabled(not busy)
        if not busy:
            self._sync_edit_enabled()
        if status_text is not None:
            self.status_label.setText(status_text)

    def _sync_edit_enabled(self) -> None:
        """Edit button is active only when an instance is selected."""
        self.edit_button.setEnabled(bool(self.current_instance()))

    def _on_selection(self) -> None:
        self.edit_button.setEnabled(bool(self.current_instance()))
        self._update_playtime_label()
        self._update_banner()
        self.selectionChanged.emit(self.current_instance())

    # -- playtime ------------------------------------------------------------------
    def _update_playtime_label(self) -> None:
        """Show the selected instance's total (+ live session while running)."""
        inst = self.current_instance()
        if inst is None:
            self.playtime_label.setText("")
            return
        total = int(getattr(inst, "playtime_seconds", 0) or 0)
        if self._session_seconds is not None:
            self.playtime_label.setText(
                f"Session {_fmt_duration(self._session_seconds)}"
                f"  ·  Total {_fmt_duration(total + self._session_seconds)}"
            )
        else:
            self.playtime_label.setText(f"Played {_fmt_duration(total)}")

    def set_session(self, seconds: int | None) -> None:
        """Live session counter (seconds) or None when no game is running."""
        self._session_seconds = seconds
        self._update_playtime_label()

    def _emit_play(self) -> None:
        name = self.current_name()
        if name:
            self.playRequested.emit(name)

