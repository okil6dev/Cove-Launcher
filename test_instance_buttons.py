"""Instance management buttons: exactly which ones grey out, and when.

Run: python -u test_instance_buttons.py

The rule these tests pin down:
  * an install/import (a launcher-side task) greys out the whole page -- Edit
    included, which used to stay clickable in the middle of one;
  * nothing selected greys out the instance actions, but never New/Import, or
    an empty launcher could never create its first instance;
  * a game that is *running* greys out nothing, and the actions that become
    reachable really do something instead of silently doing nothing.

The real widgets are built offscreen and the real slots are called, so a
button left wired to the wrong rule fails here even though it would look fine
in a string-level test.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import launcher.cove_dialog as cove_dialog  # noqa: E402
import launcher.instance_editor as editor_mod  # noqa: E402
import launcher.instances as instances_mod  # noqa: E402

_ROOTS: list[str] = []
_WINDOW = None
_STUBBED = False


def _tmp_dir() -> str:
    root = tempfile.mkdtemp(prefix="cove_buttons_case_")
    _ROOTS.append(root)
    return root


def _pump(ms: int = 60) -> None:
    """Run the event loop briefly so queued slots (QTimer.singleShot) fire."""
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        APP.processEvents()
        time.sleep(0.005)


class _FakeWorker:
    """A worker that reports itself as running, without spawning a thread.

    The window only reads ``isRunning()`` to decide what is in flight, and
    hanging a real QThread just to kill it with ``terminate()`` can deadlock
    the process (terminate can land while that thread holds the GIL).
    """

    def isRunning(self) -> bool:  # noqa: N802 - mirrors the Qt API
        return True


def _stub_threads() -> None:
    """Neutralise the network threads LauncherWindow starts on construction."""
    global _STUBBED
    if _STUBBED:
        return
    import launcher.pages.mods_page as mods_page
    import launcher.pages.skins_page as skins
    import launcher.window as winmod

    # Selecting an instance kicks off a real Modrinth search on a worker
    # thread; a test must not touch the network.
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

    winmod.VersionListThread = _NoopVersionThread
    skins._MsProfileThread = _NoopMsProfileThread
    _STUBBED = True


class _Case:
    """The shared window, a throwaway registry, and this test's recorders."""

    BUTTONS = ("new", "import", "list", "play", "edit", "folder", "delete")

    def __init__(self, win, registry, toasts, logs) -> None:
        self.win = win
        self.registry = registry
        self.toasts = toasts
        self.logs = logs

    @property
    def page(self):
        return self.win.instances_page

    def enabled(self) -> dict:
        p = self.page
        return {
            "new": p.new_button.isEnabled(),
            "import": p.import_button.isEnabled(),
            "list": p.list_widget.isEnabled(),
            "play": p.play_button.isEnabled(),
            "edit": p.edit_button.isEnabled(),
            "folder": p.folder_button.isEnabled(),
            "delete": p.delete_button.isEnabled(),
        }

    def refresh(self) -> None:
        self.page.refresh(self.registry.list())

    def install(self):
        """Put the app mid-install: an installer that is still running."""
        self.win._installer = _FakeWorker()
        return self.win._installer

    def play(self, name: str = "Alpha"):
        """Put a game in flight and run the real 'game started' path.

        A real launch locks the page while it installs/verifies (``_on_play``
        -> ``_set_busy(True)``) and only ``_on_game_started`` unlocks it
        again, so the lock is applied here first on purpose.
        """
        self.win._set_busy(True, f"Preparing '{name}'...")
        self.win._launcher = _FakeWorker()
        self.win._launching_instance = self.registry.get(name)
        self.win._on_game_started(["java", "-jar", "client.jar"])
        return self.win._launcher

    def toast_text(self) -> str:
        return " | ".join(str(t) for t in self.toasts)

    def close(self) -> None:
        win = self.win
        win._stop_playtime()          # drop the live playtime QTimer
        win._installer = None
        win._launcher = None
        win._launching_instance = None
        win.WORKER_STALL_SECONDS = 300.0
        win._worker_last_activity = time.monotonic()
        win._set_busy(False)
        win.instances_page.status_label.setText("Ready.")


def _case() -> _Case:
    """The shared window wired to a temp registry holding Alpha and Beta."""
    global _WINDOW

    _stub_threads()

    root = _tmp_dir()
    # Instance dirs (and every delete) must stay inside the temp tree: the
    # registry resolves them through this module-level path.
    instances_mod.INSTANCES_DIR = os.path.join(root, "instances")
    # Same for the console mirror, so test chatter never lands in the user's
    # real %APPDATA%\Cove\logs\console.log.
    import launcher.pages.console_page as console_page_mod

    console_page_mod.CONSOLE_LOG_PATH = os.path.join(root, "console.log")

    from launcher.window import LauncherWindow

    if _WINDOW is None:
        _WINDOW = LauncherWindow()

    win = _WINDOW
    win.console_page._log_file = console_page_mod.CONSOLE_LOG_PATH
    win.console_page.console._log_file = console_page_mod.CONSOLE_LOG_PATH
    win.console_page.console._open_log_handle()
    registry = instances_mod.InstanceRegistry(os.path.join(root, "instances.json"))
    registry.register_existing("Alpha", launch_version_id="1.21.1",
                               mc_version="1.21.1")
    registry.register_existing("Beta", launch_version_id="1.21.1",
                               mc_version="1.21.1")
    win.registry = registry
    win.accounts = win.accounts or type("_NoAccounts", (), {"current": lambda s: None})()

    toasts: list = []
    logs: list = []
    win._toast = lambda *a, **k: toasts.append(a[2] if len(a) > 2 else a)
    win.log = lambda message, *a, **k: logs.append(message)

    case = _Case(win, registry, toasts, logs)
    case.close()                 # clear anything left by the previous test
    case.refresh()
    return case


def _patch_confirm(answer: bool, calls: list):
    """Answer CoveConfirm.ask without a dialog; returns a restore callable."""
    real = cove_dialog.CoveConfirm.__dict__["ask"]

    def fake(*args, **kwargs):
        calls.append(args)
        return answer

    cove_dialog.CoveConfirm.ask = staticmethod(fake)

    def restore() -> None:
        cove_dialog.CoveConfirm.ask = real

    return restore


# ------------------------------------------------------------ mid-install -- #
def test_install_greys_out_the_whole_page():
    case = _case()
    try:
        case.win._set_busy(True, "Installing 'Alpha' (1.21.1)...")
        enabled = case.enabled()
        assert not any(enabled.values()), enabled
        # isHidden(), not isVisible(): the page itself is never shown
        # offscreen, so isVisible() would be False either way.
        assert not case.page.progress_bar.isHidden()

        case.win._set_busy(False)
        enabled = case.enabled()
        assert all(enabled.values()), enabled
        assert case.page.progress_bar.isHidden()
    finally:
        case.close()


def test_refresh_during_an_install_cannot_re_enable_edit():
    """The reported bug: Edit stayed live while everything else greyed out."""
    case = _case()
    try:
        case.win._set_busy(True, "Installing 'Alpha' (1.21.1)...")
        case.refresh()          # what _on_install_succeeded does mid-install
        assert not case.page.edit_button.isEnabled()
        assert not case.page.play_button.isEnabled()
        assert not case.page.list_widget.isEnabled()
    finally:
        case.close()


# --------------------------------------------------------- nothing picked -- #
def test_no_selection_greys_only_the_instance_actions():
    case = _case()
    try:
        case.page.list_widget.setCurrentRow(-1)
        enabled = case.enabled()
        assert enabled["new"] and enabled["import"] and enabled["list"], enabled
        for name in ("play", "edit", "folder", "delete"):
            assert not enabled[name], f"{name} is live with nothing selected"
    finally:
        case.close()


def test_empty_list_still_offers_a_way_in():
    case = _case()
    try:
        for name in ("Alpha", "Beta"):
            case.registry.delete(name)
        case.refresh()
        enabled = case.enabled()
        assert enabled["new"] and enabled["import"], enabled
        for name in ("play", "edit", "folder", "delete"):
            assert not enabled[name], f"{name} is live with no instances at all"
    finally:
        case.close()


# ------------------------------------------------------------ mid-gameplay -- #
def test_game_running_greys_out_nothing():
    case = _case()
    try:
        case.play("Alpha")
        assert case.win._is_busy(), "a running game still counts as busy"
        enabled = case.enabled()
        assert all(enabled.values()), enabled
        assert case.page.progress_bar.isHidden(), "progress bar left up"
        assert "Game running" in case.page.status_label.text()
    finally:
        case.close()


def test_edit_opens_while_the_game_runs():
    case = _case()
    real = editor_mod.InstanceEditorWindow
    opened: list[str] = []

    class _Recorder:
        def __init__(self, instance, registry, parent) -> None:
            opened.append(instance.name)

        def show(self) -> None:  # noqa: D102 - nothing to show offscreen
            return

    try:
        editor_mod.InstanceEditorWindow = _Recorder
        case.play("Alpha")
        case.win._on_edit_instance("Alpha")
        assert opened == ["Alpha"], f"editor opened for {opened!r}"
    finally:
        editor_mod.InstanceEditorWindow = real
        case.close()


def test_edit_during_an_install_is_refused_with_a_reason():
    case = _case()
    real = editor_mod.InstanceEditorWindow
    opened: list[str] = []

    class _Recorder:
        def __init__(self, instance, registry, parent) -> None:
            opened.append(instance.name)

        def show(self) -> None:  # noqa: D102
            return

    try:
        editor_mod.InstanceEditorWindow = _Recorder
        case.install()
        case.win._worker_last_activity = time.monotonic()   # not a stall
        case.win._on_edit_instance("Alpha")
        assert opened == [], "the editor opened while files were being written"
        assert "install" in case.toast_text().lower(), case.toasts
    finally:
        editor_mod.InstanceEditorWindow = real
        case.close()


def test_deleting_the_running_instance_is_refused_with_a_reason():
    case = _case()
    calls: list = []
    restore = _patch_confirm(True, calls)
    try:
        case.play("Alpha")
        case.win._on_delete_instance("Alpha")
        _pump()
        assert case.registry.get("Alpha") is not None, "the running instance went"
        assert not calls, "a delete dialog was shown for the running instance"
        assert "running" in case.toast_text().lower(), case.toasts
    finally:
        restore()
        case.close()


def test_deleting_another_instance_while_playing_works():
    case = _case()
    calls: list = []
    restore = _patch_confirm(True, calls)
    try:
        case.play("Alpha")
        case.win._on_delete_instance("Beta")
        _pump()
        assert case.registry.get("Beta") is None, "delete did nothing while playing"
        assert case.registry.get("Alpha") is not None
        assert calls, "the confirmation was skipped"
    finally:
        restore()
        case.close()


def test_playing_again_is_refused_with_a_reason():
    """Play stays clickable while playing, so it must say why it won't start."""
    case = _case()
    try:
        case.play("Alpha")
        case.win._on_play("Beta")
        assert case.win._installer is None, "a second launch started"
        assert "still running" in case.toast_text().lower(), case.toasts
    finally:
        case.close()


def test_playtime_heartbeat_keeps_the_watchdog_off_a_running_game():
    """Quiet gameplay must not look like a hung download."""
    case = _case()
    try:
        case.play("Alpha")
        win = case.win
        win.WORKER_STALL_SECONDS = 0.1
        win._worker_last_activity = time.monotonic() - 999

        win._tick_playtime()          # one tick of the live playtime timer
        win._check_worker_stalled()

        assert win._launcher is not None, "the watchdog dropped the running game"
        assert "stuck" not in case.toast_text().lower(), case.toasts
    finally:
        case.close()


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
        for root in _ROOTS:
            shutil.rmtree(root, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
