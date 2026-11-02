"""Global Minecraft options: what "Disable music" does to every instance.

Run: python -u test_minecraft_options.py

Guards the Settings -> Minecraft tab end to end: the vault is the source of
truth, each instance's own ``options.txt`` receives only the options the user
switched on (with the key that instance's Minecraft version actually reads),
nothing is touched while a switch is off, and the pre-launch hook re-applies it
all before the game starts.

Both kinds of option are covered: a plain switch (music) and a switch with a
number box, whose picked number is the value written (GUI scale).
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from launcher import minecraft_options as mo  # noqa: E402

_ALIVE: list = []

MODERN = "1.20.1"
MODERN_KEY = "soundCategory_music"
LEGACY = "1.6.4"
LEGACY_KEY = "music"


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


class FakeInstance:
    """Just enough of launcher.instances.Instance for this module."""

    def __init__(self, name: str, mc_version: str, root: str) -> None:
        self.name = name
        self.mc_version = mc_version
        self.dir = os.path.join(root, name)
        os.makedirs(self.dir, exist_ok=True)

    def options_file(self) -> str:
        return os.path.join(self.dir, "options.txt")

    def options_text(self) -> str:
        with open(self.options_file(), encoding="utf-8") as fh:
            return fh.read()

    def write_options(self, text: str) -> None:
        with open(self.options_file(), "w", encoding="utf-8") as fh:
            fh.write(text)

    def has_options(self) -> bool:
        return os.path.isfile(self.options_file())


def _root() -> str:
    path = tempfile.mkdtemp(prefix="cove_mcoptions_")
    _ALIVE.append(path)
    return path


def _instance(name="Test", version=MODERN, root=None) -> FakeInstance:
    return FakeInstance(name, version, root or _root())


def _on() -> dict:
    return {"disable_music": True}


def _gui(number) -> dict:
    """Choices with only the GUI scale managed (``None`` = leave it alone)."""
    return {"gui_scale": number}


# --------------------------------------------------------------- stored choice --
def test_everything_starts_off():
    assert mo.load(FakeVault()) == mo.DEFAULTS
    assert mo.DEFAULTS == {"disable_music": False, "gui_scale": None}
    assert all(not value for value in mo.DEFAULTS.values())
    # Off means "leave this alone", and the two kinds say it differently: a
    # toggle with False, a number option with None -- 0 is a real GUI scale
    # (Auto), so it cannot double as "unset".
    assert mo.DEFAULTS["gui_scale"] is None


def test_save_and_load_round_trip():
    vault = FakeVault({"accent_colour": "#123456"})
    mo.save(vault, _on())

    # Every option is stored, the unmanaged ones included: "not managed" is a
    # real state, not a missing key.
    assert vault.read()[mo.VAULT_KEY] == {**mo.DEFAULTS, "disable_music": True}
    assert mo.load(vault) == {**mo.DEFAULTS, "disable_music": True}
    # Other secrets survive, and only known keys are stored.
    assert vault.read()["accent_colour"] == "#123456"
    mo.save(vault, {"disable_music": False, "not_an_option": True})
    assert vault.read()[mo.VAULT_KEY] == mo.DEFAULTS
    assert "not_an_option" not in vault.read()[mo.VAULT_KEY]


def test_junk_in_the_vault_falls_back_to_the_defaults():
    for junk in (None, "nonsense", [1, 2], {"disable_music": "yes"}, 5):
        assert mo.load(FakeVault({mo.VAULT_KEY: junk})) == mo.DEFAULTS, junk
    # A string "yes" would be truthy and would silently mute music for good.
    assert mo.normalize({"disable_music": "yes"}) == mo.DEFAULTS


def test_a_locked_vault_reads_as_defaults():
    assert mo.load(BrokenVault()) == mo.DEFAULTS


# ------------------------------------------------------------------ options.txt --
def test_the_modern_key_is_written_for_modern_versions():
    instance = _instance(version=MODERN)
    assert mo.enforce(instance, _on()) == ["Disable music"]
    assert instance.options_text() == f"{MODERN_KEY}:0.0\n"


def test_the_legacy_key_is_written_only_before_1_7_2():
    for version in (LEGACY, "1.7.1"):
        instance = _instance(version=version)
        mo.enforce(instance, _on())
        assert instance.options_text().strip() == f"{LEGACY_KEY}:0.0", version
    for version in ("1.7.2", "1.7.10", "1.12.2", MODERN, "23w45a", ""):
        instance = _instance(version=version)
        mo.enforce(instance, _on())
        assert instance.options_text().strip() == f"{MODERN_KEY}:0.0", version


def test_the_rest_of_options_txt_is_left_alone():
    instance = _instance()
    instance.write_options(
        "fov:0.5\n"
        "resourcePacks:[\"file/my-pack.zip\"]\n"
        f"{MODERN_KEY}:0.4\n"
        "mouseSensitivity:0.6\n"
    )
    mo.enforce(instance, _on())

    text = instance.options_text()
    assert "fov:0.5" in text
    assert "resourcePacks:[\"file/my-pack.zip\"]" in text
    assert "mouseSensitivity:0.6" in text
    # The existing line is replaced in place of a duplicate.
    assert text.count(f"{MODERN_KEY}:") == 1
    assert f"{MODERN_KEY}:0.0" in text


def test_nothing_is_written_while_the_option_is_off():
    instance = _instance()
    instance.write_options("fov:0.5\nsoundCategory_music:0.4\n")

    assert mo.enforce(instance, {"disable_music": False}) == []
    # A slider set by hand in-game must survive untouched.
    assert instance.options_text() == "fov:0.5\nsoundCategory_music:0.4\n"


def test_a_missing_options_txt_is_left_absent_while_off_and_created_when_on():
    instance = _instance()
    mo.enforce(instance, {"disable_music": False})
    assert not instance.has_options(), "an option that is off must not create files"

    mo.enforce(instance, _on())
    assert instance.options_text() == f"{MODERN_KEY}:0.0\n"


def test_enforcing_twice_writes_nothing_the_second_time():
    instance = _instance()
    assert mo.enforce(instance, _on()) == ["Disable music"]
    assert mo.enforce(instance, _on()) == []
    assert instance.options_text() == f"{MODERN_KEY}:0.0\n"


def test_restore_puts_vanilla_default_back():
    instance = _instance()
    instance.write_options("fov:0.5\n")
    mo.enforce(instance, _on())

    assert mo.restore(instance, ["disable_music"]) == ["Disable music"]
    assert instance.options_text() == f"fov:0.5\n{MODERN_KEY}:1.0\n"
    # Unknown keys are ignored rather than raising.
    assert mo.restore(instance, ["no_such_option"]) == []


def test_an_unwritable_options_txt_is_reported_as_no_change():
    """A locked/directory path must not raise: this runs on the launch path."""
    instance = _instance()
    os.makedirs(instance.options_file())          # a directory where the file goes
    assert mo.enforce(instance, _on()) == []


# ---------------------------------------------------- GUI scale, number option --
def test_gui_scale_is_defined_as_a_number_option():
    opt = mo.option("gui_scale")
    assert opt.kind == "number"
    # One key for every version: guiScale has existed since the option file did.
    assert opt.txt_key("1.20.1") == "guiScale"
    assert opt.txt_key("1.6.4") == "guiScale"
    # Off writes Auto (0), which is what vanilla's own default is.
    assert opt.disabled_value == "0"
    assert opt.minimum == 1
    assert opt.maximum == mo.GUI_SCALE_MAX
    assert opt.minimum <= opt.initial <= opt.maximum
    assert opt.hint, "the section shows the option's hint under its description"


def test_the_gui_scale_is_written_into_options_txt():
    instance = _instance()
    assert mo.enforce(instance, _gui(2)) == ["GUI scale"]
    assert instance.options_text() == "guiScale:2\n"
    # Idempotent, exactly like the switch.
    assert mo.enforce(instance, _gui(2)) == []


def test_gui_scale_is_left_alone_while_its_switch_is_off():
    instance = _instance()
    instance.write_options("fov:0.5\nguiScale:3\n")

    assert mo.enforce(instance, _gui(None)) == []
    assert instance.options_text() == "fov:0.5\nguiScale:3\n"
    # A fresh instance stays untouched too, file and all.
    fresh = _instance()
    mo.enforce(fresh, _gui(None))
    assert not fresh.has_options()


def test_both_kinds_of_option_write_in_one_pass():
    instance = _instance()
    changed = mo.enforce(instance, {"disable_music": True, "gui_scale": 4})

    assert sorted(changed) == ["Disable music", "GUI scale"]
    text = instance.options_text()
    assert f"{MODERN_KEY}:0.0" in text
    assert "guiScale:4" in text
    assert len(text.strip().splitlines()) == 2


def test_turning_gui_scale_off_writes_auto_back():
    instance = _instance()
    mo.enforce(instance, _gui(5))

    assert mo.restore(instance, ["gui_scale"]) == ["GUI scale"]
    assert instance.options_text() == "guiScale:0\n"


def test_a_stored_number_survives_a_round_trip():
    vault = FakeVault()
    mo.save(vault, {"disable_music": False, "gui_scale": 3})

    assert vault.read()[mo.VAULT_KEY] == {"disable_music": False, "gui_scale": 3}
    assert mo.load(vault)["gui_scale"] == 3


def test_saving_none_stops_managing_a_number_option():
    vault = FakeVault({mo.VAULT_KEY: {"gui_scale": 4}})
    mo.save(vault, {"gui_scale": None})

    assert mo.load(vault)["gui_scale"] is None


def test_numbers_are_clamped_into_range_and_junk_is_refused():
    opt = mo.option("gui_scale")
    assert mo.coerce(opt, 2) == 2
    assert mo.coerce(opt, 0) == 1                 # below the minimum
    assert mo.coerce(opt, 99) == mo.GUI_SCALE_MAX  # above the maximum
    for junk in ("2", 2.0, True, False, None, [2]):
        assert mo.coerce(opt, junk) is None, junk

    # A hand-edited vault must not be able to smuggle in a managed setting.
    assert mo.normalize({"gui_scale": "2"})["gui_scale"] is None
    assert mo.normalize({"gui_scale": 2.5})["gui_scale"] is None
    assert mo.normalize({"gui_scale": True})["gui_scale"] is None
    assert mo.normalize({"gui_scale": 7})["gui_scale"] == 7
    assert mo.normalize({"gui_scale": 0})["gui_scale"] == 1


def test_sync_handles_both_kinds_together():
    instance = FakeInstance("Both", MODERN, _root())
    choices = {"disable_music": True, "gui_scale": 2}

    assert mo.sync([instance], choices) == ["Both"]
    text = instance.options_text()
    assert f"{MODERN_KEY}:0.0" in text and "guiScale:2" in text
    assert mo.sync([instance], choices) == []


# ------------------------------------------------------------------------ sync --
def test_sync_touches_every_instance_and_reports_the_changed_ones():
    root = _root()
    modern = FakeInstance("Modern", MODERN, root)
    legacy = FakeInstance("Legacy", LEGACY, root)
    already = FakeInstance("Done", MODERN, root)
    already.write_options(f"{MODERN_KEY}:0.0\n")

    changed = mo.sync([modern, legacy, already], _on())

    assert sorted(changed) == ["Legacy", "Modern"]
    assert already.options_text() == f"{MODERN_KEY}:0.0\n"     # untouched
    # Idempotent: a second pass has nothing left to do.
    assert mo.sync([modern, legacy, already], _on()) == []


def test_sync_with_no_instances_is_a_no_op():
    assert mo.sync([], _on()) == []
    assert mo.sync(None, _on()) == []


def test_switching_off_restores_through_sync():
    root = _root()
    instance = FakeInstance("Muted", MODERN, root)
    mo.sync([instance], _on())

    changed = mo.sync([instance], {"disable_music": False},
                      turn_off=["disable_music"])

    assert changed == ["Muted"]
    assert instance.options_text() == f"{MODERN_KEY}:1.0\n"


# ------------------------------------------------------------- pre-launch hook --
_APP = None
_WINDOW = None
_STUBBED = False
_TEMP_ROOT: str | None = None


def _temp_root() -> str:
    global _TEMP_ROOT
    if _TEMP_ROOT is None:
        _TEMP_ROOT = tempfile.mkdtemp(prefix="cove_mcoptions_")
    return _TEMP_ROOT


def _stub_threads() -> None:
    """Keep the window from touching the network or spawning a real game."""
    global _STUBBED
    if _STUBBED:
        return
    from PyQt6.QtCore import QThread, pyqtSignal

    import launcher.pages.skins_page as skins
    import launcher.window as winmod

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

    class _NoopSearchThread(QThread):
        """Stands in for ModSearchThread (the Mods page searches on build).

        Without this a real search thread is still running when the process
        exits, and PyQt turns "destroyed while running" into a hard abort
        (0xC0000409) -- which run_tests.py reads as this suite failing.
        """

        ready = pyqtSignal(object, list)
        failed = pyqtSignal(str)

        def __init__(self, *args, **kwargs):
            super().__init__()

        def run(self):
            return

    class _NoopLaunchThread(QThread):
        """Same signals as the real LaunchThread, but starts no process."""

        game_output = pyqtSignal(str)
        game_started = pyqtSignal(list)
        game_exited = pyqtSignal(int)
        status = pyqtSignal(str)
        failed = pyqtSignal(str)

        def __init__(self, *args, **kwargs):
            super().__init__()
            self._java_override = None

        def start(self):        # never spawn anything
            return

    import launcher.pages.mods_page as mods_page

    winmod.VersionListThread = _NoopVersionThread
    skins._MsProfileThread = _NoopMsProfileThread
    winmod.LaunchThread = _NoopLaunchThread
    mods_page.ModSearchThread = _NoopSearchThread
    _STUBBED = True


def _launch_window():
    """The real LauncherWindow, built once, wired entirely to throwaway paths.

    Both ``launcher.constants`` *and* ``launcher.instances`` have to be
    redirected: ``Instance.dir`` and the registry resolve the paths through the
    instances module's own bindings, so patching the constants alone still
    creates real directories under ``%APPDATA%/Cove/instances``.
    """
    global _APP, _WINDOW
    from PyQt6.QtWidgets import QApplication

    import launcher.constants as C
    import launcher.instances as instances_mod
    from launcher.instances import InstanceRegistry

    if QApplication.instance() is None:
        _APP = QApplication([])
    _stub_threads()

    root = _temp_root()
    C.INSTANCES_DIR = os.path.join(root, "instances")
    C.INSTANCE_INDEX_PATH = os.path.join(root, "instances.json")
    instances_mod.INSTANCES_DIR = C.INSTANCES_DIR
    instances_mod.SHARED_ASSETS_DIR = os.path.join(root, "shared-assets")

    from launcher.window import LauncherWindow

    if _WINDOW is None:
        _WINDOW = LauncherWindow()
    win = _WINDOW
    # InstanceRegistry binds its index_path as a default argument at import
    # time, so redirect it explicitly or the window reads the real list.
    win.registry = InstanceRegistry(C.INSTANCE_INDEX_PATH)
    win._splash = None
    win._launcher = None
    # Keep this run's log lines (including the one under test) off the user's
    # console.log: record them instead.
    win._test_log: list[str] = []
    win.log = lambda msg, level="INFO": win._test_log.append(str(msg))
    return win


def _seed_options(instance, *lines: str) -> None:
    """Write a few options.txt lines for the instance."""
    with open(os.path.join(instance.dir, "options.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_launching_reapplies_the_enabled_options():
    """A fresh instance (or a reset options.txt) must still obey Settings."""
    win = _launch_window()
    win._vault = FakeVault({mo.VAULT_KEY: {"disable_music": True}})

    instance = win.registry.create("Hook Test", MODERN)
    assert instance.dir.startswith(_temp_root()), (
        f"the test instance landed outside the temp tree: {instance.dir}")
    _seed_options(instance, "fov:0.5", f"{MODERN_KEY}:0.8")

    win._start_game("Hook Test", MODERN)

    with open(os.path.join(instance.dir, "options.txt"), encoding="utf-8") as fh:
        text = fh.read()
    assert f"{MODERN_KEY}:0.0" in text, text
    assert text.count(f"{MODERN_KEY}:") == 1
    assert "fov:0.5" in text, "launching must not rewrite the user's other options"
    # ...and the launch log says what it did.
    assert any("disable music" in line for line in win._test_log), win._test_log
    win._launcher = None


def test_launching_writes_nothing_when_the_option_is_off():
    win = _launch_window()
    win._vault = FakeVault()
    instance = win.registry.create("Hook Off", MODERN)

    win._start_game("Hook Off", MODERN)

    assert not os.path.exists(os.path.join(instance.dir, "options.txt"))
    win._launcher = None


def test_launching_applies_a_pinned_gui_scale():
    """A number option has to reach the instance through the same hook."""
    win = _launch_window()
    win._vault = FakeVault({mo.VAULT_KEY: {"gui_scale": 3}})
    instance = win.registry.create("Scale Hook", MODERN)
    _seed_options(instance, "fov:0.5", "guiScale:0")

    win._start_game("Scale Hook", MODERN)

    with open(os.path.join(instance.dir, "options.txt"), encoding="utf-8") as fh:
        text = fh.read()
    assert "guiScale:3" in text, text
    assert text.count("guiScale:") == 1, "the old line is replaced, not duplicated"
    assert "fov:0.5" in text
    assert any("gui scale" in line for line in win._test_log), win._test_log
    win._launcher = None


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
