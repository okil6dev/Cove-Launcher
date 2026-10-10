"""Console page: streaming log view."""

from __future__ import annotations

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import PlainTextEdit


class ConsolePanel(PlainTextEdit):
    """Fluent-styled, read-only monospace log view."""

    MAX_BLOCKS = 5000  # keep memory bounded during long gaming sessions

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 9))
        self.document().setMaximumBlockCount(self.MAX_BLOCKS)
        # Long URLs must stay on ONE physical line, otherwise selecting them
        # in the GUI introduces line breaks that corrupt pasted links.
        self.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)

    def append_log(self, message: str) -> None:
        self.appendPlainText(message)
        scrollbar = self.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class ConsolePage(QWidget):
    """Page wrapper around the console panel."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("consolePage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        self.console = ConsolePanel()
        layout.addWidget(self.console)

    def log(self, message: str, level: str = "INFO") -> None:
        self.console.append_log(f"[{level}] {message}")
