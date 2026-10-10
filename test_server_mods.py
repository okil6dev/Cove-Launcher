"""Unit tests for launcher.server_mods (instance -> server push sync)."""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from launcher import server_mods as sm  # noqa: E402


class StubInstance:
    def __init__(self, root: str, name="TestInstance") -> None:
        self.name = name
        self._dir = root
        self.external_mod_dirs = []
    @property
    def dir(self) -> str:
        return self._dir
    @property
    def mods_dir(self) -> str:
        return os.path.join(self._dir, "mods")


class StubServer:
    def __init__(self, root: str, name="TestServer",
                 server_type="fabric") -> None:
        self.name = name
        self._dir = os.path.join(root, "servers", name)
        self.server_type = server_type
    @property
    def dir(self) -> str:
        return self._dir
    @property
    def mods_dir(self) -> str:
        return os.path.join(self._dir, "mods")
    @property
    def supports_mods(self) -> bool:
        return self.server_type in ("fabric", "forge", "neoforge",
                                    "mohist", "magnetic")


def _write(folder: str, name: str, payload: bytes = b"jar-content") -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


def _mods(folder: str) -> set:
    if not os.path.isdir(folder):
        return set()
    return set(os.listdir(folder))


def _scenario(server_type="fabric"):
    root = tempfile.mkdtemp()
    inst = StubInstance(os.path.join(root, "inst"))
    srv = StubServer(root, server_type=server_type)
    return root, inst, srv
def test_link_pushes_jars():
    root, inst, srv = _scenario()
    try:
        _write(inst.mods_dir, "sodium.jar")
        _write(inst.mods_dir, "fabric-api.jar")
        result = sm.link(srv, inst)
        assert result.total() == 2, result.summary()
        assert _mods(srv.mods_dir) == {"sodium.jar", "fabric-api.jar"}
        assert sm.linked_instance(srv) == "TestInstance"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_empty_instance_rejected():
    root, inst, srv = _scenario()
    try:
        try:
            sm.link(srv, inst)
        except sm.ServerModsError:
            return
        raise AssertionError("expected ServerModsError for empty instance")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_vanilla_server_rejected():
    root, inst, srv = _scenario(server_type="vanilla")
    try:
        _write(inst.mods_dir, "sodium.jar")
        try:
            sm.link(srv, inst)
        except sm.ServerModsError:
            return
        raise AssertionError("expected ServerModsError for vanilla server")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_server_owned_mods_never_clobbered():
    root, inst, srv = _scenario()
    try:
        _write(inst.mods_dir, "sodium.jar", b"instance-version")
        mine = _write(srv.mods_dir, "sodium.jar", b"server-version")
        sm.link(srv, inst)
        with open(mine, "rb") as fh:
            assert fh.read() == b"server-version"
        names = _mods(srv.mods_dir)
        assert "sodium.jar" in names and len(names) == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_unlink_removes_only_synced():
    root, inst, srv = _scenario()
    try:
        _write(inst.mods_dir, "a.jar")
        mine = _write(srv.mods_dir, "mine.jar", b"mine")
        sm.link(srv, inst)
        result = sm.unlink(srv)
        assert result.removed == 1, result.summary()
        assert _mods(srv.mods_dir) == {"mine.jar"}
        assert os.path.isfile(mine)
        assert sm.linked_instance(srv) is None
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

