"""Custom JVM arguments: they survive Save, and ZGC asks the right JDK.

Run: python -u test_jvm_args.py

Two halves of the same complaint -- "I type a java argument, save, and it
clears without saving".

The box was split with ``shlex.split``, which on Windows eats the backslashes
out of every path (``-Djava.library.path=C:\\mods\\lib`` came back as
``C:modslib``), throws away quoting, and raises on a trailing backslash or an
unclosed quote -- out of a Qt slot, where the exception takes the app with it
instead of saving. So the round trip is pinned here end to end: type something
awkward into the real editor, press the real Save, read the real index file,
reopen the real editor and compare.

The ZGC switch is the other half: which flags enable a low-pause collector is
a property of the JDK, so the flags are chosen from the Java that will run the
game rather than typed once and hoped for.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThread, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

import launcher.instances as instances_mod  # noqa: E402
from launcher import jvm_args  # noqa: E402
from launcher.instance_editor import InstanceEditorWindow  # noqa: E402
from launcher.instances import (  # noqa: E402
    Instance,
    InstanceRegistry,
    build_jvm_arguments,
)
from launcher.jvm_args import (  # noqa: E402
    java_major,
    join,
    launch_java_major,
    major_from_version_json,
    requested_collector,
    split,
    zgc_flags,
)

# The registry resolves instance folders through this module global, so point
# it into the temp tree before anything creates an instance.
_ROOT = tempfile.mkdtemp(prefix="cove_jvm_args_")
instances_mod.INSTANCES_DIR = os.path.join(_ROOT, "instances")

_ALIVE: list = []
_CASES = itertools.count()


# ------------------------------------------------------------------- helpers --
def _case(name: str = "Args") -> tuple[InstanceRegistry, Instance, str]:
    """A throwaway registry holding one instance, plus its index path."""
    root = os.path.join(_ROOT, f"case{next(_CASES)}")
    os.makedirs(root, exist_ok=True)
    index = os.path.join(root, "instances.json")
    registry = InstanceRegistry(index_path=index)
    instance = registry.create(name, "1.21.1")
    assert instance.dir.startswith(_ROOT), (
        f"an instance escaped the temp tree: {instance.dir}")
    return registry, instance, index


def _editor(registry: InstanceRegistry, instance: Instance):
    editor = InstanceEditorWindow(instance, registry)
    _ALIVE.append(editor)
    return editor


def _fake_jdk(name: str, version: str, implementor: str = "Test") -> str:
    """A JDK folder with a release file, returning its ``bin/java.exe`` path."""
    home = os.path.join(_ROOT, "jdks", name)
    os.makedirs(os.path.join(home, "bin"), exist_ok=True)
    with open(os.path.join(home, "release"), "w", encoding="utf-8") as handle:
        handle.write(f'IMPLEMENTOR="{implementor}"\nJAVA_VERSION="{version}"\n')
    java_exe = os.path.join(home, "bin", "java.exe")
    with open(java_exe, "w", encoding="utf-8") as handle:
        handle.write("")
    return java_exe


def _find_java() -> str | None:
    """A real JDK on this machine to check composed flags against, if any."""
    found = shutil.which("java")
    if found:
        return found
    from launcher.constants import APP_DATA_DIR

    root = os.path.join(APP_DATA_DIR, "jdks")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        for exe in ("bin/java.exe", "bin/java"):
            path = os.path.join(root, name, exe)
            if os.path.isfile(path):
                return path
    return None


def _saved(index: str, name: str = "Args") -> dict:
    """The instance as it actually sits in the index file."""
    with open(index, encoding="utf-8") as handle:
        for row in json.load(handle):
            if row.get("name") == name:
                return row
    raise AssertionError(f"'{name}' is not in {index}")


# ------------------------------------------------- what the user typed, kept --
def test_split_keeps_windows_backslashes():
    """A backslash is a character, not an escape -- the whole bug in one line."""
    text = r"-Djava.library.path=C:\mods\lib -javaagent:C:\agents\a.jar"
    assert split(text) == [
        r"-Djava.library.path=C:\mods\lib",
        r"-javaagent:C:\agents\a.jar",
    ], split(text)


def test_split_groups_quoted_values_and_drops_the_quotes():
    """Quotes hold a value with spaces together; the JVM never sees them."""
    assert split('"-Dname=my launcher" -XX:+UseG1GC') == [
        "-Dname=my launcher",
        "-XX:+UseG1GC",
    ]
    assert split("-XX:OnError=\"echo hi\"") == ["-XX:OnError=echo hi"]
    assert split("'-Dname=my launcher'") == ["-Dname=my launcher"]


def test_split_survives_input_that_used_to_raise():
    """A trailing backslash or an unclosed quote used to abort the save."""
    assert split("-Ddir=C:\\mods\\") == ["-Ddir=C:\\mods\\"]
    assert split('-Dname="my launcher') == ["-Dname=my launcher"]
    assert split('-Dodd="') == ["-Dodd="]
    assert split("   ") == []
    assert split("") == []


def test_split_separates_on_any_whitespace():
    assert split("-Dx=1\t-Dy=2\n-XX:+UseG1GC") == [
        "-Dx=1", "-Dy=2", "-XX:+UseG1GC",
    ]


def test_join_round_trips_arguments_containing_spaces():
    """Save -> reopen -> save must not quietly split one argument in two."""
    args = [r"-Djava.library.path=C:\mods\lib", "-Dname=my launcher",
            "-XX:+UseG1GC"]
    text = join(args)
    assert text == r'-Djava.library.path=C:\mods\lib "-Dname=my launcher" -XX:+UseG1GC'
    assert split(text) == args
    assert join(split(text)) == text


# ------------------------------------------------------------- the ZGC flags --
def test_zgc_flags_follow_the_jdk():
    """The flags are the JDK's, so an unsupported one is never added."""
    assert zgc_flags(8) == [], "Java 8 has no ZGC to enable"
    assert zgc_flags(11) == ["-XX:+UnlockExperimentalVMOptions", "-XX:+UseZGC"]
    assert zgc_flags(14) == ["-XX:+UnlockExperimentalVMOptions", "-XX:+UseZGC"]
    assert zgc_flags(15) == ["-XX:+UseZGC"]
    assert zgc_flags(17) == ["-XX:+UseZGC"]
    for generational in (21, 22, 23):
        assert zgc_flags(generational) == ["-XX:+UseZGC", "-XX:+ZGenerational"], (
            f"Java {generational} still needs the flag to go generational")
    for modern in (24, 25):
        assert zgc_flags(modern) == ["-XX:+UseZGC"], (
            f"Java {modern} is generational by default and ignores the flag")
    assert zgc_flags(None) == ["-XX:+UseZGC", "-XX:+ZGenerational"], (
        "an unknown runtime is assumed modern, like the rest of the launcher")


def test_java_major_reads_the_release_file():
    assert java_major(_fake_jdk("jdk21", "21.0.4")) == 21
    assert java_major(_fake_jdk("jdk8", "1.8.0_402")) == 8, "1.8 means Java 8"
    assert java_major(None) is None
    assert java_major(os.path.join(_ROOT, "nope", "bin", "java.exe")) is None


def test_java_major_falls_back_to_a_version_named_folder():
    """Repackaged runtimes sometimes ship without a release file."""
    home = os.path.join(_ROOT, "jdks", "graalvm-java17", "bin")
    os.makedirs(home, exist_ok=True)
    assert java_major(os.path.join(home, "java.exe")) == 17


def test_major_from_version_json_reads_the_declared_runtime():
    home = os.path.join(_ROOT, "declared", "versions", "1.21.1")
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "1.21.1.json"), "w", encoding="utf-8") as handle:
        json.dump({"javaVersion": {"majorVersion": 21}}, handle)
    assert major_from_version_json(os.path.join(_ROOT, "declared"), "1.21.1") == 21
    assert major_from_version_json(os.path.join(_ROOT, "declared"), "9.9.9") is None
    assert major_from_version_json(None, None) is None


def test_launch_java_major_prefers_the_forced_jvm():
    instance = Instance(name="Declared", mc_version="1.21.1")
    forced = _fake_jdk("forced25", "25")
    assert launch_java_major(instance, forced) == 25
    assert launch_java_major(instance) is None, "nothing installed, nothing known"
    instance.java_path = _fake_jdk("own21", "21.0.1")
    assert launch_java_major(instance) == 21


def test_build_jvm_arguments_adds_zgc_only_when_asked():
    plain = Instance(name="Plain", mc_version="1.21.1")
    assert build_jvm_arguments(plain, 4096, 21) == ["-Xmx4096M", "-Xms4096M"]

    asked = Instance(name="Asked", mc_version="1.21.1", zgc_enabled=True)
    assert build_jvm_arguments(asked, 4096, 21) == [
        "-Xmx4096M", "-Xms4096M", "-XX:+UseZGC", "-XX:+ZGenerational",
    ]
    # The user's own line wins and is never duplicated.
    typed = Instance(name="Typed", mc_version="1.21.1", zgc_enabled=True,
                     extra_jvm_args=["-XX:+UseZGC"])
    assert build_jvm_arguments(typed, 4096, 21) == [
        "-Xmx4096M", "-Xms4096M", "-XX:+ZGenerational", "-XX:+UseZGC",
    ]
    # Java 8 has no ZGC, so the switch must not hand it a flag it would reject.
    old = Instance(name="Old", mc_version="1.7.10", zgc_enabled=True)
    assert build_jvm_arguments(old, 2048, 8) == ["-Xmx2048M", "-Xms2048M"]


# ------------------------------------------- the editor, doing what a user does --
def test_saving_awkward_arguments_keeps_them_in_the_index():
    """Type it, Save it, and read the index: nothing lost, nothing mangled."""
    registry, instance, index = _case()
    typed = (
        r'-Djava.library.path=C:\mods\lib "-Dname=my launcher" '
        "-XX:+UseG1GC -Dtrailing=C:\\mods\\"
    )
    editor = _editor(registry, instance)
    editor.jvm_args_edit.setText(typed)

    editor._apply_and_close()          # the real Save button's slot

    expected = split(typed)
    assert expected == [
        r"-Djava.library.path=C:\mods\lib",
        "-Dname=my launcher",
        "-XX:+UseG1GC",
        "-Dtrailing=C:\\mods\\",
    ], expected
    assert _saved(index)["extra_jvm_args"] == expected, _saved(index)
    assert registry.get("Args").extra_jvm_args == expected


def test_reopening_the_editor_shows_what_was_saved():
    """The box must come back with the arguments, not empty."""
    registry, instance, index = _case()
    typed = '"-Dname=my launcher" -XX:+UseG1GC'
    editor = _editor(registry, instance)
    editor.jvm_args_edit.setText(typed)
    editor._apply_and_close()

    reopened = _editor(registry, registry.get("Args"))
    assert reopened.jvm_args_edit.text() == join(split(typed)), (
        reopened.jvm_args_edit.text())

    # Saving the reopened editor unchanged must be a no-op, not a slow drift.
    reopened._apply_and_close()
    assert _saved(index)["extra_jvm_args"] == split(typed), _saved(index)


def test_editor_saves_the_zgc_switch_and_launches_with_its_flags():
    registry, instance, index = _case()
    java_exe = _fake_jdk("jdk21", "21.0.4")
    editor = _editor(registry, instance)
    editor.java_path_edit.setText(java_exe)      # repaints the hint
    assert "-XX:+UseZGC -XX:+ZGenerational" in editor.zgc_hint.text(), (
        editor.zgc_hint.text())
    editor.zgc_check.setChecked(True)

    editor._apply_and_close()

    assert _saved(index)["zgc_enabled"] is True, _saved(index)
    saved = registry.get("Args")
    assert saved.zgc_enabled is True and saved.java_path == java_exe
    launch = build_jvm_arguments(saved, 8192, launch_java_major(saved))
    assert "-XX:+UseZGC" in launch and "-XX:+ZGenerational" in launch, launch

    reopened = _editor(registry, saved)
    assert reopened.zgc_check.isChecked() is True, "the switch does not stick"


def test_zgc_hint_admits_when_the_java_is_too_old():
    registry, instance, _ = _case()
    instance.java_path = _fake_jdk("jdk8", "1.8.0_402")
    editor = _editor(registry, instance)
    assert "adds nothing" in editor.zgc_hint.text(), editor.zgc_hint.text()
    assert zgc_flags(java_major(instance.java_path)) == []


def test_zgc_hint_uses_the_modern_wording_when_nothing_is_pinned():
    registry, instance, _ = _case()
    editor = _editor(registry, instance)
    assert editor.zgc_hint.text() == (
        "Adds -XX:+UseZGC -XX:+ZGenerational to every launch "
        "(the selected Java)."
    ), editor.zgc_hint.text()


class _RecordingLaunch(QThread):
    """Stands in for LaunchThread and remembers what it was handed."""

    game_output = pyqtSignal(str)
    game_started = pyqtSignal(list)
    game_exited = pyqtSignal(int)
    status = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__()
        self.kwargs = dict(kwargs)
        self._java_override = None

    def start(self):        # never spawn anything
        return


class _NullVault:
    """A vault nothing has been written to, like the launcher's own default."""

    def read(self) -> dict:
        return {}


def _launch_extra_args(registry, instance, java_version: str) -> tuple[list, str]:
    """Run the real _start_game on a stub window; return the JVM args it used.

    Only the parts of a window that _start_game touches are stubbed, so the
    path from the switch to the command line is the real one -- including the
    JDK lookup that decides which ZGC flags the launch gets.
    """
    import launcher.window as winmod

    java_exe = _fake_jdk(f"launch{java_version}", java_version)
    instance.java_path = java_exe

    class _Stub:
        pass

    win = _Stub()
    win.registry = registry
    win.accounts = None
    win._vault = _NullVault()
    win.log = lambda message, level="INFO": None
    win.system_java = lambda: None
    page = _Stub()
    page.status_label = _Stub()
    page.status_label.setText = lambda *_: None
    page.progress_bar = _Stub()
    page.progress_bar.setValue = lambda *_: None
    page.progress_bar.maximum = lambda: 100
    win.instances_page = page
    # The launch thread never emits here (its start() does nothing), so the
    # handlers _start_game wires up only have to exist.
    for slot in ("_on_game_started", "_on_game_output", "_on_game_exited",
                 "_on_worker_failed", "_clear_worker"):
        setattr(win, slot, lambda *a, **k: None)

    original = winmod.LaunchThread
    winmod.LaunchThread = _RecordingLaunch
    try:
        winmod.LauncherWindow._start_game(win, instance.name, "1.21.1")
    finally:
        winmod.LaunchThread = original
    return win._launcher.kwargs["extra_jvm_args"], java_exe


def test_the_switch_reaches_the_real_launch_command():
    """The switch has to arrive on the JVM's command line, not just in the box."""
    registry, instance, _ = _case("Launched")
    instance.zgc_enabled = True
    registry.update(instance)

    extra, java_exe = _launch_extra_args(registry, instance, "21.0.4")

    assert extra == ["-XX:+UseZGC", "-XX:+ZGenerational"], extra
    assert instance.java_path == java_exe


def test_the_launched_flags_follow_the_java_that_will_run():
    """Java 25 ignores -XX:+ZGenerational, so the launch must not send it."""
    registry, instance, _ = _case("Modern")
    instance.zgc_enabled = True
    registry.update(instance)

    extra, _ = _launch_extra_args(registry, instance, "25")

    assert extra == ["-XX:+UseZGC"], extra


def test_requested_collector_reads_the_typed_flags():
    assert requested_collector(["-XX:+UseZGC"]) == "z"
    assert requested_collector(["-Dx=1", "-XX:+UseG1GC"]) == "g1"
    assert requested_collector(["-XX:+UseShenandoahGC"]) == "shenandoah"
    assert requested_collector(["-XX:-UseG1GC"]) is None, "that switches it off"
    assert requested_collector(["-Dx=1"]) is None
    assert requested_collector([]) is None


def test_the_graalvm_preset_steps_aside_for_another_collector():
    """G1 tuning under a ZGC launch is a startup failure, not a spare flag."""
    graal = _fake_jdk("graalvm-java21", "21.0.4", "GraalVM Community")
    plain = Instance(name="Graal", mc_version="1.21.1", java_path=graal)
    preset = build_jvm_arguments(plain, 8192, 21)
    assert "-XX:+UseG1GC" in preset and "-XX:+UseJVMCICompiler" in preset, preset

    asking = Instance(name="ZgcGraal", mc_version="1.21.1", java_path=graal,
                      zgc_enabled=True)
    flags = build_jvm_arguments(asking, 8192, 21)
    assert "-XX:+UseG1GC" not in flags, flags
    assert not any(f.startswith("-XX:G1") for f in flags), flags
    assert "-XX:+UseZGC" in flags and "-XX:+ZGenerational" in flags, flags
    assert "-XX:+UseJVMCICompiler" in flags, (
        "the compiler and code-cache flags are not collector-bound")

    typed = Instance(name="TypedGraal", mc_version="1.21.1", java_path=graal,
                     extra_jvm_args=["-XX:+UseShenandoahGC"])
    flags = build_jvm_arguments(typed, 8192, 21)
    assert "-XX:+UseG1GC" not in flags, flags
    assert "-XX:+UseShenandoahGC" in flags, flags


def test_two_collectors_stop_a_real_jvm():
    """Why the preset has to step aside: HotSpot refuses to start.

    Checked on this machine's own JDK when there is one (15+, where UseZGC is
    a product flag); skipped otherwise.
    """
    java = _find_java()
    major = java_major(java) if java else None
    if java is None or major is None or major < 15:
        print("      (skipped: no JDK 15+ to check against)")
        return
    proc = subprocess.run(                   # noqa: S603 - fixed args
        [java, "-XX:+UseG1GC", "-XX:+UseZGC", "-version"],
        capture_output=True, text=True, timeout=90,
    )
    assert proc.returncode != 0 or "Multiple garbage collectors" in proc.stderr, (
        "two collector flags started cleanly -- the G1 preset would no longer "
        "need to step aside")


def test_the_zgc_flags_start_a_real_jvm():
    """The composed ZGC flags have to be accepted by the JDK they target."""
    java = _find_java()
    major = java_major(java) if java else None
    if java is None or major is None or major < 15:
        print("      (skipped: no JDK 15+ to check against)")
        return
    flags = zgc_flags(major)
    proc = subprocess.run(                   # noqa: S603 - fixed args
        [java, *flags, "-Xlog:gc+init=info", "-version"],
        capture_output=True, text=True, timeout=90,
    )
    text = proc.stderr + proc.stdout
    assert proc.returncode == 0, text
    assert "Z Garbage Collector" in text, text


def test_the_server_editor_saves_awkward_arguments_too():
    """The same box exists for servers and was split the same broken way."""
    from launcher.server_editor import ServerEditorWindow
    from launcher.servers import ServerRegistry

    root = os.path.join(_ROOT, f"case{next(_CASES)}")
    registry = ServerRegistry(index_path=os.path.join(root, "servers.json"),
                              root=os.path.join(root, "servers"))
    server = registry.create("Args")
    editor = ServerEditorWindow(server, registry)
    _ALIVE.append(editor)
    editor.jvm_args_edit.setText(
        r"-Djava.library.path=C:\mods\lib -XX:+UseG1GC")
    editor._on_save_settings()

    assert registry.get("Args").extra_jvm_args == [
        r"-Djava.library.path=C:\mods\lib", "-XX:+UseG1GC",
    ], registry.get("Args").extra_jvm_args


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
        shutil.rmtree(_ROOT, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
