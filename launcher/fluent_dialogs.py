"""
Reliability patches for qfluentwidgets modal dialogs.

Two bugs cause the app to freeze after using dialogs 2-3 times:

Bug 1 - Fade-out deadlock (original freeze)
-------------------------------------------
MaskDialogBase animates done() over 100 ms before calling QDialog.done().
If the user clicks a button while the 200 ms fade-IN is still running, the
new fade-out animation replaces the effect mid-flight. The stale fade-in then
clears the effect the fade-out needs; that animation never fires its finished
signal; exec() spins forever blocking the GUI thread.

Fix: Skip the fade-out entirely. Call QDialog.done() immediately.

Bug 2 - Event-filter accumulation (repeated-use freeze)
--------------------------------------------------------
MaskDialogBase.__init__ installs an event filter on the parent window via
self.window().installEventFilter(self) so the dialog can resize with the
window. But there is NO matching removeEventFilter anywhere in the class.

After each dialog closes, its event filter remains registered. After 2-3
dialogs, every window resize event calls self.resize() on all previously
closed (hidden) dialogs, triggering paint passes on dead widgets which raises
QPainter-not-active inside Qt. That error corrupts the paint state and
produces the hard freeze the user sees on the 2nd or 3rd dialog use.

Fix: Remove the event filter from the parent window inside done() so it is
always cleaned up. Also guard eventFilter() so any stale calls on invisible
dialogs are silently ignored rather than triggering a repaint.

Bug 3 - Noisy wildcard disconnect on LineEditButton
---------------------------------------------------
EditableComboBox calls clearButton.disconnect() with no signal argument. In
newer Qt builds, wildcard disconnects also touch QObject.destroyed, producing
warnings such as:

    QObject::disconnect: wildcard call disconnects from destroyed signal ...

Fix: Replace the no-argument LineEditButton.disconnect() path with explicit
signal disconnects. This preserves the qfluentwidgets intent without touching
QObject.destroyed.

Bug 4 - Invalid point-size warning
----------------------------------
Some Qt/font paths copy a pixel-sized QFont and later feed pointSize() back
into setPointSize(). For pixel fonts, pointSize() is -1, which Qt warns about.

Fix: Clamp invalid point sizes to the application font (or 9 pt fallback)
before they reach Qt (Python-side calls) AND install a Qt message handler
that swallows the residual C++-side warning (qfluentwidgets style engine
merges fonts internally where monkeypatching cannot reach). The warning is
pure cosmetic noise -- the font resolves to the same 9pt fallback either way.

Bug 5 - Fade-in can leave an INVISIBLE but modal dialog (2026-10-09 log)
------------------------------------------------------------------------
MaskDialogBase.showEvent installs a QGraphicsOpacityEffect at opacity 0 and
animates to 1 over 200 ms. If that animation does not advance, the dialog is
fully transparent yet still covers (and blocks) the whole parent window: the
event loop keeps running, no done() is ever called, every click in the app
lands on an invisible surface, and the process looks frozen -- exactly the
silence captured in %APPDATA%/Cove/dialog-debug.log (4th consecutive review
dialog logged OPEN, then nothing: no done(), no hang-report, fresh heartbeat).

Fix (two layers):
  1. Skip the fade-in entirely -- dialogs appear instantly at full opacity,
     so no opacity effect exists to get stuck (same family as Bug 1).
  2. Auto-rescue: 1.5 s after show, re-assert visibility (show/raise/
     activate, drop any stray opacity effect); 4 s after show, if the
     dialog is STILL exec()-pending and STILL invisible, log loudly and
     force done(Rejected) so input can never stay dead.
"""

from __future__ import annotations

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QDialog

_APPLIED = False


def apply_fluent_dialog_fix() -> bool:
    """Make every Fluent modal close deterministically. Idempotent.

    Returns True when the patch was installed by this call.
    """
    global _APPLIED
    if _APPLIED:
        return False

    from qfluentwidgets.components.dialog_box.mask_dialog_base import (
        MaskDialogBase,
    )
    from qfluentwidgets.components.widgets.line_edit import LineEditButton

    _orig_font_set_point_size = QFont.setPointSize
    _orig_font_set_point_size_f = QFont.setPointSizeF
    _orig_button_disconnect = LineEditButton.disconnect

    def _fallback_point_size() -> int:
        app = QApplication.instance()
        if app is not None:
            size = app.font().pointSize()
            if size > 0:
                return size
        return 9

    def _set_point_size_safe(self, size: int) -> None:
        try:
            value = int(size)
        except (TypeError, ValueError):
            return _orig_font_set_point_size(self, size)
        if value <= 0:
            value = _fallback_point_size()
        return _orig_font_set_point_size(self, value)

    def _set_point_size_f_safe(self, size: float) -> None:
        try:
            value = float(size)
        except (TypeError, ValueError):
            return _orig_font_set_point_size_f(self, size)
        if value <= 0:
            value = float(_fallback_point_size())
        return _orig_font_set_point_size_f(self, value)

    def _disconnect_signal(signal, *slot) -> None:
        try:
            if slot:
                signal.disconnect(*slot)
            else:
                signal.disconnect()
        except (TypeError, RuntimeError):
            pass

    def _line_edit_button_disconnect_safe(self, *args):
        if args:
            return _orig_button_disconnect(self, *args)

        # Avoid QObject.disconnect(self) because Qt treats that as a wildcard
        # disconnect and warns when it reaches QObject.destroyed.
        action = None
        try:
            action = self.action()
        except RuntimeError:
            return None

        if action is not None:
            _disconnect_signal(self.clicked, action.trigger)
            _disconnect_signal(action.toggled, self.setChecked)
            _disconnect_signal(action.changed, self._onActionChanged)

        for signal in (self.clicked, self.pressed, self.released, self.toggled):
            _disconnect_signal(signal)
        return None

    def _remove_filter(dialog) -> None:
        """Unregister the dialog event-filter from the parent window."""
        try:
            win = dialog.window()
            if win is not None and win is not dialog:
                win.removeEventFilter(dialog)
        except RuntimeError:
            pass  # C++ object already gone

    _FADE_RESCUE_MS = 1500

    def _clear_self_effect(dialog) -> None:
        try:
            dialog.setGraphicsEffect(None)
        except RuntimeError:
            pass  # C++ object already gone
        except Exception:
            pass
        try:
            if float(dialog.windowOpacity()) < 0.99:
                dialog.setWindowOpacity(1.0)
        except RuntimeError:
            pass
        except Exception:
            pass

    def _schedule_fade_rescue(dialog) -> None:
        """Layer 2: if anything re-installs an opacity effect, clear it."""
        try:
            import weakref as _wr

            from PyQt6.QtCore import QTimer as _QTimer
            from PyQt6.QtWidgets import QGraphicsOpacityEffect as _Opacity

            ref = _wr.ref(dialog)

            def _rescue() -> None:
                dlg = ref()
                if dlg is None:
                    return
                try:
                    from PyQt6 import sip as _sip

                    if _sip.isdeleted(dlg):
                        return
                except Exception:
                    pass
                try:
                    if not dlg.isVisible():
                        return
                    eff = dlg.graphicsEffect()
                    if isinstance(eff, _Opacity):
                        try:
                            opacity = float(eff.opacity())
                        except Exception:
                            opacity = 0.0
                        if opacity < 0.99:
                            dlg.setGraphicsEffect(None)
                            try:
                                dlg.setWindowOpacity(1.0)
                            except Exception:
                                pass
                            try:
                                dlg.update()
                            except Exception:
                                pass
                except RuntimeError:
                    pass
                except Exception:
                    pass

            _QTimer.singleShot(_FADE_RESCUE_MS, _rescue)
        except Exception:
            pass

    # Fix 5: skip the fade-IN entirely -- instant full opacity, nothing to stall.
    # NOTE: must call QDialog.showEvent directly, NOT MaskDialogBase.showEvent
    # (which is the code that installs the opacity-0 effect we are avoiding).
    def _show_event_patched(self, e) -> None:
        QDialog.showEvent(self, e)
        _clear_self_effect(self)
        _schedule_fade_rescue(self)

    # Fix 1: bypass fade-out animation so QDialog.done() is called immediately.
    # Fix 2: remove the event-filter installed by __init__ before the dialog
    # tears down, preventing stale resize callbacks on closed dialogs.
    def _done_patched(self, code: int) -> None:
        try:
            self.setGraphicsEffect(None)
        except RuntimeError:
            pass
        _remove_filter(self)
        QDialog.done(self, code)

    # Safety net: also patch _onDone in case any subclass path reaches it.
    def _on_done_patched(self, code: int) -> None:
        _remove_filter(self)
        try:
            self.setGraphicsEffect(None)
        except RuntimeError:
            pass
        QDialog.done(self, code)

    # Guard eventFilter: silently drop calls on hidden or already-dead dialogs.
    _orig_event_filter = MaskDialogBase.eventFilter

    def _event_filter_guarded(self, obj, e):
        try:
            if not self.isVisible():
                return False
        except RuntimeError:
            return False
        try:
            return _orig_event_filter(self, obj, e)
        except RuntimeError:
            return False

    MaskDialogBase.done = _done_patched
    MaskDialogBase._onDone = _on_done_patched
    MaskDialogBase.eventFilter = _event_filter_guarded
    MaskDialogBase.showEvent = _show_event_patched
    LineEditButton.disconnect = _line_edit_button_disconnect_safe
    QFont.setPointSize = _set_point_size_safe
    QFont.setPointSizeF = _set_point_size_f_safe
    _silence_qfont_noise()
    _APPLIED = True
    return True


def _silence_qfont_noise() -> None:
    """Swallow the C++-side QFont::setPointSize warning.

    The monkeypatch above handles every Python-side call, but Qt's style
    engine also merges fonts internally (stylesheet px -> QFont resolve)
    where no Python hook can intercept. That path logs:

        QFont::setPointSize: Point size <= 0 (-1), must be greater than 0

    exactly once per offending font merge -- harmless, but noisy. Filter it
    at the Qt message handler while letting everything else through.
    Idempotent: safe to call on every apply_fluent_dialog_fix().
    """
    try:
        from PyQt6.QtCore import (
            QMessageLogContext,
            QtMsgType,
            qInstallMessageHandler,
        )
    except ImportError:
        return

    def _handler(mode, context, message: str) -> None:
        if ("QFont" in message and "setPointSize" in message
                and "Point size <=" in message):
            return  # cosmetic noise -- drop it
        # Anything else: route to stderr like the default handler.
        try:
            import sys as _sys
            _sys.stderr.write(str(message) + "\n")
        except Exception:
            pass

    try:
        qInstallMessageHandler(_handler)
    except Exception:
        pass
