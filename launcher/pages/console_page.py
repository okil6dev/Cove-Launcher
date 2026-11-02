"""Console page: streaming log view plus the log controls.

The Console tab shows everything the launcher prints -- install progress, auth
steps and the running game's own stdout/stderr.  Besides the view it offers:

* **Copy log** -- puts the whole on-screen log on the clipboard.
* **Open log file** -- opens the file every appended line is mirrored to.
* **Clear** -- empties the view only; the file keeps the full history.

Because the file only ever grows, a new launch can start with a clean view
(:meth:`ConsolePage.begin_session`) without losing the previous session.
"""

from __future__ import annotations

import os
import time

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices, QFont, QGuiApplication
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    PlainTextEdit,
    PushButton,
    SubtitleLabel,
)

from ..constants import CONSOLE_LOG_MAX_BYTES, CONSOLE_LOG_PATH

# Flushing on every single line would hammer the disk while the game streams
# output, so mirrored writes are buffered and flushed at most this often.
_FLUSH_INTERVAL_SECONDS = 0.5


class ConsolePanel(PlainTextEdit):
    """Fluent-styled, read-only monospace log view.

    When ``log_file`` is given, every appended line is *also* written to that
    file, giving the view a durable counterpart that "Open log file" can show
    and that survives clearing the on-screen text.
    """

    MAX_BLOCKS = 5000  # keep memory bounded during long gaming sessions

    def __init__(self, parent=None, log_file: str | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 9))
        self.document().setMaximumBlockCount(self.MAX_BLOCKS)
        # Long URLs must stay on ONE physical line, otherwise selecting them
        # in the GUI introduces line breaks that corrupt pasted links.
        self.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
        self._log_file: str | None = log_file
        self._log_handle = None
        self._last_flush = 0.0
        if self._log_file:
            self._open_log_handle()

    # -- log file ----------------------------------------------------------
    @property
    def log_file(self) -> str | None:
        """Path of the file mirroring this view, or ``None`` when disabled."""
        return self._log_file

    def _open_log_handle(self) -> None:
        """Open the mirror file for appending, rotating it when it grew huge."""
        path = self._log_file
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.getsize(path) > CONSOLE_LOG_MAX_BYTES:
                os.replace(path, path + ".old")
        except OSError:
            pass  # missing file / cannot rotate: just append as-is
        try:
            self._log_handle = open(  # noqa: SIM115 - kept open on purpose
                path, "a", encoding="utf-8", errors="replace"
            )
        except OSError:
            self._log_handle = None

    def _mirror_line(self, text: str) -> None:
        if self._log_handle is None and self._log_file:
            self._open_log_handle()
        if self._log_handle is None:
            return
        try:
            self._log_handle.write(text + "\n")
            now = time.monotonic()
            if now - self._last_flush >= _FLUSH_INTERVAL_SECONDS:
                self._log_handle.flush()
                self._last_flush = now
        except OSError:
            self._log_handle = None

    def flush_log(self) -> None:
        """Force buffered mirror writes to disk."""
        if self._log_handle is None:
            return
        try:
            self._log_handle.flush()
        except OSError:
            pass

    # -- view helpers ------------------------------------------------------
    def full_text(self) -> str:
        """The complete on-screen log."""
        return self.toPlainText()

    def copy_all(self) -> bool:
        """Copy the whole log to the clipboard; False when it is empty."""
        text = self.full_text()
        if not text:
            return False
        QGuiApplication.clipboard().setText(text)
        return True

    def open_log_file(self) -> bool:
        """Open the mirror file with the OS default handler."""
        path = self._log_file
        if not path:
            return False
        self.flush_log()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                with open(path, "a", encoding="utf-8"):
                    pass
        except OSError:
            return False
        return QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def clear_log(self) -> None:
        """Empty the view only -- the mirror file keeps the full history."""
        self.flush_log()
        self.clear()

    def append_log(self, message: str) -> None:
        self.appendPlainText(message)
        scrollbar = self.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self._mirror_line(message)


class ConsolePage(QWidget):
    """Page wrapper around the console panel: log view + its controls."""

    def __init__(self, parent=None, log_file: str | None = None) -> None:
        super().__init__(parent)
        # Resolved here (not as a default argument) so the path can be
        # redirected -- e.g. by tests -- before a page is built.
        self._log_file = CONSOLE_LOG_PATH if log_file is None else log_file
        self.setObjectName("consolePage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Header: title + log controls --------------------------------------
        header = QHBoxLayout()
        header.setSpacing(8)
        title = SubtitleLabel("Console")
        title.setStyleSheet("font-size: 20px;")
        header.addWidget(title)
        header.addStretch(1)

        self.copy_button = PushButton(FluentIcon.COPY, "Copy log")
        self.copy_button.setToolTip("Copy the whole log to the clipboard")
        self.copy_button.clicked.connect(self._on_copy)

        self.open_button = PushButton(FluentIcon.DOCUMENT, "Open log file")
        self.open_button.setToolTip(
            f"Open {self._log_file}" if self._log_file else "Open the log file")
        self.open_button.setEnabled(bool(self._log_file))
        self.open_button.clicked.connect(self._on_open_file)

        self.clear_button = PushButton(FluentIcon.DELETE, "Clear")
        self.clear_button.setToolTip(
            "Clear the view (the log file keeps the full history)")
        self.clear_button.clicked.connect(self._on_clear)

        for button in (self.copy_button, self.open_button, self.clear_button):
            header.addWidget(button)
        layout.addLayout(header)

        self.console = ConsolePanel(log_file=self._log_file or None)
        layout.addWidget(self.console, stretch=1)

    # -- controls ----------------------------------------------------------
    def _on_copy(self) -> None:
        if self.console.copy_all():
            self.log("Log copied to the clipboard.")
        else:
            self.log("Nothing to copy - the console is empty.", "WARN")

    def _on_open_file(self) -> None:
        if not self.console.open_log_file():
            self.log("Could not open the log file.", "ERROR")

    def _on_clear(self) -> None:
        self.console.clear_log()

    # -- log API -----------------------------------------------------------
    def log(self, message: str, level: str = "INFO") -> None:
        self.console.append_log(f"[{level}] {message}")

    def begin_session(self, title: str) -> None:
        """Start a fresh view for a new launch.

        The on-screen log is emptied so one instance's output can never be
        mistaken for the next one's; the mirror file keeps every earlier line.
        """
        self.console.clear_log()
        self.console.append_log(f"--- {title} ---")
