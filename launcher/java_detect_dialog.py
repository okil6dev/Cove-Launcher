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
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    ProgressBar,
    PrimaryPushButton,
    PushButton,
    TitleLabel,
)

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

class JavaDetectDialog(QDialog):
    """
    Detects all Java installations on this machine and lets the user
    pick one.  On acceptance, ``selected_path`` holds the chosen exe.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Detect Java Installations")
        self.setMinimumSize(680, 460)
        self.setModal(True)
        self.selected_path: str | None = None
        self._results: list[JavaInstall] = []
        self._worker: _DetectThread | None = None
        self._build_ui()
        self._start_detect()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        # Title
        root.addWidget(TitleLabel("Detect Java Installations"))
        sub = CaptionLabel(
            "Cove scans your system for Java runtimes — registry, common install paths, "
            "PATH, and its own managed JDKs."
        )
        sub.setWordWrap(True)
        root.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: rgba(255,255,255,0.10);")
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
            "QTableWidget { border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; }"
            "QTableWidget::item { padding: 6px 10px; }"
            "QTableWidget::item:selected { background: rgba(137,220,255,0.18); color: white; }"
            "QHeaderView::section { background: rgba(255,255,255,0.06); "
            "  border: none; border-bottom: 1px solid rgba(255,255,255,0.10);"
            "  padding: 6px 10px; font-weight: bold; }"
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

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._rescan_btn = PushButton(FluentIcon.SYNC, "Re-scan")
        self._rescan_btn.clicked.connect(self._start_detect)
        btn_row.addWidget(self._rescan_btn)
        btn_row.addStretch()

        self._cancel_btn = PushButton(FluentIcon.CLOSE, "Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._cancel_btn)

        self._select_btn = PrimaryPushButton(FluentIcon.ACCEPT_MEDIUM
                                             if hasattr(FluentIcon, "ACCEPT_MEDIUM")
                                             else FluentIcon.COMPLETED, "Select")
        self._select_btn.setEnabled(False)
        self._select_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(self._select_btn)

        root.addLayout(btn_row)

        self._table.selectionModel().selectionChanged.connect(
            lambda *_: self._select_btn.setEnabled(
                bool(self._table.selectedRows() if hasattr(self._table, "selectedRows")
                     else self._table.currentRow() >= 0)
            )
        )

    # ── scan orchestration ────────────────────────────────────────────────────

    def _start_detect(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._table.setVisible(False)
        self._empty_lbl.setVisible(False)
        self._table.setRowCount(0)
        self._results = []
        self._select_btn.setEnabled(False)
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
        self._table.setRowCount(0)
        for install in results:
            row = self._table.rowCount()
            self._table.insertRow(row)

            # Version
            ver_item = QTableWidgetItem(install.version)
            font = QFont()
            font.setBold(True)
            ver_item.setFont(font)
            self._table.setItem(row, 0, ver_item)

            # Vendor
            self._table.setItem(row, 1, QTableWidgetItem(install.vendor))

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
