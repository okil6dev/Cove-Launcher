"""
AI provider layer for the "AI Installer" tab.

Deliberately Qt-free so it can be unit tested without an app instance, and so
the GUI never touches it directly -- :class:`launcher.workers_net.AiModsThread`
calls everything here from a worker thread.

Wire format
-----------
Every provider this targets speaks the OpenAI ``/v1/chat/completions`` shape:
Ollama, LM Studio, vLLM, llama.cpp, OpenAI, OpenRouter, Groq, or any proxy.
That means one HTTP call, configurable per user, covers local and hosted
endpoints alike.

The reliability trick
---------------------
A language model will happily invent a mod id that does not exist. So the model
never searches and never invents: the launcher searches Modrinth *and*
CurseForge itself, filtered to the selected instance's MC version and loader,
and hands the model that verified catalogue. The model may only choose ids
present in it. :func:`validate_picks` enforces that, so a hallucinated project
is dropped rather than offered for download.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .constants import PROJECT_TYPE_MOD, PROJECT_TYPE_PLUGIN

DEFAULT_ENDPOINT = "http://localhost:11434/v1"
DEFAULT_MODEL = "llama3.1"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_RESULTS = 6
REQUEST_TIMEOUT = 120

# Presets offered in the gear dialog. Local servers normally need no API key.
ENDPOINT_PRESETS: tuple[tuple[str, str], ...] = (
    ("Ollama (local)", "http://localhost:11434/v1"),
    ("LM Studio (local)", "http://localhost:1234/v1"),
    ("vLLM (local)", "http://localhost:8000/v1"),
    ("llama.cpp (local)", "http://localhost:8080/v1"),
    ("OpenAI", "https://api.openai.com/v1"),
    ("OpenRouter", "https://openrouter.ai/api/v1"),
    ("Groq", "https://api.groq.com/openai/v1"),
)

# Vault keys (encrypted at rest via DPAPI + AES-256-GCM).
_VAULT_PREFIX = "ai_"


class AiError(Exception):
    """User-facing AI failure (bad endpoint, no key, refused, timed out)."""


@dataclass
class Endpoint:
    """One saved connection the user can switch between.

    ``models`` is whatever was discovered by "Fetch models" plus anything typed
    by hand, so a server that does not implement /models is still usable.
    """
    url: str = ""
    api_key: str = ""
    label: str = ""
    models: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        if self.label.strip():
            return self.label.strip()
        return self.url.strip() or "(no endpoint)"

    @property
    def is_local(self) -> bool:
        return any(h in (self.url or "").lower()
                   for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))


@dataclass
class AiConfig:
    """Everything the gear dialog edits."""

    # Empty by default: the user adds their own endpoint with the + button
    # rather than being handed a guessed localhost that may not be running.
    endpoint: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = DEFAULT_TEMPERATURE
    max_results: int = DEFAULT_MAX_RESULTS
    # Strict search mode for the AI: "mod" or "plugin". The dropdown in the
    # top bar forces this, and the search facets honour it, so a plugin can
    # never be suggested when mods were asked for (and vice versa).
    project_type: str = PROJECT_TYPE_MOD
    recent_models: list[str] = field(default_factory=list)
    # Saved connections, newest first. Empty means "the user has not added an
    # endpoint yet", which is what the UI keys off to show the empty state.
    endpoints: list[Endpoint] = field(default_factory=list)

    @property
    def is_local(self) -> bool:
        return any(h in (self.endpoint or "").lower()
                   for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))

    @property
    def has_endpoint(self) -> bool:
        return bool((self.endpoint or "").strip())


# ------------------------------------------------------------ persistence --
def load_config(vault) -> AiConfig:
    """Read the AI config from the encrypted vault (defaults when unset)."""
    cfg = AiConfig()
    if vault is None:
        return cfg
    try:
        data = vault.read()
    except Exception:  # noqa: BLE001 - a locked/corrupt vault must not crash
        return cfg

    cfg.endpoint = str(data.get(_VAULT_PREFIX + "endpoint", "") or "")
    cfg.api_key = str(data.get(_VAULT_PREFIX + "api_key", "") or "")
    cfg.model = str(data.get(_VAULT_PREFIX + "model", "") or "")
    try:
        cfg.temperature = float(
            data.get(_VAULT_PREFIX + "temperature", DEFAULT_TEMPERATURE))
    except (TypeError, ValueError):
        cfg.temperature = DEFAULT_TEMPERATURE
    try:
        cfg.max_results = max(
            1, min(20, int(data.get(_VAULT_PREFIX + "max_results",
                                     DEFAULT_MAX_RESULTS))))
    except (TypeError, ValueError):
        cfg.max_results = DEFAULT_MAX_RESULTS
    stored_type = str(data.get(_VAULT_PREFIX + "project_type",
                               PROJECT_TYPE_MOD) or "")
    cfg.project_type = (PROJECT_TYPE_PLUGIN
                        if stored_type == PROJECT_TYPE_PLUGIN
                        else PROJECT_TYPE_MOD)
    recent = data.get(_VAULT_PREFIX + "recent_models") or []
    if isinstance(recent, list):
        cfg.recent_models = [str(m) for m in recent if str(m).strip()][:10]

    # Saved endpoints. A malformed entry is skipped rather than raising, so one
    # bad row can never lock the user out of the AI tab entirely.
    saved = data.get(_VAULT_PREFIX + "endpoints") or []
    if isinstance(saved, list):
        for item in saved:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "") or "").strip()
            if not url:
                continue
            models = item.get("models") or []
            if not isinstance(models, list):
                models = []
            cfg.endpoints.append(Endpoint(
                url=url,
                api_key=str(item.get("api_key", "") or ""),
                label=str(item.get("label", "") or ""),
                models=[str(m).strip() for m in models
                        if str(m).strip()][:200],
            ))
    # Legacy vaults (saved before the endpoints list existed) store only the
    # single active ``endpoint`` and have no matching row in ``endpoints``.
    # The whole UI drives off ``cfg.endpoints`` -- the combo, the active
    # lookup and the model list -- while ``has_endpoint`` checks the lone
    # ``cfg.endpoint`` string, so such a config simultaneously reports
    # "has an endpoint" yet shows a disabled "No endpoint yet" combo with no
    # models. Reconcile here: materialise the active endpoint into the list so
    # the two can never disagree, whichever feature added it.
    if cfg.endpoint and not any(
            ep.url == cfg.endpoint for ep in cfg.endpoints):
        cfg.endpoints.insert(0, Endpoint(
            url=cfg.endpoint,
            api_key=cfg.api_key,
            models=[cfg.model] if cfg.model else [],
        ))
    return cfg


def save_config(vault, cfg: AiConfig) -> None:
    """Persist the config, remembering the model for the dropdown."""
    if vault is None:
        raise AiError("The secure vault is unavailable, so settings cannot be "
                      "saved.")
    data = vault.read()
    data[_VAULT_PREFIX + "endpoint"] = cfg.endpoint.strip()
    data[_VAULT_PREFIX + "api_key"] = cfg.api_key.strip()
    data[_VAULT_PREFIX + "model"] = cfg.model.strip()
    data[_VAULT_PREFIX + "temperature"] = float(cfg.temperature)
    data[_VAULT_PREFIX + "max_results"] = int(cfg.max_results)
    data[_VAULT_PREFIX + "project_type"] = (
        PROJECT_TYPE_PLUGIN
        if str(cfg.project_type) == PROJECT_TYPE_PLUGIN
        else PROJECT_TYPE_MOD)

    recent = [m for m in (cfg.model.strip(), *cfg.recent_models) if m][:10]
    data[_VAULT_PREFIX + "recent_models"] = recent
    data[_VAULT_PREFIX + "endpoints"] = [
        {"url": e.url.strip(), "api_key": e.api_key.strip(),
         "label": e.label.strip(),
         "models": [m for m in e.models if m][:200]}
        for e in cfg.endpoints if (e.url or "").strip()
    ]
    vault.write(data)
    cfg.recent_models = recent


# --------------------------------------------------------------- endpoint --
def normalise_endpoint(raw: str) -> str:
    """Turn whatever the user typed into a full chat-completions URL.

    * blank                       -> :class:`AiError` (the user must add one)
    * ``.../v1``                  -> ``.../v1/chat/completions``
    * ``.../v1/chat/completions`` -> unchanged (never double-appended)
    * trailing slashes/whitespace tolerated

    Raises :class:`AiError` for anything that is not http(s).
    """
    url = (raw or "").strip()
    if not url:
        raise AiError("No endpoint set. Add one with the + button.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "http://" + url
    # Require a real host: scheme:// + host [+ :port] + (/, ?, # or end).
    # Rejects bare "http://", whitespace, and stray colons.
    if not re.match(r"^https?://([^\s/?#:]+)(?::\d+)?(?:[/?#]|$)", url,
                    re.IGNORECASE):
        raise AiError(f"'{raw}' is not a valid endpoint.")

    url = url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    return url


def normalise_base(raw: str) -> str:
    """The provider root, i.e. :func:`normalise_endpoint` without the tail.

    ``http://localhost:11434/v1/chat/completions`` -> ``http://localhost:11434/v1``
    which is what ``/models`` is appended to. Blank input still yields the
    local default so callers never have to special-case it.
    """
    url = normalise_endpoint(raw)
    suffix = "/chat/completions"
    return url[:-len(suffix)] if url.endswith(suffix) else url


def fetch_models(cfg: "AiConfig", timeout: int = 15) -> list[str]:
    """Ask the provider which models it serves.

    Every OpenAI-compatible server exposes ``GET {base}/models`` and answers
    with ``{"data": [{"id": "..."}]}``. Ollama's compat shim, LM Studio and
    vLLM all follow it; a few servers instead answer ``{"models": [{"name":
    ...}]}``, so both shapes are accepted.

    Returns a sorted, de-duplicated list. Raises :class:`AiError` with
    something the user can act on (wrong key, wrong port, server down) --
    never a bare exception, since this runs from a dialog.
    """
    base = normalise_base(cfg.endpoint)
    url = base + "/models"
    headers = build_headers(cfg)
    headers.pop("Content-Type", None)      # a GET has no body to describe
    try:
        import requests
    except ImportError as exc:             # pragma: no cover - requests ships
        raise AiError("The 'requests' library is not available.") from exc

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectTimeout:
        raise AiError(f"Timed out connecting to {base}.") from None
    except requests.exceptions.ReadTimeout:
        raise AiError(f"{base} accepted the connection but did not answer in "
                      f"{timeout}s.") from None
    except requests.exceptions.ConnectionError:
        raise AiError(f"Cannot reach {base}. Is the server running and is the "
                      f"port right?") from None
    except Exception as exc:  # noqa: BLE001
        raise AiError(f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code in (401, 403):
        raise AiError("The server rejected the API key (401). Check the key "
                      "for this endpoint.")
    if resp.status_code == 404:
        raise AiError(f"{base}/models returned 404. Check the base URL -- it "
                      f"should end at /v1, not /chat/completions.")
    if resp.status_code >= 400:
        raise AiError(f"Server returned HTTP {resp.status_code} from {url}: "
                      f"{_server_error_detail(resp)}")

    try:
        data = resp.json()
    except ValueError:
        raise AiError(f"{url} did not return JSON. That is probably not an "
                      f"OpenAI-compatible endpoint.") from None

    names: list[str] = []
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        entries = data.get("models") if isinstance(data, dict) else None
    for item in entries or []:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("name") or item.get("model")
                       or "")
        else:
            continue
        name = name.strip()
        # Ollama advertises ":latest" tags that are just the same model; the
        # bare name is what the chat endpoint accepts most reliably.
        if name.endswith(":latest"):
            name = name[: -len(":latest")]
        if name:
            names.append(name)

    if not names:
        raise AiError(f"Connected to {base}, but it reported no models. You "
                      f"can still type a model name by hand.")
    return sorted(set(names))


def _server_error_detail(resp, limit: int = 220) -> str:
    """Pull the server's own error text out of a failed response.

    Servers put the useful part in a JSON ``error`` field (sometimes a nested
    ``message``), so show that rather than a wall of escaped JSON.
    """
    try:
        data = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return text[:limit] or "(empty response)"

    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        for key in ("message", "detail", "type"):
            value = err.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:limit]
    if isinstance(err, str) and err.strip():
        return err.strip()[:limit]
    if isinstance(data, dict):
        for key in ("message", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:limit]
    return str(data)[:limit]


def _short_body_hint(data, limit: int = 200) -> str:
    """Quote the unexpected body so the shape can actually be diagnosed."""
    try:
        text = json.dumps(data, ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        text = str(data)[:limit]
    return f" The response started with: {text}"


def extract_reply(data) -> str | None:
    """Pull the assistant text out of a chat-completions response.

    Handles the three shapes seen in the wild:

    * standard        ``{"choices": [{"message": {"content": ...}}]}``
    * gateway-wrapped ``{"data": {"choices": [...]}}``  <- cline.bot and friends
    * bare string     ``{"content": ...}``

    Returns ``None`` rather than raising when the shape is unrecognised, so
    the caller can show a useful message instead of a KeyError.
    """
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list):
        inner = data.get("data")
        choices = inner.get("choices") if isinstance(inner, dict) else None
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
                # Some gateways stream deltas even for a non-stream request.
                if content is None and isinstance(message.get("reasoning"),
                                                  str):
                    return message["reasoning"]
            text = first.get("text")
            if isinstance(text, str):
                return text
    content = data.get("content")
    return content if isinstance(content, str) else None


def _server_error_detail_suffix(resp, limit: int = 260) -> str:
    """The server's own explanation, appended to a status-code message.

    Without this, a 401/403 threw away the one line that says *why* -- which
    is where the real cause usually is.
    """
    detail = _server_error_detail(resp, limit)
    if not detail or detail.startswith("{"):
        return ""
    return f" Server said: {detail}"


def _restricted_model_hint(detail: str) -> str:
    """Explain the common "this model is for someone else's app" 403.

    Providers like Cline hand out keys that authenticate fine but only work
    from their own client. The raw message names the model; the useful advice
    is to pick another one.
    """
    low = detail.lower()
    if "only available via" in low or "product surfaces" in low:
        return (" Pick a different model, or use a provider that allows "
                "third-party clients.")
    return ""


def _bad_model_hint(cfg: "AiConfig", detail: str) -> str:
    """Turn a "model format" complaint into something actionable.

    Some servers (vLLM-style) only accept ids of the form ``type/model``, e.g.
    ``mistralai/Mistral-7B-Instruct``. The server says only "invalid model
    format"; this adds what to actually type.
    """
    low = detail.lower()
    if "model" not in low:
        return ""
    if "format" not in low and "expected" not in low:
        return ""
    model = (cfg.model or "").strip()
    if "/" in model:
        return ""
    return (
        f" The server wants a model id of the form 'type/model' (for example "
        f"'mistralai/Mistral-7B-Instruct'), but the model is set to '{model}'. "
        f"Edit the model name and retry."
    )


def build_headers(cfg: AiConfig) -> dict[str, str]:
    """Request headers.

    Local servers usually need no key, so the Authorization header is omitted
    entirely rather than sent empty (some servers reject that).
    """
    headers = {"Content-Type": "application/json"}
    key = (cfg.api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


# ------------------------------------------------------------------ prompt --
_SYSTEM_PROMPT = """You are a Minecraft mod assistant inside a mod launcher.

You will be given the user's request and a CATALOGUE of real, verified mods
that are already compatible with their Minecraft version and mod loader.

Rules:
- ONLY ever recommend mods that appear in the catalogue, copying project_id
  and source exactly as written there. Never guess or invent an id.
- Recommend at most {max_results} mods, most relevant first.
- If the catalogue has nothing suitable, say so plainly and recommend none.
- You are given an INSTALLED list of what the user already has. NEVER claim a
  mod is already installed unless it appears in that INSTALLED list -- if you
  are unsure whether something is installed, just recommend it and say nothing
  about its status. Do not invent mods the user "already has".
- Keep the reply short, friendly and practical. No markdown headings.

Reply with ONLY a JSON object, no other text:
{{"reply": "your message to the user",
  "mods": [{{"project_id": "...", "source": "modrinth|curseforge",
             "reason": "one short sentence"}}]}}"""


def build_messages(
    history: list[dict],
    user_text: str,
    catalogue: list[dict],
    instance_label: str,
    max_results: int,
    installed: list[str] | None = None,
    project_type: str = PROJECT_TYPE_MOD,
) -> list[dict]:
    """Assemble the chat payload for one turn.

    *installed* is the real list of jar filenames already in the target's
    mods/plugins folder. Without it the model has no idea what the user has
    and happily claims mods are "already installed" -- so it is passed in
    explicitly.

    *project_type* ("mod" or "plugin") injects a hard rule into the system
    prompt: the catalogue holds ONLY that kind, so the model cannot swap a
    plugin in for a mod (or vice versa) even if the user's wording is loose.
    """
    kind = "PLUGINS" if project_type == PROJECT_TYPE_PLUGIN else "MODS"
    kind_lower = kind.lower()

    rows = [
        f'- {r.get("title")} | id={r.get("project_id")} | '
        f'source={r.get("source")} | by {r.get("author") or "?"} | '
        f'{(r.get("description") or "")[:180]}'
        for r in catalogue
    ]
    catalogue_text = ("\n".join(rows) if rows
                      else f"(no matching {kind_lower} were found)")

    names = [str(n) for n in (installed or []) if str(n).strip()]
    installed_text = ("\n".join(f"- {n}" for n in names) if names
                      else "(nothing installed yet)")

    system = _SYSTEM_PROMPT.format(max_results=max_results) + (
        f"\n\nTarget: {instance_label}\n"
        f"PROJECT TYPE: strictly {kind}. The CATALOGUE below contains only "
        f"{kind_lower}; recommend nothing else, whatever the user asks.\n"
        f"INSTALLED (already in the folder):\n{installed_text}\n"
        f"CATALOGUE:\n{catalogue_text}"
    )

    messages = [{"role": "system", "content": system}]
    for turn in history[-10:]:
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


# ------------------------------------------------------------- HTTP call ---
def chat(cfg: AiConfig, messages: list[dict],
         timeout: int = REQUEST_TIMEOUT) -> str:
    """POST one chat turn and return the raw assistant text.

    Runs on a worker thread. Raises :class:`AiError` with a message that is
    safe to show the user.
    """
    import requests

    url = normalise_endpoint(cfg.endpoint)
    payload = {
        "model": (cfg.model or "").strip(),
        "messages": messages,
        "temperature": float(cfg.temperature),
        "stream": False,
    }
    try:
        resp = requests.post(
            url, json=payload, headers=build_headers(cfg), timeout=timeout,
        )
    except requests.exceptions.ConnectTimeout:
        raise AiError(
            f"No response from {url} within {timeout}s. Is the server "
            "running?") from None
    except requests.exceptions.ReadTimeout:
        raise AiError("The model took too long to answer. Try a smaller "
                      "model.") from None
    except requests.exceptions.ConnectionError:
        raise AiError(
            f"Cannot reach {url}. Check the endpoint and that the server is "
            "running.") from None
    except requests.RequestException as exc:
        raise AiError(f"Request failed: {exc}") from None

    if resp.status_code == 401:
        raise AiError("The API key was rejected (401): it is missing, "
                      "expired, or typed wrong."
                      + _server_error_detail_suffix(resp))
    if resp.status_code == 403:
        # NOT a bad key -- the key authenticated fine, this account just isn't
        # allowed to use this model/endpoint. Saying "rejected the API key"
        # here sent the user hunting for a key problem that did not exist.
        raise AiError(
            "The key is valid but this request is not allowed (403)."
            + _server_error_detail_suffix(resp)
            + _restricted_model_hint(_server_error_detail(resp)))
    if resp.status_code == 404:
        raise AiError(f"{url} returned 404. Check the endpoint path.")
    if resp.status_code >= 400:
        detail = _server_error_detail(resp)
        raise AiError(f"Server error {resp.status_code}: {detail}"
                      + _bad_model_hint(cfg, detail))

    try:
        data = resp.json()
    except ValueError:
        raise AiError(f"{url} returned a non-JSON response.") from None

    # Some gateways answer HTTP 200 and put the failure in the body. Detect
    # that before trying to read a reply that is not there.
    if isinstance(data, dict) and data.get("error"):
        detail = _server_error_detail(resp)
        raise AiError(f"The server returned an error: {detail}"
                      + _restricted_model_hint(detail))

    text = extract_reply(data)
    if text is None:
        raise AiError("The model replied in a format the launcher does not "
                      "understand." + _short_body_hint(data))
    return text


# ------------------------------------------------------------- responses ---
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_reply(text: str) -> dict:
    """Pull ``{reply, mods}`` out of a model response, tolerantly.

    Small local models wrap JSON in prose or code fences, so fall back to
    locating the outermost brace pair. Always returns a dict; ``reply`` is ''
    and ``mods`` [] when nothing usable was found.
    """
    raw = (text or "").strip()
    if not raw:
        return {"reply": "", "mods": []}

    candidates: list[str] = []
    fenced = _FENCE_RE.search(raw)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start:end + 1])

    for blob in candidates:
        try:
            obj = json.loads(blob.strip())
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return {
                "reply": str(obj.get("reply") or "").strip(),
                "mods": [m for m in (obj.get("mods") or [])
                         if isinstance(m, dict)],
            }
    # Not JSON at all -- show the model's words rather than dropping them.
    return {"reply": raw, "mods": []}


def validate_picks(picks: list[dict], catalogue: list[dict]) -> list[dict]:
    """Keep only mods the model named that really exist in *catalogue*.

    This is the hallucination guard: an id the model made up is discarded, and
    surviving entries are the catalogue rows themselves (real icon, real title,
    real project id) rather than anything the model supplied.
    """
    index = {str(r.get("project_id")): r for r in catalogue
             if r.get("project_id")}
    out: list[dict] = []
    seen: set[str] = set()
    for pick in picks:
        key = str(pick.get("project_id") or "").strip()
        row = index.get(key)
        if row is None or key in seen:
            continue
        seen.add(key)
        enriched = dict(row)
        reason = str(pick.get("reason") or "").strip()
        if reason:
            enriched["ai_reason"] = reason
        out.append(enriched)
    return out


_STOP_WORDS = {
    "a", "an", "the", "for", "to", "of", "and", "or", "with", "that", "this",
    "it", "i", "want", "need", "please", "can", "you", "me", "my", "is", "are",
    "be", "some", "any", "add", "install", "get", "mods", "mod", "give", "find",
    "recommend", "looking", "look", "would", "like",
}

# Verbs and question scaffolding. These survived stop-word stripping and ended
# up *inside* the query -- "can you find me a mod that adds shaders" searched
# for the literal string "adds shaders", which matches almost nothing.
_VERBS = {
    "adds", "add", "adding", "installs", "installing", "installed", "download",
    "downloads", "downloading", "gets", "getting", "finds", "finding", "shows",
    "showing", "searches", "searching", "wants", "needs", "suggests",
    "suggesting", "recommends", "gives", "helps", "could", "should", "there",
    "something", "anything", "thing", "things", "stuff", "pack", "modpack",
    "which", "what", "when", "where", "who", "how", "why", "does", "did",
    "work", "works", "working", "run", "runs", "running", "use", "uses",
    "using", "used", "make", "makes", "making", "let", "lets", "help",
    "improves", "improve", "improving", "increase", "increases", "reduce",
    "reduces", "boost", "boosts", "enhance", "fix", "fixes", "enable",
    "enables", "provides", "provide", "supports", "support", "need", "want",
    "do", "have", "has", "had", "you", "me", "my", "your", "am", "let",
    "know", "knows", "think", "any", "all", "one", "two", "get", "put",
}

# Words that are the *subject* of the question and must never be stripped --
# they are often exactly what the user is looking for.
_KEEP = {"shaders", "shader", "performance", "inventory", "minimap", "map",
         "world", "generation", "rendering", "lighting", "combat", "mobs",
         "blocks", "items", "armor", "storage", "quest", "quests", "crafting",
         "recipe", "recipes", "teleport", "hud", "sound", "weather", "animals",
         "vehicles", "boats", "currency", "economy", "chat", "sodium",
         "iris", "lithium", "ferrite", "cloth", "jei", "emi", "rei"}


def search_query(user_text: str) -> str:
    """Turn a chat message into a search string for the mod APIs.

    The previous version only stripped articles and pronouns, so an ordinary
    question like "can you find me a mod that adds shaders" searched for
    "adds shaders" -- matching nothing. Verbs and scaffolding are dropped here
    too, leaving the words that actually describe the mod.
    """
    words = re.findall(r"[\w+#.-]+", user_text or "")
    # A _KEEP word is the subject of the question and is always kept; anything
    # else has to survive the verb/stop-word filters.
    kept = [w for w in words
            if w.lower() in _KEEP
            or (w.lower() not in _VERBS
                and w.lower() not in _STOP_WORDS
                and len(w) > 1)]
    if not kept:
        # Everything was scaffolding ("install mods please"): retry without the
        # verb list, and only then fall back to the raw prompt.
        kept = [w for w in words
                if w.lower() not in _STOP_WORDS and len(w) > 1]
    if not kept:
        # Nothing usable at all. Still bound it -- the raw prompt is usually
        # 20+ words of filler and no search API wants that.
        return " ".join(words[:6])
    return " ".join(kept[:6])


def test_connection(cfg: AiConfig, timeout: int = 15) -> str:
    """Probe the endpoint with a one-token request. Returns a status line."""
    reply = chat(cfg, [{"role": "user", "content": "ping"}], timeout=timeout)
    return (reply or "").strip() or "(empty reply)"