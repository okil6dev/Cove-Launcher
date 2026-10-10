"""
Tests for the AI Installer backend (launcher/ai_client.py).

Run: python test_ai_client.py

The important one is validate_picks: it is the guard that stops the model from
inventing mod ids. Everything else is endpoint/parsing plumbing.
"""

from __future__ import annotations

import contextlib

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from launcher import ai_client  # noqa: E402
from launcher.ai_client import AiConfig, AiError  # noqa: E402


class FakeVault:
    """Stand-in for the encrypted Vault."""

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.writes = 0

    def read(self):
        return dict(self.data)

    def write(self, secrets):
        self.data = dict(secrets)
        self.writes += 1

    def get(self, key, default=None):
        return self.data.get(key, default)


def _catalogue():
    return [
        {"source": "modrinth", "project_id": "AANobbMI", "title": "Sodium",
         "description": "Modern rendering engine", "author": "CaffeineMC",
         "downloads": 100, "icon_url": "u1"},
        {"source": "curseforge", "project_id": "238222", "title": "JEI",
         "description": "Item lookup", "author": "misterfitz",
         "downloads": 50, "icon_url": "u2"},
    ]


# ------------------------------------------------------------ endpoints ----
def test_localhost_endpoints_get_the_path_appended():
    for base in ("http://localhost:11434/v1",
                 "http://127.0.0.1:1234/v1",
                 "http://localhost:8000/v1"):
        assert ai_client.normalise_endpoint(base) == base + "/chat/completions"


def test_full_url_is_not_double_appended():
    full = "http://localhost:11434/v1/chat/completions"
    assert ai_client.normalise_endpoint(full) == full
    assert ai_client.normalise_endpoint(full + "/") == full


def test_trailing_slash_and_whitespace_tolerated():
    assert ai_client.normalise_endpoint(
        "  https://api.openai.com/v1/  ") == \
        "https://api.openai.com/v1/chat/completions"


def test_scheme_is_added_when_missing():
    assert ai_client.normalise_endpoint("localhost:11434/v1").startswith(
        "http://localhost:11434")


def test_blank_endpoint_is_rejected_not_guessed():
    """No invented default: the user must add an endpoint deliberately."""
    for blank in ("", "   "):
        try:
            ai_client.normalise_endpoint(blank)
        except AiError as exc:
            assert "endpoint" in str(exc).lower()
        else:
            raise AssertionError("blank endpoint should not resolve")


def test_custom_online_proxy_is_supported():
    url = "https://my-proxy.example.com/llm/v1"
    assert ai_client.normalise_endpoint(url) == url + "/chat/completions"


def test_garbage_endpoint_raises():
    for bad in ("http://", "://nope", "http:// bad host"):
        try:
            ai_client.normalise_endpoint(bad)
        except AiError:
            continue
        raise AssertionError(f"expected AiError for {bad!r}")


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, _BadJson):
            raise ValueError("not json")
        return self._payload


class _BadJson:
    pass


@contextlib.contextmanager
def fake_get(resp=None, raises=None):
    """Patch requests.get and record the call args (url/headers/timeout)."""
    import requests

    calls = []

    def _get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {}),
                      "timeout": timeout})
        if raises is not None:
            raise raises
        return resp

    original = requests.get
    requests.get = _get
    try:
        yield calls
    finally:
        requests.get = original


# ----------------------------------------------------------------- models ---
def test_normalise_base_strips_the_chat_suffix():
    assert ai_client.normalise_base("http://localhost:11434/v1") == \
        "http://localhost:11434/v1"
    assert ai_client.normalise_base(
        "http://localhost:11434/v1/chat/completions") == \
        "http://localhost:11434/v1"


def test_fetch_models_parses_the_openai_shape():
    resp = FakeResponse(200, {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]})
    with fake_get(resp):
        out = ai_client.fetch_models(AiConfig(endpoint="https://api.x/v1"))
    assert out == ["gpt-4o", "gpt-4o-mini"]      # sorted, de-duplicated


def test_fetch_models_parses_the_ollama_shape_and_strips_latest():
    resp = FakeResponse(200, {"models": [{"name": "llama3.1:latest"},
                                         {"name": "qwen2.5-coder:7b"}]})
    with fake_get(resp):
        out = ai_client.fetch_models(
            AiConfig(endpoint="http://localhost:11434/v1"))
    assert out == ["llama3.1", "qwen2.5-coder:7b"]


def test_fetch_models_sends_no_content_type_on_a_get():
    resp = FakeResponse(200, {"data": [{"id": "m"}]})
    with fake_get(resp) as calls:
        ai_client.fetch_models(AiConfig(endpoint="http://x/v1", api_key="k"))
    headers = calls[0]["headers"]
    assert "Content-Type" not in headers
    assert headers["Authorization"] == "Bearer k"
    assert calls[0]["url"] == "http://x/v1/models"


def test_fetch_models_omits_auth_when_no_key():
    resp = FakeResponse(200, {"data": [{"id": "m"}]})
    with fake_get(resp) as calls:
        ai_client.fetch_models(AiConfig(endpoint="http://localhost:1/v1"))
    assert "Authorization" not in calls[0]["headers"]


def test_fetch_models_reports_a_bad_key_clearly():
    with fake_get(FakeResponse(401, {})):
        try:
            ai_client.fetch_models(AiConfig(endpoint="https://x/v1"))
        except AiError as exc:
            assert "key" in str(exc).lower()
        else:
            raise AssertionError("expected AiError")


def test_fetch_models_reports_a_404_as_a_bad_base_url():
    with fake_get(FakeResponse(404, {})):
        try:
            ai_client.fetch_models(
                AiConfig(endpoint="https://x/v1/chat/completions"))
        except AiError as exc:
            assert "404" in str(exc)
        else:
            raise AssertionError("expected AiError")


def test_fetch_models_reports_an_empty_list_without_dying():
    with fake_get(FakeResponse(200, {"data": []})):
        try:
            ai_client.fetch_models(AiConfig(endpoint="http://x/v1"))
        except AiError as exc:
            assert "type a model name" in str(exc)
        else:
            raise AssertionError("expected AiError")


def test_fetch_models_reports_non_json_clearly():
    with fake_get(FakeResponse(200, _BadJson())):
        try:
            ai_client.fetch_models(AiConfig(endpoint="http://x/v1"))
        except AiError as exc:
            assert "JSON" in str(exc)
        else:
            raise AssertionError("expected AiError")


def test_fetch_models_handles_a_connection_refused():
    import requests
    with fake_get(raises=requests.exceptions.ConnectionError("refused")):
        try:
            ai_client.fetch_models(AiConfig(endpoint="http://localhost:9/v1"))
        except AiError as exc:
            assert "Cannot reach" in str(exc)
        else:
            raise AssertionError("expected AiError")


# ------------------------------------------------------ reply extraction ---
def test_extract_reply_reads_the_standard_shape():
    data = {"choices": [{"message": {"content": "hello"}}]}
    assert ai_client.extract_reply(data) == "hello"


def test_extract_reply_reads_a_gateway_wrapped_shape():
    """cline.bot and similar routers nest the reply under 'data'."""
    data = {"data": {"choices": [{"message": {"content": "hi there"}}],
                    "model": "qwen/qwen3-30b"}}
    assert ai_client.extract_reply(data) == "hi there"


def test_extract_reply_reads_a_bare_content_field():
    assert ai_client.extract_reply({"content": "plain"}) == "plain"


def test_extract_reply_returns_none_for_an_unknown_shape():
    """Must not raise -- the caller turns None into a readable message."""
    assert ai_client.extract_reply({"weird": 1}) is None
    assert ai_client.extract_reply({"choices": []}) is None
    assert ai_client.extract_reply("not a dict") is None


def test_chat_survives_the_gateway_wrapped_shape():
    body = {"data": {"choices": [{"finish_reason": "stop",
                                  "message": {"content": "wrapped!"}}]}}

    class R:
        status_code = 200
        text = str(body)
        @staticmethod
        def json():
            return body

    cfg = AiConfig(endpoint="https://api.cline.bot/api/v1/chat/completions",
                  api_key="sk_x", model="m")
    with patched_post(R()):
        assert ai_client.chat(cfg, [{"role": "user", "content": "hi"}]) == "wrapped!"


def test_chat_reports_a_200_that_carries_an_error_body():
    """The gateway answers 200 with {"error": ...}; that must not become a
    bare KeyError on 'choices'."""
    body = {"error": {"message": "model not permitted"}}

    class R:
        status_code = 200
        text = str(body)
        @staticmethod
        def json():
            return body

    cfg = AiConfig(endpoint="https://api.cline.bot/api/v1/chat/completions",
                  api_key="sk_x", model="m")
    with patched_post(R()):
        try:
            ai_client.chat(cfg, [{"role": "user", "content": "hi"}])
        except AiError as exc:
            msg = str(exc)
            assert "model not permitted" in msg
            assert "choices" not in msg       # the old useless KeyError
        else:
            raise AssertionError("expected AiError")


def test_chat_quotes_the_body_when_the_shape_is_unknown():
    class R:
        status_code = 200
        text = "{}"
        @staticmethod
        def json():
            return {"totally": "unexpected"}

    cfg = AiConfig(endpoint="https://x/v1", api_key="k", model="m")
    with patched_post(R()):
        try:
            ai_client.chat(cfg, [{"role": "user", "content": "hi"}])
        except AiError as exc:
            assert "does not understand" in str(exc)
            assert "unexpected" in str(exc)
        else:
            raise AssertionError("expected AiError")


# --------------------------------------------------------- server errors ---
def test_403_is_not_reported_as_a_bad_api_key():
    """The user's exact case: Cline's key authenticates fine but the model is
    restricted. Reporting 'rejected the API key' sent them hunting for a key
    problem that did not exist."""
    body = {"error": {"code": "API_REQUEST_ERROR_CODE",
                      "message": "Error 403: deepseek/deepseek-v4-flash is only "
                                 "available via Cline product surfaces."}}

    class R:
        status_code = 403
        text = str(body)
        @staticmethod
        def json():
            return body

    cfg = AiConfig(endpoint="https://api.cline.bot/api/v1/chat/completions",
                  api_key="sk_valid", model="deepseek/deepseek-v4-flash")
    with patched_post(R()):
        try:
            ai_client.chat(cfg, [{"role": "user", "content": "hi"}])
        except AiError as exc:
            msg = str(exc)
            assert "not allowed" in msg
            assert "rejected the API key" not in msg   # the old, wrong wording
            assert "product surfaces" in msg           # server's real reason
            assert "different model" in msg            # actionable advice
        else:
            raise AssertionError("expected AiError")


def test_401_does_still_mean_a_bad_key():
    class R:
        status_code = 401
        text = '{"error":{"message":"Incorrect API key provided"}}'
        @staticmethod
        def json():
            return {"error": {"message": "Incorrect API key provided"}}
    cfg = AiConfig(endpoint="https://api.openai.com/v1", api_key="sk_bad")
    with patched_post(R()):
        try:
            ai_client.chat(cfg, [{"role": "user", "content": "hi"}])
        except AiError as exc:
            msg = str(exc)
            assert "401" in msg
            assert "Incorrect API key provided" in msg
        else:
            raise AssertionError("expected AiError")


def test_restricted_model_hint_fires_only_for_that_pattern():
    assert "different model" in ai_client._restricted_model_hint(
        "x is only available via Cline product surfaces.")
    assert ai_client._restricted_model_hint("quota exceeded") == ""


def test_server_error_detail_suffix_is_dropped_for_raw_json():
    class R:
        status_code = 401
        text = "some html"
        @staticmethod
        def json():
            raise ValueError
    assert ai_client._server_error_detail_suffix(R()) != ""
def test_server_error_detail_reads_a_string_error_field():
    class R:
        status_code = 400
        text = '{"error": "boom"}'
        @staticmethod
        def json():
            return {"error": "boom"}
    assert ai_client._server_error_detail(R()) == "boom"


def test_server_error_detail_reads_a_nested_message():
    class R:
        status_code = 400
        text = ""
        @staticmethod
        def json():
            return {"error": {"message": "bad model", "type": "invalid"}}
    assert ai_client._server_error_detail(R()) == "bad model"


def test_server_error_detail_falls_back_to_plain_text():
    class R:
        status_code = 502
        text = "  <html>gateway</html>  "
        @staticmethod
        def json():
            raise ValueError("not json")
    assert "gateway" in ai_client._server_error_detail(R())


def test_bad_model_hint_is_added_for_a_bare_model_name():
    cfg = AiConfig(model="llama3.1")
    hint = ai_client._bad_model_hint(cfg, "invalid model format")
    assert "type/model" in hint
    assert "llama3.1" in hint


def test_bad_model_hint_is_silent_when_the_name_already_has_a_slash():
    cfg = AiConfig(model="mistralai/Mistral-7B-Instruct")
    assert ai_client._bad_model_hint(cfg, "invalid model format") == ""


def test_bad_model_hint_is_silent_for_unrelated_errors():
    cfg = AiConfig(model="llama3.1")
    assert ai_client._bad_model_hint(cfg, "context length exceeded") == ""


def test_chat_reports_the_model_format_problem_clearly():
    """The user's exact failure: vLLM-style 400 saying 'modelType/model'."""
    class R:
        status_code = 400
        text = '{"error":"invalid model format. Expected format: modelType/model"}'
        @staticmethod
        def json():
            return {"error": "invalid model format. "
                             "Expected format: modelType/model"}
    cfg = AiConfig(endpoint="http://x/v1", model="llama3.1")
    with patched_post(R()):
        try:
            ai_client.chat(cfg, [{"role": "user", "content": "hi"}])
        except AiError as exc:
            msg = str(exc)
            assert "400" in msg
            assert "modelType/model" in msg      # server's own words, not raw JSON
            assert "{" not in msg               # no JSON blob leaking through
            assert "type/model" in msg           # actionable hint added
        else:
            raise AssertionError("expected AiError")


@contextlib.contextmanager
def patched_post(resp):
    """Patch requests.post and record the request body."""
    import requests

    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append(json or {})
        return resp

    original = requests.post
    requests.post = _post
    try:
        yield calls
    finally:
        requests.post = original


# --------------------------------------------------------------- endpoints ---
def test_endpoints_round_trip_through_the_vault():
    vault = FakeVault()
    cfg = AiConfig(endpoint="http://localhost:11434/v1",
                   model="llama3.1",
                   endpoints=[ai_client.Endpoint(
                       url="http://localhost:11434/v1", api_key="k",
                       models=["llama3.1", "qwen2.5"]),
                       ai_client.Endpoint(url="https://api.openai.com/v1",
                                          label="OpenAI")])
    ai_client.save_config(vault, cfg)
    back = ai_client.load_config(vault)
    assert len(back.endpoints) == 2
    assert back.endpoints[0].api_key == "k"
    assert back.endpoints[0].models == ["llama3.1", "qwen2.5"]
    assert back.endpoints[1].label == "OpenAI"


def test_endpoint_without_url_is_never_saved():
    vault = FakeVault()
    ai_client.save_config(vault, AiConfig(
        endpoint="", endpoints=[ai_client.Endpoint()]))
    assert ai_client.load_config(vault).endpoints == []


def test_loading_skips_malformed_endpoint_rows():
    vault = FakeVault({"ai_endpoints": ["nope", {"label": "no url"},
                                        {"url": "http://x/v1", "models": 5}]})
    eps = ai_client.load_config(vault).endpoints
    assert len(eps) == 1
    assert eps[0].url == "http://x/v1"
    assert eps[0].models == []


def test_endpoint_name_falls_back_to_the_url():
    assert ai_client.Endpoint(url="http://x/v1").name == "http://x/v1"
    assert ai_client.Endpoint(url="http://x/v1", label="Mine").name == "Mine"
    assert ai_client.Endpoint().name == "(no endpoint)"


def test_endpoint_local_detection():
    assert ai_client.Endpoint(url="http://localhost:1/v1").is_local
    assert not ai_client.Endpoint(url="https://api.openai.com/v1").is_local


# --------------------------------------------------------------- headers ---
def test_no_auth_header_when_key_is_blank():
    """Local servers reject an empty Authorization header."""
    assert "Authorization" not in ai_client.build_headers(AiConfig())
    assert "Authorization" not in ai_client.build_headers(
        AiConfig(api_key="   "))


def test_auth_header_present_with_key():
    h = ai_client.build_headers(AiConfig(api_key="sk-abc"))
    assert h["Authorization"] == "Bearer sk-abc"


def test_is_local_detection():
    assert AiConfig(endpoint="http://localhost:11434/v1").is_local
    assert AiConfig(endpoint="http://127.0.0.1:1234/v1").is_local
    assert not AiConfig(endpoint="https://api.openai.com/v1").is_local


# ------------------------------------------------------ hallucination ------
def test_invented_mod_ids_are_dropped():
    """The core guard: made-up ids never reach the UI."""
    picks = [
        {"project_id": "AANobbMI", "reason": "great"},
        {"project_id": "totally-made-up", "reason": "trust me"},
        {"project_id": "238222", "reason": "handy"},
    ]
    out = ai_client.validate_picks(picks, _catalogue())
    assert [r["project_id"] for r in out] == ["AANobbMI", "238222"]


def test_surviving_rows_come_from_the_catalogue():
    """Real title/icon must win over anything the model wrote."""
    picks = [{"project_id": "AANobbMI", "title": "TOTALLY DIFFERENT",
              "icon_url": "bogus", "reason": "fast"}]
    out = ai_client.validate_picks(picks, _catalogue())
    assert out[0]["title"] == "Sodium"
    assert out[0]["icon_url"] == "u1"
    assert out[0]["ai_reason"] == "fast"


def test_duplicates_are_collapsed():
    picks = [{"project_id": "AANobbMI"}, {"project_id": "AANobbMI"}]
    assert len(ai_client.validate_picks(picks, _catalogue())) == 1


def test_empty_catalogue_yields_nothing():
    assert ai_client.validate_picks([{"project_id": "AANobbMI"}], []) == []


# ------------------------------------------------------------- parsing -----
def test_parse_plain_json():
    text = '{"reply": "Here you go", "mods": [{"project_id": "AANobbMI"}]}'
    out = ai_client.parse_reply(text)
    assert out["reply"] == "Here you go"
    assert len(out["mods"]) == 1


def test_parse_json_in_code_fence():
    text = 'Sure!\n```json\n{"reply": "Hi", "mods": []}\n```'
    assert ai_client.parse_reply(text)["reply"] == "Hi"


def test_parse_json_embedded_in_prose():
    text = 'Here you go: {"reply": "Hi", "mods": []} Hope that helps!'
    assert ai_client.parse_reply(text)["reply"] == "Hi"


def test_parse_non_json_keeps_the_words():
    out = ai_client.parse_reply("I think Sodium would work well.")
    assert out["mods"] == []
    assert "Sodium" in out["reply"]


def test_parse_empty():
    assert ai_client.parse_reply("") == {"reply": "", "mods": []}


def test_parse_drops_non_dict_mods():
    text = json.dumps({"reply": "x", "mods": ["nope", {"project_id": "a"}]})
    assert len(ai_client.parse_reply(text)["mods"]) == 1


# -------------------------------------------------------------- search -----
def test_search_query_strips_filler_words():
    q = ai_client.search_query("I want some mods that improve performance")
    assert "mods" not in q.lower().split()
    assert "performance" in q


def test_search_query_falls_back_when_everything_is_filler():
    assert ai_client.search_query("mods").strip() != ""


def test_search_query_handles_empty():
    assert ai_client.search_query("") == ""


# --------------------------------------------------------- persistence -----
def test_config_round_trip():
    vault = FakeVault()
    cfg = AiConfig(endpoint="http://localhost:1234/v1", api_key="sk-x",
                   model="qwen2.5", temperature=0.9, max_results=4)
    ai_client.save_config(vault, cfg)
    assert vault.writes == 1

    back = ai_client.load_config(vault)
    assert back.endpoint == "http://localhost:1234/v1"
    assert back.api_key == "sk-x"
    assert back.model == "qwen2.5"
    assert back.temperature == 0.9
    assert back.max_results == 4


def test_config_defaults_when_vault_empty():
    """A fresh install starts with no endpoint and no model."""
    back = ai_client.load_config(FakeVault())
    assert back.endpoint == ""
    assert back.model == ""
    assert back.endpoints == []
    assert back.has_endpoint is False


def test_config_survives_a_broken_vault():
    class Broken:
        def read(self):
            raise RuntimeError("vault locked")

    assert ai_client.load_config(Broken()).model == ""


def test_load_config_with_no_vault():
    assert ai_client.load_config(None).endpoint == ""


def test_save_without_vault_raises():
    try:
        ai_client.save_config(None, AiConfig())
    except AiError:
        return
    raise AssertionError("expected AiError when the vault is missing")


def test_recent_models_keeps_latest_first():
    vault = FakeVault()
    ai_client.save_config(vault, AiConfig(model="llama3.1",
                                         recent_models=["old-model"]))
    assert vault.data["ai_recent_models"][0] == "llama3.1"
    assert "old-model" in vault.data["ai_recent_models"]


def test_out_of_range_values_are_clamped():
    back = ai_client.load_config(
        FakeVault({"ai_max_results": 9999, "ai_temperature": "nope"}))
    assert 1 <= back.max_results <= 20
    assert isinstance(back.temperature, float)


# -------------------------------------------------------------- prompt -----
def test_messages_include_catalogue_and_instance():
    msgs = ai_client.build_messages([], "give me performance mods",
                                    _catalogue(), "MyPack (1.21.1, fabric)", 4)
    assert msgs[0]["role"] == "system"
    assert "AANobbMI" in msgs[0]["content"]
    assert "MyPack (1.21.1, fabric)" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user",
                        "content": "give me performance mods"}


def test_history_is_carried_forward():
    hist = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "bogus", "content": "dropped"}]
    msgs = ai_client.build_messages(hist, "more", [], "inst", 3)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "dropped" not in msgs[2]["content"]


def test_history_is_capped():
    hist = [{"role": "user", "content": str(i)} for i in range(50)]
    msgs = ai_client.build_messages(hist, "now", [], "inst", 3)
    assert len(msgs) <= 12


# ------------------------------------------------- strict project-type mode ----
def test_build_messages_forces_project_type():
    base = dict(history=[], user_text="find things",
                catalogue=[], instance_label="target", max_results=5)
    plugin = ai_client.build_messages(project_type="plugin", **base)
    mods = ai_client.build_messages(**base)          # default = mods
    assert "strictly PLUGINS" in plugin[0]["content"]
    assert "only plugins" in plugin[0]["content"]
    assert "strictly MODS" in mods[0]["content"]
    # The strict rule must never appear reversed.
    assert "strictly MODS" not in plugin[0]["content"]


def test_build_messages_empty_catalogue_names_the_kind():
    plugin = ai_client.build_messages(
        [], "x", [], "srv", 3, project_type="plugin")
    assert "no matching plugins were found" in plugin[0]["content"]


def test_project_type_round_trips_through_vault():
    vault = FakeVault()
    cfg = AiConfig(endpoint="http://localhost:1/v1", model="m",
                   project_type="plugin")
    ai_client.save_config(vault, cfg)
    assert ai_client.load_config(vault).project_type == "plugin"


def test_project_type_defaults_and_sanitizes_to_mod():
    assert ai_client.load_config(FakeVault()).project_type == "mod"
    junk = ai_client.load_config(FakeVault({"ai_project_type": "banana"}))
    assert junk.project_type == "mod"


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