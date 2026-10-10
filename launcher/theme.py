"""
Launcher-wide theme handling.

Five user-selectable themes:

    light / dark        plain opaque Fluent themes
    mica_light          Windows 11 Mica backdrop, light Fluent theme
    mica_dark           Windows 11 Mica backdrop, dark Fluent theme
    acrylic             Windows 11 Acrylic backdrop, dark Fluent theme

The Fluent widget theme is handled by qfluentwidgets' ``setTheme``; the
window backdrop is handled through DWM attributes so the desktop wallpaper
shows through for mica/acrylic modes.  On plain themes the transparency
stylesheet is removed again so controls stay readable.
"""

from __future__ import annotations

THEME_LIGHT = "light"
THEME_DARK = "dark"
MICA_LIGHT = "mica_light"
MICA_DARK = "mica_dark"
ACRYLIC = "acrylic"

DEFAULT_THEME = THEME_DARK

#: (display label, persisted key) pairs, in UI order.
THEME_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Light", THEME_LIGHT),
    ("Dark", THEME_DARK),
    ("Mica Light", MICA_LIGHT),
    ("Mica Dark", MICA_DARK),
    ("Acrylic", ACRYLIC),
)

_KEYS = tuple(key for _, key in THEME_OPTIONS)
_LABELS = {key: text for text, key in THEME_OPTIONS}

# Windows DWM attribute / values
_DWMWA_SYSTEMBACKDROP_TYPE = 38   # Win11 22H2+: 1=none 2=mica 3=acrylic 4=tabbed
_DWMWA_MICA_EFFECT = 1029         # older Win11 builds: undocumented Mica flag
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20

_BACKDROP_MICA = 2
_BACKDROP_ACRYLIC = 3

# Applied when a backdrop is active: strip every background so the
# window composited by DWM (Mica / Acrylic) shows through.
_TRANSPARENT_QSS = """
MSFluentWindow { background: transparent; border: none; }
QWidget { background: transparent; border: none; }
QStackedWidget { background: transparent; border: none; }
QWidget#navigationInterface { background: transparent; border: none; }
QWidget#navigationMenuWidget { background: transparent; border: none; }
QWidget#stackedWidget { background: transparent; border: none; }
QFrame { background: transparent; border: none; }
QScrollArea { background: transparent; border: none; }
"""


def normalize(key: str | None) -> str:
    """Return *key* if it is a known theme, else the default."""
    return key if key in _KEYS else DEFAULT_THEME


def label(key: str | None) -> str:
    """Human-readable label for *key*."""
    return _LABELS[normalize(key)]


def is_dark(key: str | None) -> bool:
    return normalize(key) in (THEME_DARK, MICA_DARK, ACRYLIC)


def uses_backdrop(key: str | None) -> bool:
    return normalize(key) in (MICA_LIGHT, MICA_DARK, ACRYLIC)


# ── palette helpers ───────────────────────────────────────────────────────────
#
# Several pages hardcode colours from the old always-dark look.  These helpers
# return the right colour for whatever theme is currently applied so pages can
# build their QSS dynamically (via ``refresh_theme``).

_current_key: str = DEFAULT_THEME


def current_theme() -> str:
    """Theme key most recently applied via :func:`apply_theme`."""
    return _current_key


def is_dark_now() -> bool:
    return is_dark(_current_key)


def fg() -> str:
    """Primary text colour."""
    return "#e6f2f2" if is_dark_now() else "#1a2129"


def fg_muted() -> str:
    """Secondary text colour."""
    return "#9fb4bd" if is_dark_now() else "#556875"


def fg_faint() -> str:
    """Tertiary/hint text colour."""
    return "#7d939d" if is_dark_now() else "#7c8a94"


def fg_disabled() -> str:
    return "#5c707a" if is_dark_now() else "#a5b1b9"


def hairline() -> str:
    """Subtle 1px border colour."""
    return "rgba(255,255,255,0.10)" if is_dark_now() else "rgba(0,0,0,0.12)"


def icon_tile_bg() -> str:
    return "rgba(255,255,255,0.06)" if is_dark_now() else "rgba(0,0,0,0.05)"


def item_sel_bg() -> str:
    """Selected-item background for item views."""
    return "#264f59" if is_dark_now() else "rgba(137,220,255,0.35)"


def item_hover_bg() -> str:
    return "#22333f" if is_dark_now() else "rgba(0,0,0,0.05)"


def accent_edge() -> str:
    """Checkbox / indicator border colour."""
    return "#3fa7a7" if is_dark_now() else "#2b7a7a"


def indicator_bg() -> str:
    return "#152028" if is_dark_now() else "#ffffff"


def apply_theme(key: str | None, window=None) -> str:
    """
    Apply the theme *key* app-wide (Fluent widgets) and to *window* (DWM
    backdrop + transparency).  Returns the normalised key.
    """
    from qfluentwidgets import Theme, setTheme

    key = normalize(key)
    global _current_key
    _current_key = key
    setTheme(Theme.DARK if is_dark(key) else Theme.LIGHT)
    if window is not None:
        _apply_window_effect(window, key)
    return key


# ── window effect internals ───────────────────────────────────────────────────

def _set_window_dark_titlebar(hwnd: int, dark: bool) -> None:
    try:
        import ctypes

        dwmapi = ctypes.windll.dwmapi
        value = ctypes.c_int(1 if dark else 0)
        dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value), 4,
        )
    except Exception:
        pass


def _enable_backdrop(hwnd: int, *, acrylic: bool) -> None:
    """Enable the Windows 11 Mica/Acrylic backdrop (best effort)."""
    try:
        import ctypes

        dwmapi = ctypes.windll.dwmapi
        if acrylic:
            # qframelesswindow's dark-mode Mica sets the legacy 1029 flag,
            # which overrides/conflicts with the modern backdrop attribute.
            off = ctypes.c_int(0)
            dwmapi.DwmSetWindowAttribute(hwnd, _DWMWA_MICA_EFFECT,
                                         ctypes.byref(off), 4)
            value = ctypes.c_int(_BACKDROP_ACRYLIC)
            dwmapi.DwmSetWindowAttribute(
                hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
                ctypes.byref(value), 4,
            )
            return
        wanted = _BACKDROP_MICA
        value = ctypes.c_int(wanted)
        result = dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(value), 4,
        )
        if result != 0:
            # Pre-22H2 Windows 11: undocumented Mica attribute.
            mica = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(
                hwnd, _DWMWA_MICA_EFFECT, ctypes.byref(mica), 4,
            )
    except Exception:
        pass  # Non-Windows or very old DWM: fall back to the opaque theme.


def _install_acrylic_guard(window, hwnd: int) -> None:
    """
    Keep the Acrylic backdrop alive.

    qfluentwidgets re-applies its own Mica on show/restore and after theme
    changes (some asynchronously), which silently replaces the Acrylic
    backdrop.  A lightweight timer polls the DWM attributes and re-asserts
    Acrylic only when something actually overrode it.  The timer stops
    itself as soon as the active theme is no longer Acrylic.
    """
    from PyQt6.QtCore import QTimer
    import ctypes

    if getattr(window, "_cove_acrylic_guard_timer", None) is not None:
        return

    def _tick():
        if current_theme() != ACRYLIC:
            timer.stop()
            return
        try:
            back = ctypes.c_int(0)
            legacy = ctypes.c_int(-1)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(
                hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(back), 4)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(
                hwnd, _DWMWA_MICA_EFFECT, ctypes.byref(legacy), 4)
            # Re-assert only when overridden (avoids needless DWM churn).
            if back.value != _BACKDROP_ACRYLIC or legacy.value != 0:
                _enable_backdrop(hwnd, acrylic=True)
        except Exception:
            pass

    timer = QTimer(window)
    timer.setInterval(300)
    timer.timeout.connect(_tick)
    timer.start()
    window._cove_acrylic_guard_timer = timer
    window.destroyed.connect(timer.stop)


def _apply_window_effect(window, key: str) -> None:
    """
    Toggle the window backdrop + transparency stylesheet on *window*.

    qfluentwidgets' ``FluentWidget`` base enables its own Mica by default and
    paints a fully transparent background while it is on — so the library's
    own ``setMicaEffectEnabled`` must be used to turn the backdrop off again,
    otherwise the solid background colour is never restored and the rounded
    window corners show through as black.
    """
    try:
        hwnd = int(window.winId())
    except Exception:
        hwnd = 0

    set_mica = getattr(window, "setMicaEffectEnabled", None)
    if uses_backdrop(key):
        if callable(set_mica):
            set_mica(True)   # transparent background + library-managed Mica
        window.setStyleSheet(_TRANSPARENT_QSS)
        if key == ACRYLIC and hwnd:
            # Upgrade the library's Mica to a true Acrylic backdrop.  The
            # library re-applies Mica asynchronously on theme change and on
            # show/restore (minimize), so an event guard re-asserts Acrylic.
            _enable_backdrop(hwnd, acrylic=True)
            _install_acrylic_guard(window, hwnd)
    else:
        if callable(set_mica):
            set_mica(False)  # restores solid light/dark background colour
        window.setStyleSheet("")

    if hwnd:
        _set_window_dark_titlebar(hwnd, is_dark(key))