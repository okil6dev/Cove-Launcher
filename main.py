"""
Cove Launcher — application entry point.

Responsibilities kept deliberately minimal:
  1. Verify third-party dependencies are importable *before* touching them,
     with a friendly native message box when they are missing.
  2. Construct the QApplication, apply the saved theme and show the window.
"""

from __future__ import annotations

import sys

REQUIRED_MODULES: tuple[str, ...] = ("PyQt6", "minecraft_launcher_lib")


def check_dependencies() -> None:
    """
    Abort early (with a clear message) when required packages are missing.

    Uses a native Win32 message box when possible because Qt itself may be
    the missing dependency and therefore unusable for error display.
    """
    missing: list[str] = []
    for module in REQUIRED_MODULES:
        try:
            __import__(module)
        except ImportError:
            missing.append(module)

    if not missing:
        return

    message = (
        f"The following required packages are missing:\n\n"
        f"    {', '.join(missing)}\n\n"
        f"Install them with:\n"
        f"    pip install -r requirements.txt"
    )
    print(message, file=sys.stderr)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Cove Launcher", 0x10)
    except Exception:
        pass  # Non-Windows platform: stderr output above is enough.
    sys.exit(1)


check_dependencies()

from qfluentwidgets import setThemeColor  # noqa: E402
from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402 - after dependency check

from launcher.constants import APP_NAME  # noqa: E402
from launcher.window import LauncherWindow  # noqa: E402
from launcher.security import Vault  # noqa: E402
from launcher.pages.settings_page import _DEFAULT_ACCENT  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    # Must run before any modal can open: Fluent dialogs can otherwise wedge
    # exec() forever (see launcher/fluent_dialogs.py for the mechanism).
    from launcher.fluent_dialogs import apply_fluent_dialog_fix

    apply_fluent_dialog_fix()

    # Flight recorder for every modal dialog plus a stuck-modal watchdog:
    # the intermittent dialog freeze gets caught with a full event trail in
    # %APPDATA%/Cove/dialog-debug.log (see launcher/dialog_debug.py).
    from launcher.dialog_debug import install_dialog_debug

    install_dialog_debug()

    # If the GUI thread ever blocks, dump every thread's stack so the freeze
    # can be diagnosed instead of guessed at (see launcher/hang_watchdog.py).
    from launcher.hang_watchdog import start_hang_watchdog, beat
    from PyQt6.QtCore import QTimer

    start_hang_watchdog()
    _pulse = QTimer()
    _pulse.timeout.connect(beat)
    _pulse.start(500)

    # Claim our own Windows AppUserModelID so the taskbar groups/pins us
    # under the Cove icon instead of the host python.exe default icon.
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Cove.Launcher.1.1.0"
        )
    except Exception:
        pass  # Non-Windows or very old shell: window icon still applies.

    from PyQt6.QtGui import QIcon

    from launcher.constants import ICON_FILE, asset_path
    app.setWindowIcon(QIcon(asset_path(ICON_FILE)))

    # Fluent design system: saved (or default) theme + accent colour.
    theme_key = "dark"
    try:
        vault = Vault()
        saved = vault.read().get("accent_colour") or ""
        accent = saved if saved else _DEFAULT_ACCENT
        theme_key = str(vault.read().get("theme", "") or "")
    except Exception:
        accent = _DEFAULT_ACCENT
    setThemeColor(QColor(accent))
    from launcher.theme import apply_theme
    apply_theme(theme_key)

    window = LauncherWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
