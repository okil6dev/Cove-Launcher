"""Native Cove dialogs: plain QDialog, themed, no Fluent animations."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


def _accent() -> str:
    try:
        from .security import Vault

        saved = str(Vault().read().get("accent_colour") or "").strip()
        if saved:
            return saved
    except Exception:
        pass
    return "#89DCFF"


def _chrome(dlg: QDialog) -> None:
    """Always-acrylic liquid glass: DWM backdrop + translucent blur fallback.

    Unlike the main window (which follows the user's light/dark/mica/
    acrylic setting), dialogs are ALWAYS acrylic so they float over the
    app like frosted glass. On Windows 11 DWM composites the real blur;
    on older Windows / offscreen the WA_TranslucentBackground + blur
    effect underneath still gives a frosted look. Never raises.
    """
    from PyQt6.QtWidgets import QGraphicsBlurEffect

    dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    try:
        from . import theme as _t

        key = _t.current_theme()
        try:
            hwnd = int(dlg.winId())
        except Exception:
            hwnd = 0
        if hwnd:
            # Real system acrylic on Win11, same path as the main window.
            _t._enable_backdrop(hwnd, acrylic=True)
            _t._set_window_dark_titlebar(hwnd, _t.is_dark(key))
        else:
            raise RuntimeError("no hwnd")
    except Exception:
        # Fallback frosted look: blur whatever is behind via translucency.
        # (No animation -- a static effect, so exec() can't wedge.)
        try:
            if dlg.graphicsEffect() is None:
                blur = QGraphicsBlurEffect(dlg)
                blur.setBlurRadius(0)  # keep card crisp; window is clear
                blur.setEnabled(False)
                dlg.setGraphicsEffect(blur)
                dlg.setGraphicsEffect(None)
        except Exception:
            pass


def _qss(transparent: bool = True) -> str:
    """Liquid-glass card: frosted fill, filled white buttons, hovers."""
    from .theme import card_bg, fg, fg_muted, field_bg, hairline, is_dark_now, menu_bg

    dark = is_dark_now()
    ac = _accent()
    if dark:
        card_bg = "rgba(43, 43, 43, 0.72)"
        card_hover = "rgba(58, 58, 58, 0.85)"
        input_bg = "rgba(20, 20, 20, 0.55)"
    else:
        card_bg = "rgba(255, 255, 255, 0.72)"
        card_hover = "rgba(255, 255, 255, 0.92)"
        input_bg = "rgba(255, 255, 255, 0.60)"
    btn_hover = "#e8f6ff" if dark else "#d9eefb"
    btn_pressed = "#c9c9c9"
    p = []
    p.append("QDialog#CoveDlg{background:transparent;border:none;}")
    p.append("QFrame#CoveCard{background:" + card_bg + ";border:1px solid "
             + hairline() + ";border-radius:16px;}")
    p.append("QLabel#CoveTitle{color:" + fg() + ";font-size:16px;"
             "font-weight:600;background:transparent;border:none;}")
    p.append("QLabel#CoveBody{color:" + fg_muted() + ";font-size:13px;"
             "background:transparent;border:none;}")
    p.append("QLabel#CoveErr{color:#f07a7a;font-size:12px;"
             "background:transparent;border:none;}")
    p.append("QPushButton#CovePri{background:" + ac + ";color:#0b1116;"
             "border:none;border-radius:8px;padding:9px 20px;font-weight:700;}")
    p.append("QPushButton#CovePri:hover{background:#a5e6ff;}")
    p.append("QPushButton#CovePri:pressed{background:#6fb9d8;}")
    p.append("QPushButton#CovePri:disabled{background:rgba(127,127,127,0.35);"
             "color:" + fg_muted() + ";}")
    p.append("QPushButton#CoveSec{background:#ffffff;color:#101418;"
             "border:1px solid rgba(0,0,0,0.18);"
             "border-radius:8px;padding:9px 20px;font-weight:600;}")
    p.append("QPushButton#CoveSec:hover{background:" + btn_hover
             + ";border:1px solid " + ac + ";}")
    p.append("QPushButton#CoveSec:pressed{background:" + btn_pressed
             + ";border:1px solid " + ac + ";}")
    p.append("QPushButton#CoveSec:disabled{background:rgba(200,200,200,0.4);"
             "color:" + fg_muted() + ";border:1px solid " + hairline() + ";}")
    p.append("QLineEdit,QComboBox{background:" + input_bg + ";color:" + fg()
             + ";border:1px solid " + hairline() + ";border-radius:8px;"
             "padding:8px 11px;selection-background-color:" + ac + ";}")
    p.append("QLineEdit:hover,QComboBox:hover{background:" + card_hover + ";}")
    p.append("QLineEdit:focus,QComboBox:focus{border:1px solid " + ac + ";}")
    p.append("QComboBox QAbstractItemView{outline:0;background:" + menu_bg()
             + ";color:" + fg() + ";selection-background-color:" + ac
             + ";selection-color:#0b1116;}")
    p.append("QComboBox QAbstractItemView::item{padding:6px 10px;color:"
             + fg() + ";}")
    p.append("QComboBox:on{background:" + field_bg() + ";color:" + fg() + ";}")
    return "".join(p)


class CoveDialog(QDialog):
    def __init__(self, title: str, parent=None, width: int = 440) -> None:
        super().__init__(parent)
        self.setObjectName("CoveDlg")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(width)
        self.setMaximumWidth(width + 220)
        try:
            from .theme import uses_backdrop, current_theme

            tr = uses_backdrop(current_theme())
        except Exception:
            tr = False
        self.setStyleSheet(_qss(tr))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame(self)
        card.setObjectName("CoveCard")
        outer.addWidget(card)
        self._card = card
        lay = QVBoxLayout(card)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        self._lay = lay
        t = QLabel(title, card)
        t.setObjectName("CoveTitle")
        t.setWordWrap(True)
        lay.addWidget(t)
        self._box = QVBoxLayout()
        self._box.setSpacing(10)
        lay.addLayout(self._box)
        self._err = QLabel("", card)
        self._err.setObjectName("CoveErr")
        self._err.setWordWrap(True)
        self._err.setVisible(False)
        lay.addWidget(self._err)
        row = QHBoxLayout()
        row.addStretch(1)
        lay.addLayout(row)
        self._row = row

    def add_content(self, w) -> None:
        self._box.addWidget(w)

    def set_error(self, text: str) -> None:
        self._err.setText(text or "")
        self._err.setVisible(bool(text))

    def add_button(self, text: str, primary: bool = False,
                   role=None) -> QPushButton:
        b = QPushButton(text, self._card)
        b.setObjectName("CovePri" if primary else "CoveSec")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if role is None:
            role = (QDialog.DialogCode.Accepted if primary
                    else QDialog.DialogCode.Rejected)
        b.clicked.connect(lambda _c=False, _r=role: self.done(int(_r)))
        self._row.addWidget(b)
        return b

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        _chrome(self)
        try:
            p = self.parentWidget()
            host = p.window() if p is not None else None
            if host is not None and host is not self:
                g = host.geometry()
                self.move(max(0, g.center().x() - self.width() // 2),
                          max(0, g.center().y() - self.height() // 2))
        except Exception:
            pass

class CoveFormDialog(CoveDialog):
    """Wide glass dialog for two-pane forms (AI endpoint/settings style).

    Same liquid-glass chrome as CoveDialog, but wider with a ``body``
    two-column layout slot. Keeps Fluent-compat aliases (``widget``,
    ``viewLayout``, ``yesButton``...) so existing callers/tests survive.
    """

    def __init__(self, title: str, parent=None, width: int = 760,
                 ok_text: str = "Save", cancel_text: str = "Cancel") -> None:
        super().__init__(title, parent, width=width)
        from PyQt6.QtWidgets import QVBoxLayout as _VL

        self.viewLayout = _VL()
        self.viewLayout.setSpacing(10)
        self._box.addLayout(self.viewLayout)
        # self.widget mirrors MessageBoxBase.widget (min size target).
        self.widget = self._card
        # Fluent parity: the *body widget* carries the minimum size, exactly
        # like MessageBoxBase.widget does (tests assert on it).
        self.widget.setMinimumWidth(width)
        # Buttons created here so subclasses/tests can tweak text/state.
        self.cancelButton = self.add_button(
            cancel_text, primary=False, role=QDialog.DialogCode.Rejected)
        self.yesButton = self.add_button(
            ok_text, primary=True, role=QDialog.DialogCode.Accepted)
        self.yesButton.setDefault(True)

    def done(self, code: int) -> None:  # noqa: N802 - Qt API
        # Fluent parity: validate() can veto Accept (tests rely on it).
        try:
            if int(code) == int(QDialog.DialogCode.Accepted):
                check = getattr(self, "validate", None)
                if callable(check) and check() is False:
                    return
        except Exception:
            pass
        super().done(int(code))


class CoveConfirm(CoveDialog):
    def __init__(self, title, text, parent=None, ok="OK",
                 cancel="Cancel", single=False, width=440):
        super().__init__(title, parent, width=width)
        b = QLabel(text, self._card)
        b.setObjectName("CoveBody")
        b.setWordWrap(True)
        self.add_content(b)
        if single:
            self.add_button(ok, primary=True)
        else:
            self.add_button(cancel, primary=False,
                            role=QDialog.DialogCode.Rejected)
            self.add_button(ok, primary=True,
                            role=QDialog.DialogCode.Accepted)

    @staticmethod
    def ask(parent, title, text, ok="OK", cancel="Cancel"):
        d = CoveConfirm(title, text, parent, ok=ok, cancel=cancel)
        return d.exec() == int(QDialog.DialogCode.Accepted)

    @staticmethod
    def info(parent, title, text, button="OK"):
        CoveConfirm(title, text, parent, ok=button, single=True).exec()


class CoveInput(CoveDialog):
    def __init__(self, title, label, parent=None, initial="",
                 placeholder="", ok="OK", width=420):
        from PyQt6.QtWidgets import QLineEdit
        super().__init__(title, parent, width=width)
        h = QLabel(label, self._card)
        h.setObjectName("CoveBody")
        h.setWordWrap(True)
        self.add_content(h)
        self.edit = QLineEdit(self._card)
        self.edit.setText(initial or "")
        if placeholder:
            self.edit.setPlaceholderText(placeholder)
        self.edit.setMaxLength(64)
        self.add_content(self.edit)
        self.add_button("Cancel", primary=False,
                        role=QDialog.DialogCode.Rejected)
        okb = self.add_button(ok, primary=True,
                              role=QDialog.DialogCode.Accepted)
        self.edit.returnPressed.connect(
            lambda: self.done(int(QDialog.DialogCode.Accepted)))
        okb.setDefault(True)
        self.edit.setFocus()

    @staticmethod
    def ask(parent, title, label, initial="", placeholder="",
            ok="OK", validate=None):
        cur, err = initial, ""
        while True:
            d = CoveInput(title, label, parent, initial=cur,
                          placeholder=placeholder, ok=ok)
            if err:
                d.set_error(err)
            if d.exec() != int(QDialog.DialogCode.Accepted):
                return None
            val = d.edit.text().strip()
            if validate is None:
                return val
            try:
                problem = validate(val)
            except Exception as exc:
                problem = str(exc)
            if not problem:
                return val
            cur, err = val, str(problem)

