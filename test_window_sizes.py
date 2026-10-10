"""Window-size settings: defaults, clamping, persistence and the Settings UI.

Run: python -u test_window_sizes.py

Guards the wiring behind Settings -> Window Sizes: the shared module that
clamps every value, the main window booting at (and applying) the configured
size, the editors picking theirs up, and the Settings controls that drive it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

APP = QApplication.instance() or QApplication([])

from launcher import window_sizes as ws  # noqa: E402
from launcher.pages.settings_page import SettingsPage  # noqa: E402

_ALIVE: list = []
_LOGS: list = []
_TOASTS: list = []


class FakeVault:
    """dict-backed stand-in for launcher.security.Vault."""

    def __init__(self, data=None) -> None:
        self._data = dict(data or {})

    def read(self) -> dict:
        return dict(self._data)

    def write(self, data) -> None:
        self._data = dict(data)


class BrokenVault:
    """A locked vault: every access raises."""

    def read(self):
        raise RuntimeError("vault locked")

    def write(self, data):
        raise RuntimeError("vault locked")


class HostWindow(QWidget):
    """Top-level window standing in for LauncherWindow."""

    def __init__(self) -> None:
        super().__init__()
        self.applied: dict | None = None

    def apply_window_sizes(self, sizes) -> None:
        self.applied = dict(sizes)


def _page(vault) -> tuple[SettingsPage, HostWindow]:
    host = HostWindow()
    page = SettingsPage(
        vault=vault,
        log=lambda *a, **k: _LOGS.append(a),
        toast=lambda *a, **k: _TOASTS.append(a),
        parent=host,
    )
    _ALIVE.extend([host, page])
    return page, host


def _pump(times: int = 30) -> None:
    for _ in range(times):
        APP.processEvents()


def _temp_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="cove_size_test_")


# ------------------------------------------------------------- the module --
def test_defaults_match_the_documented_boot_sizes():
    assert ws.default_sizes() == {
        "main": (1280, 820),
        "instance_editor": (900, 620),
        "server_editor": (900, 620),
    }


def test_clamp_enforces_minimums_and_maximums():
    # Below the minimum -> the minimum, never something unusable.
    assert ws.clamp_size("main", 10, 10) == (900, 620)
    assert ws.clamp_size("instance_editor", -5, 0) == (760, 520)
    # Absurdly large -> the absolute ceiling.
    assert ws.clamp_size("main", 99999, 99999) == (ws.MAX_WIDTH, ws.MAX_HEIGHT)
    # Non-numeric input -> that window's default instead of an exception.
    assert ws.clamp_size("main", "wide", None) == (1280, 820)
    assert ws.clamp_size("server_editor", [], {}) == (900, 620)


def test_normalize_merges_onto_defaults_and_drops_junk():
    merged = ws.normalize({
        "main": [1400, 880],          # valid
        "server_editor": "zz",        # malformed -> default
        "no_such_window": [1, 2],     # unknown -> dropped
        "instance_editor": {"w": 1},  # not a 2-sequence -> default
    })
    assert merged["main"] == (1400, 880)
    assert merged["server_editor"] == (900, 620)
    assert merged["instance_editor"] == (900, 620)
    assert "no_such_window" not in merged
    # Non-mapping blobs (None, a string, a list) fall back wholesale.
    for junk in (None, "nonsense", [1, 2, 3]):
        assert ws.normalize(junk) == ws.default_sizes()


def test_save_and_load_round_trip():
    vault = FakeVault({"accent_colour": "#123456"})
    ws.save_sizes(vault, {"main": (1500, 950)})

    stored = vault.read()[ws.VAULT_KEY]
    assert stored["main"] == [1500, 950]
    assert isinstance(stored["main"][0], int)
    # Untouched windows keep their defaults, and other secrets survive.
    assert stored["server_editor"] == [900, 620]
    assert vault.read()["accent_colour"] == "#123456"

    assert ws.load_sizes(vault)["main"] == (1500, 950)
    assert ws.get_size("main", vault) == (1500, 950)


def test_save_clamps_and_ignores_unknown_keys():
    vault = FakeVault()
    ws.save_sizes(vault, {"main": (5, 5), "nope": (1, 1)})
    assert vault.read()[ws.VAULT_KEY]["main"] == [900, 620]
    assert "nope" not in vault.read()[ws.VAULT_KEY]


def test_reads_fall_back_to_defaults_when_the_vault_breaks():
    assert ws.load_sizes(BrokenVault()) == ws.default_sizes()
    assert ws.get_size("main", BrokenVault()) == (1280, 820)
    # A corrupt stored blob must not break the read either.
    corrupt = FakeVault({ws.VAULT_KEY: "garbage"})
    assert ws.load_sizes(corrupt) == ws.default_sizes()


def test_get_size_rejects_unknown_keys():
    try:
        ws.get_size("nope", FakeVault())
    except KeyError:
        return
    raise AssertionError("unknown window key should raise KeyError")


# --------------------------------------------------------- the settings UI --
def test_settings_page_shows_the_configured_sizes():
    vault = FakeVault({ws.VAULT_KEY: {"main": [1500, 950]}})
    page, _host = _page(vault)
    assert page._size_spins.keys() == {spec.key for spec in ws.WINDOW_SPECS}
    w_spin, h_spin = page._size_spins["main"]
    assert (w_spin.value(), h_spin.value()) == (1500, 950)
    # Untouched windows show their defaults.
    w2, h2 = page._size_spins["server_editor"]
    assert (w2.value(), h2.value()) == (900, 620)
    # The section is actually present on the page.
    from qfluentwidgets import SubtitleLabel

    titles = [w.text() for w in page.findChildren(SubtitleLabel)]
    assert "Window Sizes" in titles
    assert "Settings" in titles


def test_apply_sizes_persists_and_resizes_the_main_window():
    vault = FakeVault()
    page, host = _page(vault)
    page._size_spins["main"][0].setValue(1400)
    page._size_spins["main"][1].setValue(880)

    page.size_apply_btn.click()

    assert vault.read()[ws.VAULT_KEY]["main"] == [1400, 880]
    assert host.applied is not None
    assert host.applied["main"] == (1400, 880)
    # The feedback path ran too (log + toast).
    assert any("Window sizes applied" in str(entry) for entry in _LOGS)


def test_apply_sizes_clamps_before_storing():
    vault = FakeVault()
    page, host = _page(vault)
    # The spin boxes stop at their range minimum, and _current_sizes clamps
    # again on the way out: nothing below the documented minimums can be saved.
    w_spin, _ = page._size_spins["instance_editor"]
    w_spin.setValue(w_spin.minimum())
    page.size_apply_btn.click()

    assert vault.read()[ws.VAULT_KEY]["instance_editor"][0] >= 760
    assert host.applied["instance_editor"][0] >= 760


def test_reset_sizes_restores_the_defaults_in_the_boxes():
    vault = FakeVault({ws.VAULT_KEY: {"main": [1500, 950]}})
    page, host = _page(vault)
    assert page._size_spins["main"][0].value() == 1500
    before = vault.read()

    page.size_reset_btn.click()

    for key, (w, h) in ws.default_sizes().items():
        w_spin, h_spin = page._size_spins[key]
        assert (w_spin.value(), h_spin.value()) == (w, h), key
    # Reset only fills the boxes: nothing is stored or applied yet.
    assert host.applied is None
    assert vault.read() == before


def test_the_main_save_button_persists_the_sizes_too():
    vault = FakeVault()
    page, host = _page(vault)
    page._size_spins["main"][0].setValue(1300)

    page._on_save()

    assert vault.read()[ws.VAULT_KEY]["main"] == [1300, 820]
    assert host.applied is None  # plain Save does not resize live


# ------------------------------------------------------------- the editors --
def test_editors_use_their_configured_size():
    """Both editors must ask the shared module, not hard-code 900x620."""
    from launcher import window_sizes

    fake = {
        "instance_editor": (1111, 777),
        "server_editor": (1234, 654),
        "main": (1280, 820),
    }
    original = window_sizes.get_size
    window_sizes.get_size = lambda key, vault=None: fake[key]
    try:
        from launcher.instance_editor import InstanceEditorWindow
        from launcher.instances import Instance, InstanceRegistry
        from launcher.server_editor import ServerEditorWindow
        from launcher.servers import ServerRegistry

        root = _temp_dir()
        instance = Instance(name="Size Test", mc_version="1.20.1")
        registry = InstanceRegistry(
            index_path=os.path.join(root, "instances.json"))
        editor = InstanceEditorWindow(instance, registry)
        _ALIVE.append(editor)
        assert (editor.width(), editor.height()) == fake["instance_editor"], (
            f"instance editor was {editor.width()}x{editor.height()}"
        )

        s_registry = ServerRegistry(
            index_path=os.path.join(root, "servers.json"),
            root=os.path.join(root, "servers"),
        )
        server = s_registry.create("SizeTest")
        s_editor = ServerEditorWindow(server, s_registry)
        _ALIVE.append(s_editor)
        assert (s_editor.width(), s_editor.height()) == fake["server_editor"], (
            f"server editor was {s_editor.width()}x{s_editor.height()}"
        )
    finally:
        window_sizes.get_size = original


# ---------------------------------------------------------- the main window --
def test_main_window_boots_at_the_configured_size():
    """The boot size (resize + hard-cap minimum) comes from the vault."""
    from PyQt6.QtCore import QThread, pyqtSignal

    import launcher.constants as C
    import launcher.pages.skins_page as skins
    import launcher.window as winmod
    from launcher.instances import InstanceRegistry

    # Neutralise the two network threads LauncherWindow starts on construction
    # (same approach as test_freeze_fixes.py).
    class _NoopVersionThread(QThread):
        ready = pyqtSignal(list)
        failed = pyqtSignal(str)

        def run(self):
            return

    class _NoopMsProfileThread(QThread):
        ready = pyqtSignal(dict)
        failed = pyqtSignal(str)

        def __init__(self, *args, **kwargs):
            super().__init__()

        def run(self):
            return

    winmod.VersionListThread = _NoopVersionThread
    skins._MsProfileThread = _NoopMsProfileThread

    class _VaultReturning:
        """Answers reads with our seeded window_sizes and keeps writes."""

        def __init__(self, data):
            self._data = dict(data)

        def read(self):
            return dict(self._data)

        def write(self, data):
            self._data = dict(data)

    root = _temp_dir()
    old_index, old_dir = C.INSTANCE_INDEX_PATH, C.INSTANCES_DIR
    C.INSTANCE_INDEX_PATH = os.path.join(root, "instances.json")
    C.INSTANCES_DIR = os.path.join(root, "instances")
    original_vault = ws.Vault
    ws.Vault = lambda *a, **k: _VaultReturning(
        {ws.VAULT_KEY: {"main": [1400, 880]}}
    )
    try:
        win = winmod.LauncherWindow()
        _ALIVE.append(win)
        # InstanceRegistry binds its path as a default argument at import time.
        win.registry = InstanceRegistry(C.INSTANCE_INDEX_PATH)
        win.show()
        _pump()

        assert (win.width(), win.height()) == (1400, 880), (
            f"boot size was {win.width()}x{win.height()}, expected 1400x880"
        )
        # The hard cap must match, or a page could still inflate the window.
        assert (win.minimumWidth(), win.minimumHeight()) == (1400, 880)

        area = APP.primaryScreen().availableGeometry()

        def expected(w: int, h: int, min_w: int = 900,
                     min_h: int = 620) -> tuple[int, int]:
            """Mirror apply_window_sizes: clamp to limits, then fit to screen."""
            return (min(max(w, min_w), area.width()),
                    min(max(h, min_h), area.height()))

        # Applying new sizes resizes the open window immediately.
        win.apply_window_sizes({"main": (1000, 700)})
        _pump()
        assert (win.width(), win.height()) == expected(1000, 700)
        assert (win.minimumWidth(), win.minimumHeight()) == expected(1000, 700)
        assert win.width() <= area.width() and win.height() <= area.height()

        # Sub-minimum sizes come back up to the documented floor.
        win.apply_window_sizes({"main": (1, 1)})
        _pump()
        assert (win.width(), win.height()) == expected(1, 1)
        assert (win.minimumWidth(), win.minimumHeight()) == expected(1, 1)
        assert win.width() >= min(900, area.width())
    finally:
        ws.Vault = original_vault
        C.INSTANCE_INDEX_PATH, C.INSTANCES_DIR = old_index, old_dir


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



