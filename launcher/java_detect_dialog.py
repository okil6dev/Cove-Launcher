"""
Java Detection Dialog for Cove Launcher.

Shows a table of all detected Java installations (like PrismLauncher's
"Detect Java Versions" dialog) and lets the user pick one.

Usage
-----
    dlg = JavaDetectDialog(parent=self)
    if dlg.exec() and dlg.selected_path:
        self.java_path_edit.setText(dlg.selected_path)
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    ProgressBar,
    PushButton,
)

from .cove_dialog import CoveFormDialog
from .java_detector import JavaInstall, detect_java_installations


# ── background worker ──────────────────────────────────────────────────────────

class _DetectThread(QThread):
    progress = pyqtSignal(int, int)       # done, total
    finished_results = pyqtSignal(list)   # list[JavaInstall]

    def run(self) -> None:  # noqa: D102
        results = detect_java_installations(
            progress_cb=lambda d, t: self.progress.emit(d, t)
        )
        self.finished_results.emit(results)


# ── source badge colours ───────────────────────────────────────────────────────

def _fg() -> str:
    try:
        from .theme import fg
        return fg()
    except Exception:
        return "#1a2129"


def _hairline() -> str:
    try:
        from .theme import hairline
        return hairline()
    except Exception:
        return "rgba(0,0,0,0.12)"


def _tile_bg() -> str:
    try:
        from .theme import icon_tile_bg
        return icon_tile_bg()
    except Exception:
        return "rgba(0,0,0,0.05)"

_SOURCE_STYLE = {
    "launcher": ("Launcher", "#89DCFF", "rgba(137,220,255,0.15)"),
    "PATH":     ("PATH",     "#4CAF50", "rgba(76,175,80,0.15)"),
    "registry": ("Registry", "#FF9800", "rgba(255,152,0,0.15)"),
    "common_path": ("Found",  "#9E9E9E", "rgba(158,158,158,0.12)"),
}


def _badge(source: str) -> QLabel:
    label_text, color, bg = _SOURCE_STYLE.get(
        source, (source, "#aaaaaa", "rgba(170,170,170,0.12)")
    )
    lbl = QLabel(label_text)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(
        f"color: {color}; background: {bg};"
        f"border: 1px solid {color}; border-radius: 4px;"
        "padding: 1px 6px; font-size: 9px; font-weight: bold;"
    )
    return lbl


# ── dialog ────────────────────────────────────────────────────────────────────

class JavaDetectDialog(CoveFormDialog):
    """
    Detects all Java installations on this machine and lets the user
    pick one.  On acceptance, ``selected_path`` holds the chosen exe.
    """

    def __init__(self, parent=None) -> None:
        super().__init__("Detect Java Installations", parent, width=680,
                         ok_text="Select", cancel_text="Cancel")
        self.selected_path: str | None = None
        self._results: list[JavaInstall] = []
        self._worker: _DetectThread | None = None
        self._build_ui()
        self._start_detect()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = self.viewLayout
        sub = CaptionLabel(
            "Cove scans your system for Java runtimes — registry, common install paths, "
            "PATH, and its own managed JDKs."
        )
        sub.setWordWrap(True)
        root.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_hairline()};")
        root.addWidget(sep)

        # Status / progress
        self._status_lbl = BodyLabel("Scanning…")
        root.addWidget(self._status_lbl)

        self._progress = ProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        root.addWidget(self._progress)

        # Results table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Version", "Vendor", "Source", "Path"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            f"QTableWidget {{ border: 1px solid {_hairline()}; border-radius: 8px; }}"
            "QTableWidget::item { padding: 6px 10px; }"
            "QTableWidget::item:selected { background: rgba(137,220,"
            f"255,0.18); color: {_fg()}; }}"
            f"QHeaderView::section {{ background: {_tile_bg()};"
            f"  border: none; border-bottom: 1px solid {_hairline()};"
            f"  padding: 6px 10px; font-weight: bold; color: {_fg()}; }}"
            "QTableWidget QTableCornerButton::section"
            " { background: transparent; border: none; }"
        )
        self._table.setVisible(False)
        self._table.doubleClicked.connect(self._on_accept)
        root.addWidget(self._table, stretch=1)

        # Empty hint (shown when scan finds nothing)
        self._empty_lbl = CaptionLabel(
            "No Java installations found on this machine.\n"
            "Use the Java Downloads tab to install one."
        )
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_lbl.setVisible(False)
        root.addWidget(self._empty_lbl)

        # Re-scan lives above the Cove Select/Cancel row.
        self._rescan_btn = PushButton(FluentIcon.SYNC, "Re-scan")
        self._rescan_btn.clicked.connect(self._start_detect)
        root.addWidget(self._rescan_btn)

        self._table.selectionModel().selectionChanged.connect(
            lambda *_: self.yesButton.setEnabled(
                bool(self._table.selectedRows() if hasattr(self._table, "selectedRows")
                     else self._table.currentRow() >= 0)
            )
        )
        self.yesButton.setEnabled(False)
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_accept)

    # ── scan orchestration ────────────────────────────────────────────────────

    def _start_detect(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._table.setVisible(False)
        self._empty_lbl.setVisible(False)
        self._table.setRowCount(0)
        self._results = []
        self.yesButton.setEnabled(False)
        self._rescan_btn.setEnabled(False)
        self._status_lbl.setText("Scanning for Java installations…")
        self._progress.setValue(0)
        self._progress.setVisible(True)

        from .threading_utils import retire
        retire(self._worker)
        self._worker = _DetectThread(self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_results.connect(self._on_results)
        self._worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._progress.setValue(int(done * 100 / total))
            self._status_lbl.setText(f"Scanning… ({done}/{total})")

    def _on_results(self, results: list) -> None:
        self._results = results
        self._rescan_btn.setEnabled(True)
        self._progress.setVisible(False)

        if not results:
            self._status_lbl.setText("No Java installations found.")
            self._empty_lbl.setVisible(True)
            return

        self._status_lbl.setText(
            f"Found {len(results)} Java installation{'s' if len(results) != 1 else ''}. "
            "Select one and click Select, or double-click a row."
        )
        self._table.setVisible(True)
        self._populate_table(results)

    def _populate_table(self, results: list[JavaInstall]) -> None:
        from PyQt6.QtGui import QBrush, QColor

        try:
            cell_fg = QColor(_fg())
        except Exception:
            cell_fg = None
        self._table.setRowCount(0)
        for install in results:
            row = self._table.rowCount()
            self._table.insertRow(row)

            # Version
            ver_item = QTableWidgetItem(install.version)
            font = QFont()
            font.setBold(True)
            ver_item.setFont(font)
            if cell_fg is not None:
                ver_item.setForeground(QBrush(cell_fg))
            self._table.setItem(row, 0, ver_item)

            # Vendor
            vendor_item = QTableWidgetItem(install.vendor)
            if cell_fg is not None:
                vendor_item.setForeground(QBrush(cell_fg))
            self._table.setItem(row, 1, vendor_item)

            # Source badge (as widget in cell)
            badge = _badge(install.source)
            cell_widget = QFrame()
            cell_layout = QHBoxLayout(cell_widget)
            cell_layout.setContentsMargins(6, 2, 6, 2)
            cell_layout.addWidget(badge)
            cell_layout.addStretch()
            self._table.setCellWidget(row, 2, cell_widget)

            # Path
            path_item = QTableWidgetItem(install.path)
            path_item.setToolTip(install.path)
            if cell_fg is not None:
                path_item.setForeground(QBrush(cell_fg))
            self._table.setItem(row, 3, path_item)

            # Store the install path as user data on the version cell
            ver_item.setData(Qt.ItemDataRole.UserRole, install.path)

        self._table.resizeRowsToContents()

    # ── accept/select ─────────────────────────────────────────────────────────

    def _on_accept(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, 0)
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.selected_path = path
            self.accept()

    # ── cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802
        from .threading_utils import retire
        retire(self._worker)
        super().closeEvent(event)
