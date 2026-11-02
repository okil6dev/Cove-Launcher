"""Console tab: mirrored log file, its controls, and the per-launch reset.

Run: python -u test_console_page.py

The Console tab gained controls (copy the whole log, open the log file, clear)
backed by a file every printed line is mirrored to. These tests build the real
widgets offscreen and drive the real ``_on_play`` path, so they catch a broken
signal connection or a missed reset that a string-level test cannot see.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

from PyQt6.QtCore import QThread, pyqtSignal  # noqa: E402

import launcher.pages.console_page as console_page_mod  # noqa: E402
from launcher.pages.console_page import ConsolePage, ConsolePanel  # noqa: E402

_ALIVE: list = []


def _tmp_dir() -> str:
    return tempfile.mkdtemp(prefix="cove_console_case_")


def _panel(log_file: str) -> ConsolePanel:
    panel = ConsolePanel(log_file=log_file)
    _ALIVE.append(panel)
    return panel


def _page(log_file: str) -> ConsolePage:
    page = ConsolePage(log_file=log_file)
    _ALIVE.append(page)
    return page


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class _RecordingClipboard:
    """Stand-in for QClipboard that records every write our code makes."""

    def __init__(self) -> None:
        self.text = ""
        self.writes: list[str] = []

    def setText(self, value: str) -> None:  # noqa: N802 - mirrors the Qt API
        self.writes.append(value)
        self.text = value


class _FakeGuiApplication:
    """Minimal QGuiApplication whose clipboard never touches the OS.

    The offscreen platform's real clipboard is not reliable across tests that
    share one process (a later write can land as an empty string), so the
    copy tests assert against this recorder instead. On a real desktop the
    call is the ordinary ``QGuiApplication.clipboard().setText(text)``.
    """

    _clipboard = _RecordingClipboard()

    @staticmethod
    def clipboard() -> _RecordingClipboard:
        return _FakeGuiApplication._clipboard


@contextmanager
def _fake_clipboard():
    """Route the console page's clipboard writes into a recorder."""
    real = console_page_mod.QGuiApplication
    fake = _FakeGuiApplication._clipboard
    fake.text = ""
    fake.writes = []
    console_page_mod.QGuiApplication = _FakeGuiApplication
    try:
        yield fake
    finally:
        console_page_mod.QGuiApplication = real


# ------------------------------------------------------- mirroring to disk --
def test_appended_lines_are_mirrored():
    root = _tmp_dir()
    try:
        log = os.path.join(root, "console.log")
        panel = _panel(log)
        panel.append_log("hello there")
        panel.flush_log()
        assert panel.full_text().strip() == "hello there"
        assert "hello there" in _read(log)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_clear_empties_the_view_but_keeps_the_file():
    root = _tmp_dir()
    try:
        log = os.path.join(root, "console.log")
        panel = _panel(log)
        panel.append_log("before clear")
        panel.clear_log()
        assert panel.full_text() == ""
        panel.flush_log()
        assert "before clear" in _read(log), "clearing must not wipe the log file"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_mirroring_can_be_disabled():
    panel = ConsolePanel(log_file=None)
    _ALIVE.append(panel)
    panel.append_log("not persisted")
    assert panel.full_text().strip() == "not persisted"
    assert panel.log_file is None


# ------------------------------------------------------------ the controls --
def test_copy_all_hands_the_whole_log_to_the_clipboard():
    root = _tmp_dir()
    try:
        with _fake_clipboard() as clip:
            panel = _panel(os.path.join(root, "console.log"))
            assert panel.copy_all() is False, "an empty log must not copy"
            assert clip.writes == [], "the clipboard was written to for nothing"

            panel.append_log("line one")
            panel.append_log("line two")
            assert panel.copy_all() is True
            assert clip.text == "line one\nline two"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_page_header_exposes_the_log_controls():
    root = _tmp_dir()
    try:
        page = _page(os.path.join(root, "console.log"))
        assert page.copy_button.text() == "Copy log"
        assert page.open_button.text() == "Open log file"
        assert page.clear_button.text() == "Clear"
        assert page.open_button.isEnabled()
        # The tooltip names the real path so "where do logs live?" is answerable.
        assert root in page.open_button.toolTip()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_buttons_drive_the_panel():
    root = _tmp_dir()
    try:
        page = _page(os.path.join(root, "console.log"))
        page.log("a warning", "WARN")
        assert page.console.full_text().strip() == "[WARN] a warning"

        with _fake_clipboard() as clip:
            page.copy_button.click()
            assert clip.writes == ["[WARN] a warning"]

        page.clear_button.click()
        assert page.console.full_text() == ""
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_open_log_file_hands_the_path_to_the_os():
    root = _tmp_dir()
    opened: list[str] = []
    real_desktop = console_page_mod.QDesktopServices

    class _FakeDesktopServices:
        @staticmethod
        def openUrl(url):  # noqa: N802 - mirrors the Qt API
            opened.append(url.toLocalFile())
            return True

    try:
        console_page_mod.QDesktopServices = _FakeDesktopServices
        page = _page(os.path.join(root, "console.log"))
        page.open_button.click()
        target = os.path.join(root, "console.log")
        # QUrl round-trips to a forward-slash path; compare canonical forms.
        assert [os.path.normpath(p) for p in opened] == [os.path.normpath(target)]
        assert os.path.exists(target), "the file must exist before it can open"
    finally:
        console_page_mod.QDesktopServices = real_desktop
        shutil.rmtree(root, ignore_errors=True)


# -------------------------------------------------------- the launch reset --
def test_begin_session_starts_a_fresh_view():
    root = _tmp_dir()
    try:
        log = os.path.join(root, "console.log")
        page = _page(log)
        page.log("output from the first instance")
        page.begin_session("Launching 'Second'")

        assert "output from the first instance" not in page.console.full_text()
        assert page.console.full_text().strip() == "--- Launching 'Second' ---"

        page.console.flush_log()
        history = _read(log)
        assert "output from the first instance" in history, "history must survive"
        assert "Launching 'Second'" in history
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------- the reset on a real launch ----
_WINDOW = None
_APP = None            # kept alive or Qt tears the whole world down
_LOG_ROOT = None
_STUBBED = False


class _StubAccounts:
    def current(self):
        return None


def _stub_threads() -> None:
    """Neutralise the network threads LauncherWindow starts on construction."""
    global _STUBBED
    if _STUBBED:
        return
    import launcher.pages.mods_page as mods_page
    import launcher.pages.skins_page as skins
    import launcher.window as winmod

    # Selecting an instance kicks off a real Modrinth search on a worker
    # thread; a test must not touch the network (and a still-running search
    # thread aborts the process when Qt destroys it at exit).
    mods_page.ModsPage.on_instance_changed = lambda self: None

    class _NoopVersionThread(QThread):
        ready = pyqtSignal(list)
        failed = pyqtSignal(str)

        def run(self):  # noqa: D102 - no network in tests
            return

    class _NoopMsProfileThread(QThread):
        ready = pyqtSignal(dict)
        failed = pyqtSignal(str)

        def __init__(self, *args, **kwargs) -> None:
            super().__init__()

        def run(self):  # noqa: D102 - no network in tests
            return

    class _NoopInstaller(QThread):
        # Mirrors InstallerThread's signals so _on_play can wire them all up.
        status = pyqtSignal(str)
        progress_max = pyqtSignal(int)
        progress_value = pyqtSignal(int)
        succeeded = pyqtSignal(str, str)
        failed = pyqtSignal(str)

        def __init__(self, *args, **kwargs) -> None:
            super().__init__()

        def run(self):  # noqa: D102 - nothing to install in tests
            return

        def start(self):  # noqa: D102 - never spawn a real thread in tests
            return

    winmod.VersionListThread = _NoopVersionThread
    winmod.InstallerThread = _NoopInstaller
    skins._MsProfileThread = _NoopMsProfileThread
    _STUBBED = True


def _window_with_instance():
    """A LauncherWindow wired to a throwaway registry and console log file."""
    global _WINDOW, _APP, _LOG_ROOT

    from PyQt6.QtWidgets import QApplication as _QApplication

    from launcher.instances import InstanceRegistry

    if _QApplication.instance() is None:
        _APP = _QApplication([])
    _stub_threads()

    root = _tmp_dir()
    _LOG_ROOT = root
    console_page_mod.CONSOLE_LOG_PATH = os.path.join(root, "console.log")

    from launcher.window import LauncherWindow

    if _WINDOW is None:
        _WINDOW = LauncherWindow()

    win = _WINDOW
    # Point this window's console at the throwaway file (it was built before
    # the constant was redirected, or by an earlier test).
    win.console_page._log_file = console_page_mod.CONSOLE_LOG_PATH
    win.console_page.console._log_file = console_page_mod.CONSOLE_LOG_PATH
    win.console_page.console._open_log_handle()

    # Clear any worker left over from a previous test.
    stale = getattr(win, "_installer", None)
    if stale is not None and isinstance(stale, QThread) and stale.isRunning():
        stale.terminate()
        stale.wait(2000)
    win._installer = None
    win._launcher = None
    win._set_busy(False)

    win.registry = InstanceRegistry(os.path.join(root, "instances.json"))
    win.registry.create("Test Instance", "1.21.1")
    win.system_java = lambda: "java"          # bypass system Java detection
    if win.accounts is None:
        win.accounts = _StubAccounts()
    return win


def _settle(win) -> None:
    """Drop the stubbed installer so the next launch starts unblocked."""
    installer = getattr(win, "_installer", None)
    if installer is not None and isinstance(installer, QThread) and installer.isRunning():
        installer.wait(2000)
    win._installer = None
    win._launcher = None
    win._set_busy(False)


def test_launching_another_instance_clears_the_console():
    win = _window_with_instance()
    console = win.console_page.console

    console.clear_log()
    console.append_log("OLDPROJECT leftover output")

    try:
        win._on_play("Test Instance")

        text = console.full_text()
        assert "OLDPROJECT" not in text, "previous instance output leaked through"
        assert "--- Launching 'Test Instance' ---" in text
        assert "Installing/verifying" in text
    finally:
        _settle(win)


def test_relaunching_also_starts_clean():
    """The reset is per launch, not just per different instance."""
    win = _window_with_instance()
    console = win.console_page.console

    console.clear_log()
    console.append_log("first run output")
    try:
        win._on_play("Test Instance")
        assert "first run output" not in console.full_text()
        _settle(win)

        console.append_log("second run output")
        win._on_play("Test Instance")
        assert "second run output" not in console.full_text()
    finally:
        _settle(win)


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {exc!r}")
            else:
                print(f"ok    {name}")
    finally:
        if _LOG_ROOT:
            shutil.rmtree(_LOG_ROOT, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
