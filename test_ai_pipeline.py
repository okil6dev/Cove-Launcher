"""
End-to-end test of the AI pipeline with a fake local LLM server.

Run: python test_ai_pipeline.py

Proves the whole chain: prompt -> search both sources -> call the OpenAI-style
endpoint -> parse -> drop hallucinated ids -> hand back only real mods.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication  # noqa: E402

APP = QApplication.instance() or QApplication([])

from launcher import moddb  # noqa: E402
from launcher.ai_client import AiConfig  # noqa: E402
from launcher.workers_net import AiModsThread  # noqa: E402

# What the fake "model" will answer, and what it was asked.
STATE = {"content": "", "last_payload": {}, "path": ""}

CATALOGUE = [
    {"source": "modrinth", "project_id": "AANobbMI", "slug": "sodium",
     "title": "Sodium", "description": "Modern rendering engine",
     "author": "CaffeineMC", "downloads": 500, "icon_url": ""},
    {"source": "curseforge", "project_id": "238222", "slug": "jei",
     "title": "Just Enough Items", "description": "Item lookup",
     "author": "misterfitz", "downloads": 300, "icon_url": ""},
]


def _handler_factory():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            STATE["path"] = self.path
            length = int(self.headers.get("Content-Length") or 0)
            STATE["last_payload"] = json.loads(
                self.rfile.read(length).decode("utf-8"))
            body = json.dumps({
                "choices": [{"message": {"content": STATE["content"]}}]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _start_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _handler_factory())
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _run_thread(cfg, text, history=None):
    """Run AiModsThread to completion; return (reply, rows, error, status)."""
    out = {"reply": None, "rows": None, "error": None, "status": []}
    thread = AiModsThread(
        config=cfg, user_text=text, history=history or [],
        game_version="1.21.1", loader="fabric",
        instance_label="TestPack (1.21.1, fabric)", curseforge_api_key="",
    )
    thread.status.connect(out["status"].append)
    thread.ready.connect(lambda r, rows: out.update(reply=r, rows=rows))
    thread.failed.connect(lambda m: out.update(error=m))
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        APP.processEvents()
        time.sleep(0.02)
        if out["reply"] is not None or out["error"] is not None:
            break
    thread.wait(3000)
    return out


def _stub_searches():
    """Replace the two mod searches so the test never hits the network."""
    moddb.search_modrinth = lambda *a, **k: list(CATALOGUE)
    moddb.search_curseforge = lambda *a, **k: list(CATALOGUE)


def test_full_pipeline_drops_hallucinated_mods():
    server, port = _start_server()
    _stub_searches()
    # The model returns one real id, one invented, and a real CF id.
    STATE["content"] = json.dumps({
        "reply": "Sodium will massively boost FPS.",
        "mods": [
            {"project_id": "AANobbMI", "source": "modrinth",
             "reason": "Huge FPS gain"},
            {"project_id": "totally-invented-xyz", "source": "modrinth",
             "reason": "Sounds useful"},
            {"project_id": "238222", "source": "curseforge",
             "reason": "Better inventory"},
        ],
    })
    cfg = AiConfig(endpoint=f"http://127.0.0.1:{port}/v1",
                   model="fake-model", max_results=5)
    try:
        out = _run_thread(cfg, "mods that improve performance")
        assert out["error"] is None, f"unexpected failure: {out['error']}"
        assert "FPS" in out["reply"]

        ids = [r["project_id"] for r in out["rows"]]
        assert ids == ["AANobbMI", "238222"], f"got {ids}"
        assert "totally-invented-xyz" not in ids

        # The invented one is reported as dropped, not silently swallowed.
        assert any("did not exist" in s for s in out["status"]), out["status"]

        # Rows carry the catalogue's real data, not the model's claims.
        assert out["rows"][0]["title"] == "Sodium"
        assert out["rows"][0]["ai_reason"] == "Huge FPS gain"
    finally:
        server.shutdown()
        server.server_close()


def test_request_targets_chat_completions_and_sends_the_catalogue():
    server, port = _start_server()
    _stub_searches()
    STATE["content"] = json.dumps({"reply": "ok", "mods": []})
    cfg = AiConfig(endpoint=f"http://127.0.0.1:{port}/v1", model="fake-model")
    try:
        out = _run_thread(cfg, "give me performance mods")
        assert out["error"] is None, out["error"]
        assert STATE["path"] == "/v1/chat/completions", STATE["path"]
        payload = STATE["last_payload"]
        assert payload["model"] == "fake-model"
        system = payload["messages"][0]["content"]
        assert "AANobbMI" in system, "catalogue not sent"
        assert "TestPack" in system, "instance not sent"
    finally:
        server.shutdown()
        server.server_close()


def test_no_api_key_is_fine_for_a_local_server():
    server, port = _start_server()
    _stub_searches()
    STATE["content"] = json.dumps({"reply": "ok", "mods": []})
    cfg = AiConfig(endpoint=f"http://127.0.0.1:{port}/v1", api_key="")
    try:
        assert _run_thread(cfg, "hello")["error"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_unreachable_endpoint_reports_cleanly():
    out = _run_thread(AiConfig(endpoint="http://127.0.0.1:9/v1", model="x"),
                      "hi")
    assert out["error"] is not None, "expected a connection error"
    assert "Cannot reach" in out["error"], out["error"]


def test_no_search_results_answers_without_calling_the_model():
    """Empty catalogue means nothing to choose, so the model is never asked."""
    server, port = _start_server()
    STATE["last_payload"] = {}          # reset: STATE is shared between tests
    moddb.search_modrinth = lambda *a, **k: []
    moddb.search_curseforge = lambda *a, **k: []
    cfg = AiConfig(endpoint=f"http://127.0.0.1:{port}/v1", model="x")
    try:
        out = _run_thread(cfg, "something obscure")
        assert out["error"] is None
        assert out["rows"] == []
        assert "couldn't find" in out["reply"].lower()
        assert STATE["last_payload"] == {}, "should not call the model"
    finally:
        server.shutdown()
        server.server_close()


def test_history_is_forwarded_on_the_second_turn():
    server, port = _start_server()
    _stub_searches()
    STATE["content"] = json.dumps({"reply": "sure", "mods": []})
    cfg = AiConfig(endpoint=f"http://127.0.0.1:{port}/v1", model="x")
    try:
        _run_thread(cfg, "first question",
                    history=[{"role": "user", "content": "earlier"},
                             {"role": "assistant",
                              "content": "earlier reply"}])
        roles = [m["role"] for m in STATE["last_payload"]["messages"]]
        assert roles == ["system", "user", "assistant", "user"], roles
    finally:
        server.shutdown()
        server.server_close()


def test_empty_query_retries_with_fewer_words():
    """A multi-word query with no hits retries word-by-word.

    Regression for "more nature plants biomes": the full phrase matches
    nothing on Modrinth even though each word alone matches plenty, so the
    thread must widen (recall) rather than answer "couldn't find any".
    """
    server, port = _start_server()
    calls: list[str] = []

    def fake_modrinth(query, *a, **k):
        calls.append(query)
        if query == "more nature plants biomes":
            return []
        return [dict(CATALOGUE[0])]

    moddb.search_modrinth = fake_modrinth
    moddb.search_curseforge = lambda *a, **k: []
    STATE["content"] = json.dumps({
        "reply": "ok",
        "mods": [{"project_id": "AANobbMI", "reason": "nature"}],
    })
    cfg = AiConfig(endpoint=f"http://127.0.0.1:{port}/v1", model="x")
    try:
        out = _run_thread(cfg, "more nature plants biomes")
        assert out["error"] is None, out["error"]
        assert len(calls) >= 2, f"never retried: {calls}"
        assert calls[0] == "more nature plants biomes"
        assert out["rows"], "fallback query should have found Sodium"
    finally:
        server.shutdown()
        server.server_close()


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