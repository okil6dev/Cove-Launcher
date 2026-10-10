"""
Tests for the server model (launcher/servers.py): naming, folder layout,
index persistence and jar detection -- including dual-loader hybrids.

Run: python -m pytest test_servers.py
"""

from __future__ import annotations

import zipfile

import pytest

from launcher.servers import (
    ServerError,
    ServerRegistry,
    delete_server_file,
    detect_server_jar,
    list_server_files,
    list_worlds,
    read_properties,
    rename_world,
    validate_name,
    write_properties,
)


def _registry(tmp_path) -> ServerRegistry:
    return ServerRegistry(
        index_path=str(tmp_path / "servers.json"),
        root=str(tmp_path / "servers"),
    )


def _make_jar(path, manifest: str | None = None,
              version_json: str | None = None,
              extra_files: dict | None = None) -> str:
    """Build a synthetic server jar for detection tests."""
    with zipfile.ZipFile(str(path), "w") as zf:
        if manifest is not None:
            zf.writestr("META-INF/MANIFEST.MF", manifest)
        if version_json is not None:
            zf.writestr("version.json", version_json)
        for name, body in (extra_files or {}).items():
            zf.writestr(name, body)
    return str(path)


# --------------------------------------------------------------------------- names
def test_validate_name_matches_instance_rules():
    validate_name("My Server 1")
    validate_name("survival.local")
    with pytest.raises(ServerError):
        validate_name("")
    with pytest.raises(ServerError):
        validate_name("../escape")
    with pytest.raises(ServerError):
        validate_name("x" * 65)


# ------------------------------------------------------------------- persistence
def test_create_builds_folder_and_index(tmp_path):
    reg = _registry(tmp_path)
    server = reg.create("My Server")
    assert server.dir.endswith("My Server")
    for sub in ("mods", "plugins"):
        import os
        assert os.path.isdir(f"{server.dir}/{sub}")
    # Reload from disk: the index must round-trip with the temp root applied.
    reg2 = _registry(tmp_path)
    loaded = reg2.get("My Server")
    assert loaded is not None
    assert loaded.root == str(tmp_path / "servers")
    assert loaded.server_type == "unknown"


def test_duplicate_name_rejected(tmp_path):
    reg = _registry(tmp_path)
    reg.create("Dup")
    with pytest.raises(ServerError):
        reg.create("Dup")


def test_delete_only_touches_servers_root(tmp_path):
    reg = _registry(tmp_path)
    reg.create("Gone")
    reg.delete("Gone")
    assert reg.get("Gone") is None
    import os
    assert not os.path.isdir(f"{tmp_path}/servers/Gone")
    # Unknown names are a no-op, not an error.
    reg.delete("Never existed")


# --------------------------------------------------------------------- detection
def test_detect_paper(tmp_path):
    jar = _make_jar(
        tmp_path / "paperclip.jar",
        manifest="Manifest-Version: 1.0\nImplementation-Title: Paper\n",
        version_json='{"id": "paper-1.21.1", "mainClass": "io.papermc."}',
    )
    info = detect_server_jar(jar)
    assert info.server_type == "paper"
    assert info.mc_version == "1.21.1"
    assert info.supports_plugins and not info.supports_mods
    assert not info.is_dual


def test_detect_vanilla(tmp_path):
    jar = _make_jar(
        tmp_path / "server.jar",
        manifest="Manifest-Version: 1.0\nMain-Class: net.minecraft.server.Main\n",
        version_json='{"id": "1.20.4"}',
    )
    info = detect_server_jar(jar)
    assert info.server_type == "vanilla"
    assert info.mc_version == "1.20.4"
    # Vanilla runs neither mods nor plugins -- no loader, no Bukkit API.
    assert not info.supports_mods and not info.supports_plugins
    assert not info.is_dual


def test_detect_fabric_by_content_and_filename(tmp_path):
    jar = _make_jar(
        tmp_path / "fabric-server-1.20.1.jar",
        extra_files={"fabric-server-launch.properties": "loader.version=0.15\n"},
    )
    info = detect_server_jar(jar)
    assert info.server_type == "fabric"
    assert info.mc_version == "1.20.1"
    assert info.supports_mods and not info.supports_plugins


def test_detect_mohist_is_dual(tmp_path):
    # Hybrid marker must win over the embedded "forge"/version clues.
    jar = _make_jar(
        tmp_path / "mohist-1.20.1.jar",
        manifest="Manifest-Version: 1.0\nImplementation-Title: Mohist\n",
        version_json='{"id": "mohist-1.20.1"}',
    )
    info = detect_server_jar(jar)
    assert info.server_type == "mohist"
    assert info.mc_version == "1.20.1"
    assert info.supports_mods and info.supports_plugins
    assert info.is_dual


def test_detect_cardboard_is_dual(tmp_path):
    jar = _make_jar(
        tmp_path / "cardboard.jar",
        manifest="Manifest-Version: 1.0\nImplementation-Title: Cardboard\n",
        version_json='{"id": "cardboard-1.20.4"}',
    )
    info = detect_server_jar(jar)
    assert info.server_type == "cardboard"
    assert info.is_dual


def test_detect_spigot(tmp_path):
    jar = _make_jar(
        tmp_path / "spigot-1.21.jar",
        manifest="Manifest-Version: 1.0\nImplementation-Title: CraftBukkit\n",
    )
    info = detect_server_jar(jar)
    assert info.server_type == "spigot"
    assert info.mc_version == "1.21"
    assert info.supports_plugins and not info.supports_mods


def test_unknown_jar_claims_nothing(tmp_path):
    # Not a zip at all: filename fallback finds no known marker either.
    path = tmp_path / "weird.jar"
    path.write_bytes(b"not a zip")
    info = detect_server_jar(str(path))
    assert info.server_type == "unknown"
    assert not info.supports_mods and not info.supports_plugins
    assert not info.is_dual


def test_corrupt_jar_falls_back_to_filename(tmp_path):
    path = tmp_path / "purpur-1.20.6.jar"
    path.write_bytes(b"garbage bytes")
    info = detect_server_jar(str(path))
    assert info.server_type == "purpur"
    assert info.mc_version == "1.20.6"


# ---------------------------------------------------------------------- import
def test_import_jar_copies_detects_and_persists(tmp_path):
    reg = _registry(tmp_path)
    server = reg.create("Hybrid")
    src = _make_jar(
        tmp_path / "outside-mohist-1.20.1.jar",
        manifest="Manifest-Version: 1.0\nImplementation-Title: Mohist\n",
        version_json='{"id": "mohist-1.20.1"}',
    )
    info = reg.import_jar(server, src)
    assert info.is_dual
    import os
    assert os.path.isfile(os.path.join(server.dir, "outside-mohist-1.20.1.jar"))
    # Detection result is persisted, not just returned.
    reloaded = _registry(tmp_path).get("Hybrid")
    assert reloaded.server_type == "mohist"
    assert reloaded.mc_version == "1.20.1"
    assert reloaded.supports_mods and reloaded.supports_plugins


def test_import_missing_jar_raises(tmp_path):
    reg = _registry(tmp_path)
    server = reg.create("NoJar")
    with pytest.raises(ServerError):
        reg.import_jar(server, str(tmp_path / "does-not-exist.jar"))


# ------------------------------------------------------------------ properties
def test_properties_missing_file_is_empty(tmp_path):
    reg = _registry(tmp_path)
    server = reg.create("Fresh")
    entries, raw = read_properties(server)
    assert entries == [] and raw == []


def test_properties_round_trip_preserves_comments(tmp_path):
    import os

    reg = _registry(tmp_path)
    server = reg.create("Props")
    body = ("# Minecraft server properties\n"
            "motd=A Minecraft Server\n"
            "server-port=25565\n"
            "\n"
            "# game settings\n"
            "difficulty = easy\n")
    with open(os.path.join(server.dir, "server.properties"), "w",
              encoding="utf-8") as fh:
        fh.write(body)
    entries, raw = read_properties(server)
    assert [e.key for e in entries] == ["motd", "server-port", "difficulty"]
    assert entries[2].value == "easy"   # spaced " = " parses cleanly
    write_properties(server, raw, {"motd": "My Server", "difficulty": "hard"})
    with open(os.path.join(server.dir, "server.properties"), encoding="utf-8") as fh:
        text = fh.read()
    assert "motd=My Server" in text
    assert "difficulty =hard" in text   # original spacing kept
    assert "# Minecraft server properties" in text
    assert "server-port=25565" in text  # untouched keys survive
    # Unknown modded keys round-trip too.
    write_properties(server, read_properties(server)[1],
                     {"mohist-extra-flag": "true"})
    with open(os.path.join(server.dir, "server.properties"), encoding="utf-8") as fh:
        assert "mohist-extra-flag=true" in fh.read()


# ---------------------------------------------------------------------- worlds
def test_worlds_detected_by_level_dat(tmp_path):
    import os

    reg = _registry(tmp_path)
    server = reg.create("Worlds")
    world = os.path.join(server.dir, "world")
    os.makedirs(os.path.join(world, "region"))
    open(os.path.join(world, "level.dat"), "wb").close()
    # mods/ already exists from create() -- it must never count as a world.
    found = {w.name: w for w in list_worlds(server)}
    assert "world" in found and found["world"].confirmed is True
    assert "mods" not in found


def test_worlds_fallback_lists_unmarked_folders(tmp_path):
    import os

    reg = _registry(tmp_path)
    server = reg.create("Fresh2")
    os.makedirs(os.path.join(server.dir, "MyWorld", "data"))
    with open(os.path.join(server.dir, "MyWorld", "data", "x.dat"), "wb") as fh:
        fh.write(b"0" * 100)
    found = {w.name: w for w in list_worlds(server)}
    assert found["MyWorld"].confirmed is False
    assert found["MyWorld"].size_bytes >= 100


def test_rename_world_guards(tmp_path):
    import os

    reg = _registry(tmp_path)
    server = reg.create("Renamer")
    os.makedirs(os.path.join(server.dir, "world"))
    open(os.path.join(server.dir, "world", "level.dat"), "wb").close()
    renamed = rename_world(server, "world", "survival")
    assert renamed.name == "survival"
    assert os.path.isdir(os.path.join(server.dir, "survival"))
    with pytest.raises(ServerError):
        rename_world(server, "survival", "../escape")


# ----------------------------------------------------------------------- files
def test_server_files_list_and_delete_guarded(tmp_path):
    import os

    reg = _registry(tmp_path)
    server = reg.create("Files")
    plugins = os.path.join(server.dir, "plugins")
    open(os.path.join(plugins, "Essentials.jar"), "wb").close()
    open(os.path.join(plugins, "notes.txt"), "w").write("x")
    assert list_server_files(server, "plugins") == ["Essentials.jar"]
    delete_server_file(server, "plugins", "Essentials.jar")
    assert list_server_files(server, "plugins") == []
    with pytest.raises(ServerError):
        list_server_files(server, "config")
    with pytest.raises(ServerError):
        delete_server_file(server, "plugins", "../../evil.jar")


# ----------------------------------------------------------------- mod loader
def test_mod_loader_for_maps_hybrids_and_refuses_plugin_only():
    from launcher.servers import mod_loader_for

    assert mod_loader_for("mohist") == "forge"
    assert mod_loader_for("magma") == "forge"
    assert mod_loader_for("cardboard") == "fabric"
    assert mod_loader_for("fabric") == "fabric"
    assert mod_loader_for("neoforge") == "neoforge"
    assert mod_loader_for("paper") is None      # plugins only, no mods/
    assert mod_loader_for("vanilla") is None
    assert mod_loader_for("unknown") is None

