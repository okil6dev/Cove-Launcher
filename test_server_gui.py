"""Constructs the Server page widgets for real.

Run: python -m pytest test_server_gui.py

The pure server model tests (test_servers.py) cannot catch a wrong qfluent
call or a layout mistake, so this file builds ServerPage against a temp
registry and asserts the list/info/badge/control wiring.
"""

from __future__ import annotations

import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

APP = QApplication.instance() or QApplication([])

from launcher.servers import ServerRegistry  # noqa: E402
from launcher.pages.server_page import ServerPage  # noqa: E402

_ALIVE: list = []


def _host() -> QWidget:
    w = QWidget()
    w.resize(1100, 700)
    _ALIVE.append(w)
    return w


def _page(tmp_path) -> ServerPage:
    registry = ServerRegistry(
        index_path=str(tmp_path / "servers.json"),
        root=str(tmp_path / "servers"),
    )
    page = ServerPage(
        registry=registry,
        log=lambda *a, **k: None,
        toast=lambda *a, **k: None,
        parent=_host(),
    )
    _ALIVE.append(page)
    return page


def _jar(path, title: str, version_id: str) -> str:
    with zipfile.ZipFile(str(path), "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF",
                    f"Manifest-Version: 1.0\nImplementation-Title: {title}\n")
        zf.writestr("version.json", f'{{"id": "{version_id}"}}')
    return str(path)


def test_empty_state_disables_everything(tmp_path):
    page = _page(tmp_path)
    assert page.count_label.text() == "0"
    assert page.start_button.isEnabled() is False
    assert page.current_server() is None
    assert "no servers" in page.empty_label.text().lower()


def test_create_and_select_shows_badges(tmp_path):
    page = _page(tmp_path)
    server = page._registry.create("My Server")
    src = _jar(tmp_path / "paper-1.21.1.jar", "Paper", "paper-1.21.1")
    page._registry.import_jar(server, src)
    page.refresh()
    assert page.list_widget.count() == 1
    assert page.current_server() is not None
    assert page.current_server().name == "My Server"
    # Paper: plugins yes, mods no, not dual. isVisibleTo() reflects the
    # setVisible() calls without needing the whole window to be shown.
    assert page.plugins_badge.isVisibleTo(page) is True
    assert page.mods_badge.isVisibleTo(page) is False
    assert page.dual_badge.isVisibleTo(page) is False
    # A jar is present and nothing is running -> Start is available.
    assert page.start_button.isEnabled() is True
    assert page.stop_button.isEnabled() is False
    assert page.input.isEnabled() is False
    assert "paper" in page.meta_label.text().lower()


def test_dual_loader_server_shows_both_badges(tmp_path):
    page = _page(tmp_path)
    server = page._registry.create("Hybrid")
    src = _jar(tmp_path / "mohist-1.20.1.jar", "Mohist", "mohist-1.20.1")
    page._registry.import_jar(server, src)
    page.refresh()
    assert page.mods_badge.isVisibleTo(page) is True
    assert page.plugins_badge.isVisibleTo(page) is True
    assert page.dual_badge.isVisibleTo(page) is True
    assert "1.20.1" in page.meta_label.text()


def test_server_without_jar_cannot_start(tmp_path):
    page = _page(tmp_path)
    page._registry.create("Bare")
    page.refresh()
    assert page.current_server() is not None
    assert page.start_button.isEnabled() is False
    assert "no jar" in page.meta_label.text().lower()


def test_eula_already_true_skips_the_prompt(tmp_path):
    page = _page(tmp_path)
    server = page._registry.create("EulaBox")
    with open(os.path.join(server.dir, "eula.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("eula=true\n")
    # Must return True WITHOUT opening a dialog (which would hang tests).
    assert page._eula_accepted(server) is True


def test_edit_button_opens_editor_for_selection(tmp_path):
    page = _page(tmp_path)
    server = page._registry.create("Editable")
    page.refresh()
    assert page.edit_button.isEnabled() is True
    assert page.current_server() is not None
    assert page.current_server().name == "Editable"


def test_editor_settings_save_round_trips(tmp_path):
    from launcher.server_editor import ServerEditorWindow

    registry = ServerRegistry(
        index_path=str(tmp_path / "servers.json"),
        root=str(tmp_path / "servers"),
    )
    server = registry.create("Cfg")
    ed = ServerEditorWindow(server, registry, parent=_host())
    _ALIVE.append(ed)
    ed.jvm_args_edit.setText("-XX:+UseG1GC")
    ed._on_save_settings()
    reloaded = registry.get("Cfg")
    assert reloaded.extra_jvm_args == ["-XX:+UseG1GC"]
    assert reloaded.ram_gb == ed._ram_value()


def test_editor_props_lists_keys_and_saves(tmp_path):
    from launcher.server_editor import ServerEditorWindow

    registry = ServerRegistry(
        index_path=str(tmp_path / "servers.json"),
        root=str(tmp_path / "servers"),
    )
    server = registry.create("Props")
    with open(os.path.join(server.dir, "server.properties"), "w",
              encoding="utf-8") as fh:
        fh.write("# hello\nmotd=Old\nmax-players=20\n")
    ed = ServerEditorWindow(server, registry, parent=_host())
    _ALIVE.append(ed)
    assert set(ed._prop_edits) == {"motd", "max-players"}
    ed._prop_edits["motd"].setText("New")
    ed._on_save_properties()
    with open(os.path.join(server.dir, "server.properties"),
              encoding="utf-8") as fh:
        text = fh.read()
    assert "motd=New" in text and "# hello" in text


def test_editor_props_empty_state_without_file(tmp_path):
    from launcher.server_editor import ServerEditorWindow

    registry = ServerRegistry(
        index_path=str(tmp_path / "servers.json"),
        root=str(tmp_path / "servers"),
    )
    server = registry.create("Bare2")
    ed = ServerEditorWindow(server, registry, parent=_host())
    _ALIVE.append(ed)
    assert ed._prop_edits == {}
    # Stacked nav pages are never "visible to" the window in tests, so check
    # the explicit hidden flag that _refresh_props sets.
    assert ed.props_hint.isHidden() is False
    assert ed.props_list.isHidden() is True


def test_editor_worlds_lists_and_files_gated_by_type(tmp_path):
    from launcher.server_editor import ServerEditorWindow

    registry = ServerRegistry(
        index_path=str(tmp_path / "servers.json"),
        root=str(tmp_path / "servers"),
    )
    server = registry.create("Lands")
    os.makedirs(os.path.join(server.dir, "world"))
    open(os.path.join(server.dir, "world", "level.dat"), "wb").close()
    # Unknown type: neither mods nor plugins sections. isHidden() checks the
    # explicit flag instead of isVisibleTo (stacked pages report False).
    ed = ServerEditorWindow(server, registry, parent=_host())
    _ALIVE.append(ed)
    assert ed.worlds_list.count() == 1
    assert ed.mods_section.isHidden() is True
    assert ed.plugins_section.isHidden() is True
    assert ed.files_hint.isHidden() is False
    # Flip to dual-loader: both sections appear.
    server.server_type = "mohist"
    ed.refresh_server(server, running=False)
    assert ed.mods_section.isHidden() is False
    assert ed.plugins_section.isHidden() is False


def test_editor_files_lists_jars(tmp_path):
    from launcher.server_editor import ServerEditorWindow

    registry = ServerRegistry(
        index_path=str(tmp_path / "servers.json"),
        root=str(tmp_path / "servers"),
    )
    server = registry.create("Modded")
    server.server_type = "paper"  # plugins-capable: plugins/ shows, mods/ hides
    open(os.path.join(server.dir, "plugins", "Essentials.jar"), "wb").close()
    ed = ServerEditorWindow(server, registry, parent=_host())
    _ALIVE.append(ed)
    assert ed.plugins_list.count() == 1
    assert ed.plugins_list.item(0).text() == "Essentials.jar"
    assert ed.mods_section.isHidden() is True


def test_resolve_java_prefers_server_path(tmp_path):
    page = _page(tmp_path)
    server = page._registry.create("JavaBox")
    server.java_path = "C:/tools/java.exe"
    assert page._resolve_java(server) == "C:/tools/java.exe"
    server.java_path = ""
    page._java_path = "cached-java"
    assert page._resolve_java(server) == "cached-java"


def _run_all() -> int:
    import tempfile
    import pathlib

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fn(pathlib.Path(tmp))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
