"""
Unit tests for launcher.external_mods.

Run with:  python test_external_mods.py   (or: python -m pytest test_external_mods.py -v)

The tests use a stub instance whose ``dir``/``mods_dir`` point at a temp
directory, so nothing touches the real %APPDATA%/Cove tree.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from launcher import external_mods as em  # noqa: E402


class StubInstance:
    """Minimal stand-in for launcher.instances.Instance."""

    def __init__(self, root: str) -> None:
        self.name = "TestInstance"
        self._dir = root
        self.external_mod_dirs: list[str] = []

    @property
    def dir(self) -> str:
        return self._dir

    @property
    def mods_dir(self) -> str:
        return os.path.join(self._dir, "mods")


def _write_jar(folder: str, name: str, payload: bytes = b"jar-content") -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _mods(inst) -> set[str]:
    if not os.path.isdir(inst.mods_dir):
        return set()
    return set(os.listdir(inst.mods_dir))


def _scenario():
    """(temp root, instance, external folder) with automatic cleanup."""
    root = tempfile.mkdtemp()
    ext = os.path.join(root, "shared")
    inst = StubInstance(os.path.join(root, "inst"))
    return root, ext, inst


# ------------------------------------------------------------------ tests --
def test_add_folder_exposes_jars():
    root, ext, inst = _scenario()
    try:
        _write_jar(ext, "sodium.jar")
        _write_jar(ext, "fabric-api.jar")
        _write_jar(ext, "readme.txt")          # ignored: not a jar

        em.add_folder(inst, ext)

        assert _mods(inst) == {"sodium.jar", "fabric-api.jar"}
        assert inst.external_mod_dirs == [os.path.abspath(ext)]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_source_file_is_never_deleted_or_moved():
    root, ext, inst = _scenario()
    try:
        src = _write_jar(ext, "sodium.jar", b"original-bytes")
        em.add_folder(inst, ext)

        assert os.path.isfile(src)
        assert _read(src) == b"original-bytes"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_removed_entry_keeps_the_source():
    root, ext, inst = _scenario()
    try:
        src = _write_jar(ext, "sodium.jar")
        em.add_folder(inst, ext)

        assert em.remove_entry(inst, "sodium.jar") is True
        assert os.path.isfile(src), "source must survive"
        assert "sodium.jar" not in _mods(inst)
        assert em.remove_entry(inst, "sodium.jar") is False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_own_mods_are_never_clobbered():
    root, ext, inst = _scenario()
    try:
        _write_jar(ext, "sodium.jar", b"external-version")
        # The user already owns a real mod with the same name.
        mine = _write_jar(inst.mods_dir, "sodium.jar", b"my-own-version")

        em.add_folder(inst, ext)

        assert _read(mine) == b"my-own-version"
        # The external copy landed beside it under a distinct name.
        assert "sodium (ext).jar" in _mods(inst)
        assert _read(os.path.join(inst.mods_dir,
                                  "sodium (ext).jar")) == b"external-version"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_modification_is_picked_up_on_next_sync():
    """The core requirement: edit the external file, resync, game sees it."""
    root, ext, inst = _scenario()
    try:
        src = _write_jar(ext, "mod.jar", b"version-one")
        em.add_folder(inst, ext)
        target = os.path.join(inst.mods_dir, "mod.jar")
        assert _read(target) == b"version-one"

        # Replace the external jar the way a real launcher would.
        bump = os.path.getmtime(src) + 10
        _write_jar(ext, "mod.jar", b"version-two-longer")
        os.utime(src, (bump, bump))

        em.sync(inst)
        assert _read(target) == b"version-two-longer"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_live_link_methods_reflect_edits_immediately():
    """When symlink/hardlink wins, the exposed file *is* the source file."""
    root, ext, inst = _scenario()
    try:
        src = _write_jar(ext, "mod.jar", b"version-one")
        em.add_folder(inst, ext)

        entry = em.source_for(inst, "mod.jar")
        assert entry is not None, "must be recorded in the manifest"

        if entry.method in ("symlink", "hardlink"):
            _write_jar(ext, "mod.jar", b"version-two")
            # No sync call at all -- the link already resolves to the new file.
            target = os.path.join(inst.mods_dir, "mod.jar")
            assert _read(target) == b"version-two"
        else:
            # Documented fallback: a copy refreshes on the next sync.
            assert entry.method == "copy"
        assert os.path.isfile(src)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_sync_is_idempotent():
    root, ext, inst = _scenario()
    try:
        _write_jar(ext, "a.jar")
        _write_jar(ext, "b.jar")

        em.add_folder(inst, ext)
        second = em.sync(inst)

        assert second.total() == 0, "second run must be a no-op"
        assert _mods(inst) == {"a.jar", "b.jar"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_deleted_source_is_pruned():
    root, ext, inst = _scenario()
    try:
        src = _write_jar(ext, "gone.jar")
        _write_jar(ext, "stays.jar")

        em.add_folder(inst, ext)
        assert "gone.jar" in _mods(inst)

        os.remove(src)
        em.sync(inst)

        assert _mods(inst) == {"stays.jar"}
        assert em.source_for(inst, "gone.jar") is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_remove_folder_unlinks_but_keeps_sources():
    root, ext, inst = _scenario()
    try:
        _write_jar(ext, "a.jar")
        _write_jar(ext, "b.jar")

        em.add_folder(inst, ext)
        result = em.remove_folder(inst, ext)

        assert result.removed == 2
        assert _mods(inst) == set()
        assert os.path.isfile(os.path.join(ext, "a.jar"))
        assert os.path.isfile(os.path.join(ext, "b.jar"))
        assert inst.external_mod_dirs == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_disabled_mods_stay_disabled_when_renamed():
    root, ext, inst = _scenario()
    try:
        _write_jar(ext, "mod.jar.disabled")
        _write_jar(inst.mods_dir, "mod.jar.disabled", b"mine")

        em.add_folder(inst, ext)

        names = _mods(inst)
        assert "mod.jar.disabled" in names, "user's own file must be intact"
        renamed = [n for n in names
                   if n.endswith(".jar.disabled") and n != "mod.jar.disabled"]
        assert renamed, "expected the external one to be renamed"
        assert renamed[0].endswith(".jar.disabled")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_cannot_add_the_instances_own_mods_folder():
    root, ext, inst = _scenario()
    try:
        os.makedirs(inst.mods_dir, exist_ok=True)
        try:
            em.add_folder(inst, inst.mods_dir)
        except em.ExternalModsError:
            return
        raise AssertionError("should reject the instance's own mods folder")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_missing_folder_does_not_raise():
    root, ext, inst = _scenario()
    try:
        inst.external_mod_dirs = [os.path.join(root, "gone")]
        result = em.sync(inst)          # must not raise
        assert result.errors
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_manifest_ignores_escaping_and_invalid_entries():
    root, ext, inst = _scenario()
    try:
        os.makedirs(inst.dir, exist_ok=True)
        with open(em.manifest_path(inst), "w", encoding="utf-8") as fh:
            fh.write(
                '{"version": 1, "entries": ['
                '{"filename": "../evil.jar", "source": "x.jar",'
                ' "method": "copy"},'
                '{"filename": "ok.jar", "source": "x.jar",'
                ' "method": "nonsense"}]}'
            )
        assert em.load_manifest(inst) == {}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_corrupt_manifest_is_tolerated():
    root, ext, inst = _scenario()
    try:
        os.makedirs(inst.dir, exist_ok=True)
        with open(em.manifest_path(inst), "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        assert em.load_manifest(inst) == {}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_folder_methods_reports_link_style():
    root, ext, inst = _scenario()
    try:
        _write_jar(ext, "a.jar")
        em.add_folder(inst, ext)

        methods = em.folder_methods(inst)
        key = os.path.abspath(ext)
        assert key in methods
        assert methods[key] <= {"symlink", "hardlink", "copy"}
        assert methods[key], "at least one method should be recorded"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_clear_removes_every_folder():
    root, ext, inst = _scenario()
    try:
        other = os.path.join(root, "other")
        _write_jar(ext, "a.jar")
        _write_jar(other, "b.jar")

        em.add_folder(inst, ext)
        em.add_folder(inst, other)
        assert len(_mods(inst)) == 2

        result = em.clear(inst)

        assert result.removed == 2
        assert _mods(inst) == set()
        assert inst.external_mod_dirs == []
        assert os.path.isfile(os.path.join(ext, "a.jar"))
        assert os.path.isfile(os.path.join(other, "b.jar"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_normalise_folders_dedupes():
    root, ext, inst = _scenario()
    try:
        messy = [ext, ext + os.sep, ext.upper(), "", "   "]
        inst.external_mod_dirs = messy
        folders = em.normalise_folders(inst)

        assert len(folders) == 1, f"expected one unique folder, got {folders}"
        assert os.path.isabs(folders[0])
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
