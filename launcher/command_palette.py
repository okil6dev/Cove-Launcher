"""
Ctrl+K command palette — fuzzy-searchable quick actions.

Provides a Spotlight/VS-Code-style overlay with fuzzy matching over
navigation, instance launching, theming and misc launcher actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QKeyEvent, QPainter
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import SearchLineEdit, SubtitleLabel

from .theme import fg, fg_muted, hairline, is_dark_now, item_sel_bg


# ── commands ──────────────────────────────────────────────────────────────────

@dataclass
class Command:
    """A single palette action."""
    title: str
    subtitle: str = ""
    keywords: str = ""          # extra hidden search terms
    callback: Callable[[], None] = field(repr=False, default=None)


def fuzzy_score(query: str, text: str) -> int:
    """
    Subsequence fuzzy match score; 0 means "no match".

    Consecutive and word-start matches score higher, so "inst" ranks
    "Instances" above "Console - instance log".
    """
    if not query:
        return 1
    q, t = query.lower(), text.lower()
    score = 0
    ti = 0
    streak = 0
    for ch in q:
        found = t.find(ch, ti)
        if found < 0:
            return 0
        streak = streak + 1 if found == ti else 1
        bonus = 2 if found == 0 or t[found - 1] in " -_." else 0
        score += 1 + streak + bonus
        ti = found + 1
    # Prefer compact matches.
    return score + max(0, 10 - ti // 4)


# ── widget ────────────────────────────────────────────────────────────────────

class CommandPalette(QDialog):
    """Modal fuzzy-search overlay.  Enter runs, Esc cancels."""

    def __init__(self, parent: QWidget, commands: list[Command]) -> None:
        super().__init__(parent)
        self._commands = commands
        self._filtered: list[Command] = []

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)

        card = QFrame()
        card.setObjectName("paletteCard")
        card.setStyleSheet(self._card_qss())
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 120))
        card.setGraphicsEffect(shadow)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        title = SubtitleLabel("Command Palette")
        title.setStyleSheet(f"color: {fg()};")
        lay.addWidget(title)

        self.search = SearchLineEdit()
        self.search.setPlaceholderText(
            "Type a command…  (Enter to run, Esc to close)"
        )
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._refilter())
        self.search.installEventFilter(self)
        lay.addWidget(self.search)

        self.list = QListWidget()
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.list.itemActivated.connect(lambda _it: self._run_current())
        self.list.clicked.connect(lambda _ix: self._run_current())
        self.list.setStyleSheet(self._list_qss())
        lay.addWidget(self.list, stretch=1)

        root.addWidget(card)
        self.resize(560, 420)

        self._refilter()
        self.search.setFocus()

    # ── filtering ─────────────────────────────────────────────────────────────
    def _refilter(self) -> None:
        query = self.search.text().strip()
        scored: list[tuple[int, Command]] = []
        for cmd in self._commands:
            haystack = f"{cmd.title} {cmd.subtitle} {cmd.keywords}"
            score = fuzzy_score(query, haystack)
            if score > 0:
                scored.append((score, cmd))
        scored.sort(key=lambda pair: -pair[0])
        self._filtered = [cmd for _, cmd in scored[:12]]

        self.list.clear()
        for cmd in self._filtered:
            label = (cmd.title if not cmd.subtitle
                     else f"{cmd.title}   —  {cmd.subtitle}")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, cmd)
            self.list.addItem(item)
        if self._filtered:
            self.list.setCurrentRow(0)

    # ── running ───────────────────────────────────────────────────────────────
    def _run_current(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        cmd: Command = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
        if cmd.callback is not None:
            cmd.callback()

    # ── keyboard ──────────────────────────────────────────────────────────────
    def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
        from PyQt6.QtCore import QEvent

        if obj is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                self._move_selection(1)
                return True
            if key == Qt.Key.Key_Up:
                self._move_selection(-1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run_current()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def _move_selection(self, delta: int) -> None:
        row = self.list.currentRow() + delta
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)

    # ── theming / painting ────────────────────────────────────────────────────
    def _card_qss(self) -> str:
        card_bg = "#2b2b2b" if is_dark_now() else "#ffffff"
        return (
            f"QFrame#paletteCard {{ background: {card_bg};"
            f" border: 1px solid {hairline()}; border-radius: 12px; }}"
        )

    def _list_qss(self) -> str:
        return (
            "QListWidget { background: transparent; border: none;"
            f" color: {fg_muted()}; font-size: 14px; }}"
            "QListWidget::item { padding: 8px 10px; border-radius: 6px; }"
            f"QListWidget::item:selected {{ background: {item_sel_bg()};"
            f" color: {fg()}; }}"
            f"QListWidget::item:hover:!selected {{ color: {fg()}; }}"
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Dim the launcher behind the palette.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        painter.end()
        super().paintEvent(event)

    # ── positioning ───────────────────────────────────────────────────────────
    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        parent = self.parent()
        if parent is not None:
            geo = parent.geometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + max(60, (geo.height() - self.height()) // 3),
            )
        self.search.setFocus()
        self.search.selectAll()


def show_palette(parent: QWidget, commands: list[Command]) -> None:
    """Build + open a palette modal over *parent*."""
    palette = CommandPalette(parent, commands)
    palette.exec()
