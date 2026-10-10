"""Shared page helper: put a content page's layout inside a transparent
QScrollArea.

Why: Qt clamps a top-level widget UP to its layout's totalMinimumSize (verified
with a minimal repro -- an overridable minimumSizeHint does NOT stop this, and
only an explicit window minimum ignores the layout minimum). Several pages
(Server/Accounts/Skins/Mods) build side-by-side rows whose summed minimums are
988-1708px wide, which silently grew the launcher's boot window from its
intended boot size to >2200px right after the splash -- the "splash is not the
same size as the window" bug.

Wrapping the page content in a QScrollArea collapses the page's minimum size
to ~zero, so the window can never be dragged wider. Content that genuinely
needs more than the available area gets scrollbars instead of clipping or
resizing the whole window. The scroll area and its container are transparent so
the mica/acrylic backdrops keep showing through, exactly like before.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget


def scrollable_page_root(
    page: QWidget,
    margins: tuple[int, int, int, int] = (24, 20, 24, 16),
    spacing: int = 14,
) -> QVBoxLayout:
    """Give *page* a scrollable, transparent content area.

    Returns the QVBoxLayout to fill with the page's content (the same role
    ``QVBoxLayout(self)`` had before), carrying the page's original margins
    and spacing so nothing moves visually when everything fits.
    """
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    scroll = QScrollArea()
    scroll.setObjectName("pageScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    scroll.viewport().setAutoFillBackground(False)
    # Narrow selectors on purpose: anything deeper keeps its own card
    # background (a broad "QScrollArea QWidget" rule would blank the cards).
    scroll.setStyleSheet(
        "QScrollArea#pageScroll { background: transparent; border: none; }"
        "QScrollArea#pageScroll > QWidget { background: transparent; }"
    )

    inner = QWidget()
    inner.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    scroll.setWidget(inner)
    outer.addWidget(scroll)

    root = QVBoxLayout(inner)
    root.setContentsMargins(*margins)
    root.setSpacing(spacing)
    return root
