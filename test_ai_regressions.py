"""Regressions for three bugs reported together:

1. AI mod cards showed "?" forever -- ModBubble.set_pixmap() existed but the
   view had no icon fetcher and nothing ever called it.
2. The model claimed mods were "already installed" -- it was never told what is
   actually in the mods folder, so it invented that from nothing.
3. Searching used the whole question -- "can you find me a mod that adds
   shaders" searched for the literal string "adds shaders".

Run: python test_ai_regressions.py
"""

from __future__ import annotations

import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QBuffer, QByteArray, QEventLoop, QTimer  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

APP = QApplication.instance() or QApplication([])

from launcher import ai_client  # noqa: E402
from launcher.pages.ai_mods_view import AiModsView  # noqa: E402


class Vault:
    def __init__(self):
        self.data = {}

    def read(self):
        return dict(self.data)

    def write(self, secrets):
        self.data = dict(secrets)


class FakeInst:
    def __init__(self, loader="fabric"):
        self.name = "Pack"
        self.loader_id = loader
        self.mc_version = "1.21.1"
        self.mods_dir = "mods"


_ALIVE: list = []


def _png_bytes() -> bytes:
    img = QImage(64, 64, QImage.Format.Format_RGB32)
    img.fill(QColor("#3fa7a7"))
    p = QPainter(img)
    p.setPen(QColor("white"))
    p.drawText(20, 35, "M")
    p.end()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


class _IconServer:
    """Serves one real PNG so the icon path is exercised for real."""

    def __init__(self):
        payload = _png_bytes()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        outer.url = f"http://127.0.0.1:{self.srv.server_port}/icon.png"


def _view(inst=None):
    vault = Vault()
    host = QWidget()
    host.resize(900, 600)
    v = AiModsView(
        get_instance=lambda: inst if inst is not None else FakeInst(),
        log=lambda *a, **k: None, toast=lambda *a, **k: None,
        get_cf_key=lambda: "", get_vault=lambda: vault, parent=host)
    _ALIVE.extend([host, v])
    return v

def _pump(ms=2500):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


# ------------------------------------------------- 1. icons actually load ---
def test_icon_reaches_the_mod_card():
    """The reported bug: every AI suggestion showed "?" not a thumbnail."""
    srv = _IconServer()
    v = _view()
    v._add_mod_bubbles([{"source": "modrinth", "project_id": "p1",
                         "title": "Sodium", "icon_url": srv.url}])
    bubble = v._bubbles[0]
    assert bubble.icon.text() == "?", "should start as the placeholder"
    _pump()
    assert not bubble.icon.pixmap().isNull(), "pixmap never arrived"
    assert bubble.icon.text() == "", "placeholder text not cleared"


def test_second_card_with_same_icon_uses_the_cache():
    srv = _IconServer()
    v = _view()
    row = {"source": "modrinth", "project_id": "p1", "title": "A",
           "icon_url": srv.url}
    v._add_mod_bubbles([row])
    _pump()
    assert srv.url in v._icon_cache
    v._add_mod_bubbles([dict(row, project_id="p2", title="B")])
    assert not v._bubbles[1].icon.pixmap().isNull(), "cache miss on 2nd card"


def test_row_without_an_icon_is_left_alone():
    v = _view()
    v._add_mod_bubbles([{"source": "modrinth", "project_id": "p",
                         "title": "NoIcon", "icon_url": ""}])
    _pump(400)
    assert v._bubbles[0].icon.text() == "?"
    assert v._bubbles[0].icon.pixmap().isNull()


def test_garbage_icon_bytes_do_not_crash():
    v = _view()
    v._add_mod_bubbles([{"source": "modrinth", "project_id": "p",
                         "title": "T", "icon_url": "http://x/y.png"}])
    v._on_icon_loaded("http://x/y.png", b"not an image")
    assert "http://x/y.png" not in v._icon_cache


def test_icon_loaded_only_touches_matching_bubbles():
    v = _view()
    v._add_mod_bubbles([
        {"source": "modrinth", "project_id": "a", "title": "A",
         "icon_url": "http://x/a.png"},
        {"source": "modrinth", "project_id": "b", "title": "B",
         "icon_url": "http://x/b.png"},
    ])
    v._on_icon_loaded("http://x/a.png", _png_bytes())
    assert not v._bubbles[0].icon.pixmap().isNull()
    assert v._bubbles[1].icon.pixmap().isNull(), "wrong card got the icon"

# ------------------------------------- 2. the model knows what is installed ---
def _catalogue():
    return [{"source": "modrinth", "project_id": "AANobbMI", "title": "Iris",
             "slug": "iris", "author": "CaffeineMC", "description": "Shaders",
             "downloads": 1, "icon_url": ""}]


def test_installed_mods_are_named_to_the_model():
    msgs = ai_client.build_messages(
        [], "add shaders", _catalogue(), "Pack (1.21.1, fabric)", 5,
        installed=["sodium-fabric-0.5.8.jar", "lithium-fabric.jar"])
    system = msgs[0]["content"]
    assert "sodium-fabric-0.5.8.jar" in system
    assert "lithium-fabric.jar" in system


def test_prompt_forbids_claiming_mods_are_installed():
    """The actual complaint: the AI said a mod was already installed."""
    assert "NEVER claim a" in ai_client._SYSTEM_PROMPT
    assert "already installed" in ai_client._SYSTEM_PROMPT


def test_empty_instance_is_stated_not_left_blank():
    msgs = ai_client.build_messages([], "x", _catalogue(), "P", 5, installed=[])
    assert "(nothing installed yet)" in msgs[0]["content"]


def test_installed_section_precedes_the_catalogue():
    msgs = ai_client.build_messages([], "x", _catalogue(), "P", 5,
                                    installed=["a.jar"])
    system = msgs[0]["content"]
    # Match the section headers exactly -- "CATALOGUE" also appears as prose in
    # the system prompt, so a plain .index() finds the wrong occurrence.
    assert system.index("INSTALLED (already") < system.index("CATALOGUE:\n")


def test_view_reports_the_instance_mods_folder():
    """The view must read the real mods dir, not invent one."""
    v = _view(FakeInst())
    v._instance.mods_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "does_not_exist_mods")
    assert v._installed_files() == []      # no crash on a missing folder


def test_view_installed_list_is_empty_without_an_instance():
    v = _view()
    v._instance = None
    assert v._installed_files() == []


def test_thread_forwards_installed_into_the_prompt():
    import inspect

    from launcher.workers_net import AiModsThread

    assert "installed=self._installed" in inspect.getsource(AiModsThread.run)
# --------------------------------------------- 3. the query is the subject ---
def test_question_is_not_searched_verbatim():
    q = ai_client.search_query("can you find me a mod that adds shaders")
    assert q == "shaders"
    assert "adds" not in q
    assert "can" not in q


def test_plain_install_request_still_works():
    assert ai_client.search_query("install sodium") == "sodium"


def test_single_word_prompt_is_unchanged():
    assert ai_client.search_query("shaders") == "shaders"


def test_multiword_subject_is_kept():
    q = ai_client.search_query("do you have a mod for inventory management")
    assert "inventory" in q
    assert "management" in q
    assert "have" not in q


def test_performance_question_drops_the_scaffolding():
    q = ai_client.search_query("what mod improves fps")
    assert "improves" not in q
    assert "fps" in q


def test_query_is_bounded():
    q = ai_client.search_query("a b c d e f g h i j k l m n o p q r s t u v")
    assert len(q.split()) <= 6


def test_blank_input_does_not_crash():
    assert isinstance(ai_client.search_query(""), str)


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
