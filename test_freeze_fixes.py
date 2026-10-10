"""
Regression tests for the GUI-freeze fixes.

Run with:  python test_freeze_fixes.py

The bug these guard against: work that blocks the Qt GUI thread. A slot that
performs network I/O or a full disk walk stops Qt from processing any event,
so the window stops repainting and Windows shows "Not Responding".
"""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from launcher import moddb  # noqa: E402
from launcher import workers_net  # noqa: E402
from launcher import external_mods  # noqa: E402


def _make_jar(folder: str, name: str, title: str) -> str:
    import json
    import zipfile

    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("fabric.mod.json",
                    json.dumps({"name": title, "id": name, "icon": "i.png"}))
        zf.writestr("i.png", b"\x89PNG" * 200)
    return path


# ------------------------------------------------------- #3 name caching --
def test_display_name_is_cached():
    """A cached read must not depend on the file still being there."""
    root = tempfile.mkdtemp()
    try:
        mods = os.path.join(root, "mods")
        _make_jar(mods, "a.jar", "Alpha Mod")
        assert moddb.get_mod_display_name(mods, "a.jar") == "Alpha Mod"

        # Remove it: the isfile() guard must still report "gone".
        os.remove(os.path.join(mods, "a.jar"))
        assert moddb.get_mod_display_name(mods, "a.jar") is None

        # Recreating at a new mtime must produce a fresh read.
        _make_jar(mods, "a.jar", "Alpha Mod Renamed")
        assert moddb.get_mod_display_name(mods, "a.jar") == "Alpha Mod Renamed"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_display_name_cache_is_actually_hit():
    """A warm cache lookup must be cheaper than the cold read."""
    root = tempfile.mkdtemp()
    try:
        mods = os.path.join(root, "mods")
        for i in range(60):
            _make_jar(mods, f"m{i}.jar", f"Mod {i}")

        moddb._NAME_CACHE.clear()
        t0 = time.perf_counter()
        for i in range(60):
            moddb.get_mod_display_name(mods, f"m{i}.jar")
        cold = time.perf_counter() - t0

        t0 = time.perf_counter()
        for i in range(60):
            moddb.get_mod_display_name(mods, f"m{i}.jar")
        warm = time.perf_counter() - t0

        assert warm < cold, f"cache not helping: cold={cold:.3f}s warm={warm:.3f}s"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_display_name_survives_a_broken_jar():
    root = tempfile.mkdtemp()
    try:
        mods = os.path.join(root, "mods")
        os.makedirs(mods)
        with open(os.path.join(mods, "broken.jar"), "wb") as fh:
            fh.write(b"not a zip at all")
        assert moddb.get_mod_display_name(mods, "broken.jar") is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------- #1 description render --
def test_render_description_markdown():
    out = workers_net.render_description("# Title\n\nSome text.", "modrinth")
    assert "Title" in out


def test_render_description_curseforge_passthrough():
    html = "<p>already html</p>"
    assert workers_net.render_description(html, "curseforge") == html


def test_embed_images_no_images_is_identity():
    html = "<p>nothing here</p>"
    assert workers_net.embed_images(html) == html


def test_embed_images_skips_data_uris():
    html = '<img src="data:image/png;base64,AAAA">'
    assert workers_net.embed_images(html) == html


def test_embed_images_inlines_and_leaves_failures():
    """Good images are inlined; broken ones stay as remote URLs."""
    import base64
    import http.server
    import threading

    payload = b"\x89PNG\r\n\x1a\n" + b"fakepng"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/good.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        good = f"http://127.0.0.1:{port}/good.png"
        bad = f"http://127.0.0.1:{port}/missing.png"
        html = f'<p>hi</p><img src="{good}"><img src="{bad}">'

        out = workers_net.embed_images(html, timeout=5)

        assert "data:image/png;base64," in out, "good image not inlined"
        assert base64.b64encode(payload).decode("ascii") in out
        assert bad in out, "failed image should be left as a remote URL"
    finally:
        server.shutdown()
        server.server_close()


def test_embed_images_tolerates_a_dead_host():
    """A hanging host must not raise, and must still return the HTML."""
    html = '<p>text</p><img src="http://127.0.0.1:9/never-answers.png">'
    t0 = time.perf_counter()
    out = workers_net.embed_images(html, timeout=2, max_workers=2)
    elapsed = time.perf_counter() - t0

    assert elapsed < 8, f"took too long: {elapsed:.1f}s"
    assert "never-answers.png" in out
    assert "text" in out


def test_embed_images_deduplicates_repeated_urls():
    """The same image twice must only be fetched once."""
    import http.server
    import threading

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            body = b"\x89PNG\r\n\x1a\n"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        url = f"http://127.0.0.1:{port}/dup.png"
        html = f'<img src="{url}"><img src="{url}">'
        workers_net.embed_images(html, timeout=5)
        assert len(hits) == 1, f"fetched {len(hits)} times, expected 1"
    finally:
        server.shutdown()
        server.server_close()


# -------------------------------------------- #4 external mods sync guard --
def test_sync_tolerates_missing_instance():
    """LaunchThread may be constructed without an instance."""
    result = external_mods.sync(None)
    assert result.total() == 0
    assert result.errors == []


# ------------------------------------------------------------------ checks --
# Test harness notes -------------------------------------------------------
# LauncherWindow starts two network threads (version list, Microsoft profile).
# Building several windows means several such threads, and destroying a window
# whose QThread is still running aborts the whole process. So the harness
# stubs both and reuses a single window.
_WINDOW = None
_APP = None            # must be kept alive or Qt tears the world down
_THREADS_STUBBED = False


def _stub_network_threads() -> None:
    """Neutralise the network threads LauncherWindow starts on construction."""
    global _THREADS_STUBBED
    if _THREADS_STUBBED:
        return
    from PyQt6.QtCore import QThread, pyqtSignal

    import launcher.window as winmod
    import launcher.pages.skins_page as skins

    class _NoopVersionThread(QThread):
        ready = pyqtSignal(list)
        failed = pyqtSignal(str)

        def run(self):  # noqa: D102 - no network in tests
            return

    class _NoopMsProfileThread(QThread):
        ready = pyqtSignal(dict)
        failed = pyqtSignal(str)

        def __init__(self, *args, **kwargs) -> None:
            # Real signature is (store, account, parent).
            super().__init__()

        def run(self):  # noqa: D102 - no network in tests
            return

    winmod.VersionListThread = _NoopVersionThread
    skins._MsProfileThread = _NoopMsProfileThread
    _THREADS_STUBBED = True


def _locked_window():
    """A LauncherWindow whose installer thread is hung (never returns)."""
    global _WINDOW, _APP
    import tempfile

    from PyQt6.QtCore import QThread
    from PyQt6.QtWidgets import QApplication

    import launcher.constants as C

    # A local QApplication would be garbage collected immediately, taking the
    # whole Qt world (and every widget) with it.
    if QApplication.instance() is None:
        _APP = QApplication([])
    _stub_network_threads()

    root = tempfile.mkdtemp()
    C.INSTANCES_DIR = os.path.join(root, "instances")
    C.INSTANCE_INDEX_PATH = os.path.join(root, "instances.json")

    from launcher.window import LauncherWindow

    if _WINDOW is None:
        _WINDOW = LauncherWindow()

    win = _WINDOW

    # Point the registry at the temp dir explicitly. Reassigning
    # C.INSTANCE_INDEX_PATH is NOT enough: InstanceRegistry binds its
    # index_path as a default argument at import time, so the window keeps
    # reading and writing the real instances.json and these tests litter the
    # user's real instance list.
    from launcher.instances import InstanceRegistry

    win.registry = InstanceRegistry(C.INSTANCE_INDEX_PATH)

    # Reset any worker left over from a previous test before hanging a new one.
    stale = getattr(win, "_installer", None)
    if stale is not None and stale.isRunning():
        stale.terminate()
        stale.wait(2000)
    win._installer = None
    win._launcher = None
    win._set_busy(False)
    win._worker_last_activity = time.monotonic()

    toasts, logs = [], []
    win._toast = lambda *a, **k: toasts.append((a[1], a[2]))
    win.log = lambda *a, **k: logs.append(a[0])

    class Hanger(QThread):
        def run(self):
            time.sleep(10 ** 6)          # simulates a dead network call

    win._installer = Hanger()
    win._installer.start()
    time.sleep(0.2)
    return win, toasts, logs, root


def test_blocked_create_is_not_silent():
    """A refused action must explain itself -- this is the 'dead button' bug."""
    win, toasts, logs, root = _locked_window()
    try:
        win._on_new_instance()
        assert toasts, "no toast shown: the button looked dead"
        assert logs, "nothing written to the console log"
        assert "still running" in toasts[-1][1].lower()
        assert win._busy_reason()
    finally:
        win._installer.terminate()
        win._installer.wait()
        shutil.rmtree(root, ignore_errors=True)


def test_blocked_play_and_import_are_not_silent():
    win, toasts, logs, root = _locked_window()
    try:
        win._on_play("whatever")
        win._on_import_pack()
        assert len(toasts) >= 2, f"expected 2 warnings, got {len(toasts)}"
        assert any("play" in t[1] for t in toasts)
        assert any("import" in t[1] for t in toasts)
    finally:
        win._installer.terminate()
        win._installer.wait()
        shutil.rmtree(root, ignore_errors=True)


def test_watchdog_unlocks_a_hung_worker():
    """The launcher must never stay permanently locked out."""
    win, toasts, logs, root = _locked_window()
    try:
        assert win._is_busy()
        win.WORKER_STALL_SECONDS = 0.1
        win._worker_last_activity = time.monotonic() - 999
        win._check_worker_stalled()

        assert not win._is_busy(), "watchdog failed to unlock the launcher"
        assert win._installer is None, "hung worker reference not cleared"
        assert any("stuck" in t[0].lower() for t in toasts)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_watchdog_leaves_healthy_workers_alone():
    """A busy-but-progressing worker must never be killed."""
    win, toasts, logs, root = _locked_window()
    try:
        win.WORKER_STALL_SECONDS = 300.0
        win._worker_last_activity = time.monotonic()   # just reported progress
        win._check_worker_stalled()

        assert win._is_busy(), "watchdog killed a healthy worker"
        assert win._installer is not None
        assert not any("stuck" in t[0].lower() for t in toasts)
    finally:
        win._installer.terminate()
        win._installer.wait()
        shutil.rmtree(root, ignore_errors=True)


def test_watchdog_is_a_noop_when_idle():
    win, toasts, logs, root = _locked_window()
    try:
        win._installer.terminate()
        win._installer.wait()
        win._installer = None
        win.WORKER_STALL_SECONDS = 0.0
        win._worker_last_activity = time.monotonic() - 999
        win._check_worker_stalled()
        assert not toasts, "watchdog fired while idle"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_clear_worker_respects_identity():
    """A finished worker must not clear a newer one that replaced it."""
    win, toasts, logs, root = _locked_window()
    try:
        old = win._installer
        new = object()
        win._installer = new
        win._clear_worker("_installer", old)
        assert win._installer is new, "stale worker cleared the current one"
        win._clear_worker("_installer", new)
        assert win._installer is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_create_works_again_after_recovery():
    """End-to-end: recover from the lock, then create an instance normally."""
    win, toasts, logs, root = _locked_window()
    try:
        from launcher.dialogs import NewInstanceDialog

        win.WORKER_STALL_SECONDS = 0.1
        win._worker_last_activity = time.monotonic() - 999
        win._check_worker_stalled()

        started = []
        win._start_install = lambda inst: started.append(inst.name)

        def fake_exec(self):
            self.name_edit.setText("Recovered")
            return True

        NewInstanceDialog.exec = fake_exec
        win._on_new_instance()

        names = [i.name for i in win.registry.list()]
        assert "Recovered" in names, f"instance not created: {names}"
        assert started == ["Recovered"]
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

