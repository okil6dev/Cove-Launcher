"""
Regression test for the modal-dialog freeze.

The bug: qfluentwidgets' MaskDialogBase reaches QDialog.done() ONLY from a
100ms fade-out animation's finished signal. Clicking a button while the 200ms
fade-in is still running can destroy that animation's target, so exec() never
returns and the GUI thread is stuck forever.

Run: python test_dialog_freeze.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class _MessageCapture:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self._previous = None

    def __enter__(self):
        from PyQt6.QtCore import qInstallMessageHandler

        def _handler(_mode, _context, message):
            self.messages.append(str(message))

        self._previous = qInstallMessageHandler(_handler)
        return self.messages

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        from PyQt6.QtCore import qInstallMessageHandler

        qInstallMessageHandler(self._previous)


def _dialog():
    """A stock MessageBoxBase with its own top-level host window."""
    from qfluentwidgets import MessageBoxBase

    host = QWidget()
    host.resize(600, 400)
    host.show()
    return MessageBoxBase(host), host


def _exec_with_click(delay_ms: int, apply_fix: bool) -> float:
    """Open a dialog, click its primary button after *delay_ms*, time exec()."""
    if apply_fix:
        from launcher.fluent_dialogs import apply_fluent_dialog_fix

        apply_fluent_dialog_fix()

    dlg, host = _dialog()
    QTimer.singleShot(delay_ms, dlg.yesButton.click)
    start = time.perf_counter()
    dlg.exec()
    elapsed = time.perf_counter() - start
    dlg.deleteLater()
    host.deleteLater()
    return elapsed


def test_dialog_closed_immediately_does_not_hang():
    """The exact user action: click Create/Delete the instant it appears."""
    elapsed = _exec_with_click(10, apply_fix=True)
    assert elapsed < 3.0, f"exec() took {elapsed:.1f}s -- dialog hung"


def test_dialog_closed_mid_fade_in_does_not_hang():
    """Clicking partway through the 200ms fade-in used to be the worst case."""
    for delay in (0, 5, 25, 60, 120, 199):
        elapsed = _exec_with_click(delay, apply_fix=True)
        assert elapsed < 3.0, f"delay={delay}ms -> exec() took {elapsed:.1f}s"


def test_dialog_closed_after_fade_in():
    elapsed = _exec_with_click(400, apply_fix=True)
    assert elapsed < 3.0, f"exec() took {elapsed:.1f}s"


def test_cancel_button_does_not_hang():
    from launcher.fluent_dialogs import apply_fluent_dialog_fix

    apply_fluent_dialog_fix()
    dlg, host = _dialog()
    QTimer.singleShot(10, dlg.cancelButton.click)
    start = time.perf_counter()
    dlg.exec()
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"cancel hung for {elapsed:.1f}s"
    dlg.deleteLater()
    host.deleteLater()


def test_accept_still_returns_the_right_code():
    """Skipping the fade-out must not break the return value."""
    from PyQt6.QtWidgets import QDialog

    from launcher.fluent_dialogs import apply_fluent_dialog_fix

    apply_fluent_dialog_fix()
    dlg, host = _dialog()
    QTimer.singleShot(10, dlg.accept)
    dlg.exec()
    assert dlg.result() == int(QDialog.DialogCode.Accepted), \
        "accept() result changed"
    dlg.deleteLater()
    host.deleteLater()

    dlg2, host2 = _dialog()
    QTimer.singleShot(10, dlg2.reject)
    dlg2.exec()
    assert dlg2.result() == int(QDialog.DialogCode.Rejected), \
        "reject() result changed"
    dlg2.deleteLater()
    host2.deleteLater()


def test_fix_is_idempotent():
    from launcher.fluent_dialogs import apply_fluent_dialog_fix

    assert apply_fluent_dialog_fix() is False, "patch applied twice"
    assert apply_fluent_dialog_fix() is False


def test_line_edit_button_disconnect_does_not_touch_destroyed_signal():
    from qfluentwidgets.common.icon import FluentIcon as FIF
    from qfluentwidgets.components.widgets.line_edit import LineEditButton

    from launcher.fluent_dialogs import apply_fluent_dialog_fix

    apply_fluent_dialog_fix()
    button = LineEditButton(FIF.CLOSE)
    clicked = []
    button.clicked.connect(lambda: clicked.append(True))
    button.destroyed.connect(lambda: None)

    with _MessageCapture() as messages:
        button.disconnect()
        button.click()

    assert not clicked, "clicked slot should have been disconnected"
    assert not any("QObject::disconnect" in m for m in messages), messages
    button.deleteLater()


def test_invalid_font_point_size_is_clamped():
    from PyQt6.QtGui import QFont

    from launcher.fluent_dialogs import apply_fluent_dialog_fix

    apply_fluent_dialog_fix()
    font = QFont()

    with _MessageCapture() as messages:
        font.setPointSize(-1)

    assert font.pointSize() > 0
    assert not any("QFont::setPointSize" in m for m in messages), messages


def test_qfont_cpp_warning_is_silenced_but_real_errors_pass():
    """C++-side QFont noise is swallowed; genuine Qt errors surface."""
    import io
    import sys as _sys
    from PyQt6.QtCore import qCritical, qWarning
    from launcher.fluent_dialogs import apply_fluent_dialog_fix
    apply_fluent_dialog_fix()
    captured = io.StringIO()
    previous = _sys.stderr
    _sys.stderr = captured
    try:
        qWarning("QFont::setPointSize: Point size <= 0 (-1), "
                 "must be greater than 0")
        qCritical("QFONT-FILTER-PROBE-MARKER-98765")
        _APP.processEvents()
    finally:
        _sys.stderr = previous
    out = captured.getvalue()
    assert "QFont" not in out, f"QFont noise leaked: {out!r}"
    assert "PROBE-MARKER-98765" in out, f"real error lost: {out!r}"


def test_real_confirm_dialog_roundtrip():
    """A real MessageBox, as used for delete confirmation."""
    from qfluentwidgets import MessageBox

    from launcher.fluent_dialogs import apply_fluent_dialog_fix

    apply_fluent_dialog_fix()
    host = QWidget()
    host.resize(600, 400)
    host.show()

    box = MessageBox("Delete mod", "Delete 'sodium.jar'?", host)
    QTimer.singleShot(10, box.yesButton.click)
    start = time.perf_counter()
    box.exec()
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"MessageBox hung for {elapsed:.1f}s"
    box.deleteLater()
    host.deleteLater()


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
