"""
Network worker threads for accounts and mods.

  * :class:`MsLoginThread` -- full interactive Microsoft login:
    PKCE login data -> system browser -> local HTTP redirect capture ->
    token exchange. The refresh token is returned in the profile dict and
    must be stored via AccountStore.add_microsoft (encrypted vault).
  * :class:`ElyLoginThread` -- Ely.by OAuth2 device-code login (no redirect
    server needed): fetch a device code, show the short user code, poll
    until the user authorizes in the browser, then fetch the profile.
  * :class:`ModSearchThread` / :class:`ModInstallThread` -- Modrinth and
    CurseForge search/installation off the GUI thread.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from PyQt6.QtCore import QThread, pyqtSignal

from .accounts import AuthError
from .constants import (
    ELY_SCOPES,
    MS_LOGIN_PORT,
    MS_LOGIN_TIMEOUT_SECONDS,
    MS_REDIRECT_URI,
    PROJECT_TYPE_MOD,
    PROJECT_TYPE_PLUGIN,
    SOURCE_CURSEFORGE,
    SOURCE_MODRINTH,
)
from . import elyby
from . import moddb
from . import ai_client


class _RedirectCapture(BaseHTTPRequestHandler):
    """Tiny handler that captures ?code=... from the OAuth redirect."""

    captured_url: str | None = None
    server_ref: "RedirectServer | None" = None  # set per-server instance

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).captured_url = self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Login complete.</h2>"
            b"<p>You can close this window and return to the launcher.</p>"
            b"</body></html>"
        )
        if self.server_ref is not None:
            self.server_ref.stop_waiting()

    def log_message(self, *args) -> None:  # silence default stderr logging
        pass


class RedirectServer(HTTPServer):
    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _RedirectCapture)
        _RedirectCapture.server_ref = self
        self._wait_event = threading.Event()

    def stop_waiting(self) -> None:
        self._wait_event.set()

    def captured(self) -> str | None:
        return _RedirectCapture.captured_url

    def wait_for_redirect(self, timeout: float) -> bool:
        return self._wait_event.wait(timeout)


class MsLoginThread(QThread):
    """
    Runs the whole interactive Microsoft login in the background:

    1. Generate PKCE login data (verifier + state).
    2. Serve a one-shot local HTTP listener for the redirect.
    3. Open the system browser at the authorization URL.
    4. Wait for the redirect, validate state, exchange the code.

    Emits ``succeeded(profile_dict)`` -- the caller MUST persist it via
    :meth:`launcher.accounts.AccountStore.add_microsoft`.
    """

    status = pyqtSignal(str)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, client_id: str, parent=None) -> None:
        super().__init__(parent)
        self._client_id = client_id
        self._server: RedirectServer | None = None
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        if self._server is not None:
            self._server.stop_waiting()

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            from minecraft_launcher_lib import microsoft_account as mll_ms

            if not self._client_id:
                raise AuthError(
                    "No Azure Client ID configured. Add your app's Client "
                    "ID in Settings first (portal.azure.com -> App "
                    "registrations, redirect URI "
                    f"{MS_REDIRECT_URI})."
                )

            self.status.emit("Generating secure login data...")
            login_url, state, code_verifier = mll_ms.get_secure_login_data(
                self._client_id, MS_REDIRECT_URI
            )
            # Surface the raw URL so users can paste it into an incognito
            # window (or another browser) to escape stale sessions and
            # guarantee they pick the intended account.
            self.status.emit(f"LOGIN_URL:{login_url}")

            _RedirectCapture.captured_url = None
            self._server = RedirectServer(MS_LOGIN_PORT)
            server_thread = threading.Thread(
                target=self._server.serve_forever, daemon=True
            )
            server_thread.start()

            self.status.emit("Opening browser for Microsoft login...")
            webbrowser.open(login_url)
            self.status.emit("Waiting for login to finish...")

            got = self._server.wait_for_redirect(MS_LOGIN_TIMEOUT_SECONDS)
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None

            if self._cancelled.is_set():
                raise AuthError("Login cancelled.")
            if not got or _RedirectCapture.captured_url is None:
                raise AuthError("Login timed out - please try again.")

            redirect_url = _RedirectCapture.captured_url
            if "error=" in redirect_url:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)
                raise AuthError(
                    f"Microsoft returned an error: {qs.get('error_description', qs.get('error'))}"
                )

            auth_code = mll_ms.parse_auth_code_url(redirect_url, state)

            self.status.emit("Exchanging tokens with Microsoft...")
            profile = mll_ms.complete_login(
                client_id=self._client_id,
                client_secret=None,
                redirect_uri=MS_REDIRECT_URI,
                auth_code=auth_code,
                code_verifier=code_verifier,
            )
            self.succeeded.emit(dict(profile))
        except AuthError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ElyLoginThread(QThread):
    """
    Ely.by OAuth2 device-code login.

    1. Ask ely.by for a device code (``POST /api/oauth2/v1/devicecode``).
    2. Emit ``device_code(verification_uri, user_code)`` so the UI can show
       the short code and open the verification page.
    3. Poll the token endpoint until the user authorizes (or the code
       expires / is denied).
    4. Fetch the account profile and emit ``succeeded(profile_dict)`` with
       ``{uuid, username, access_token, refresh_token}``; the caller must
       persist it via :meth:`launcher.accounts.AccountStore.add_elyby`.
    """

    status = pyqtSignal(str)
    device_code = pyqtSignal(str, str)  # (verification_uri, user_code)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, client_id: str, client_secret: str = "", parent=None) -> None:
        super().__init__(parent)
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            if not self._client_id:
                raise AuthError(
                    "No Ely.by client ID configured. Enter one in Settings "
                    "or clear the override to use the built-in value."
                )
            self.status.emit("Requesting code from Ely.by...")
            device = elyby.request_device_code(self._client_id, ELY_SCOPES)
            verification_uri = str(
                device.get("verification_uri") or "http://account.ely.by/code"
            )
            user_code = str(device.get("user_code", ""))
            interval = max(1, int(device.get("interval", 5)))
            expires = int(device.get("expires_in", 600))
            self.device_code.emit(verification_uri, user_code)
            self.status.emit(
                f"Open {verification_uri} and enter code {user_code}..."
            )

            deadline = time.monotonic() + expires
            tokens = None
            while time.monotonic() < deadline:
                if self._cancelled.is_set():
                    raise AuthError("Ely.by login cancelled.")
                tokens = elyby.poll_access_token(
                    self._client_id, device["device_code"], self._client_secret
                )
                if tokens is not None:
                    break
                time.sleep(interval)
            if tokens is None:
                raise AuthError("Ely.by login timed out - please try again.")

            access_token = str(tokens.get("access_token", ""))
            refresh_token = str(tokens.get("refresh_token", ""))
            if not access_token:
                raise AuthError("Ely.by returned no access token.")
            if not refresh_token:
                raise AuthError(
                    "Ely.by did not issue a refresh token (offline_access "
                    "scope missing)."
                )

            self.status.emit("Fetching Ely.by profile...")
            info = elyby.fetch_user_info(access_token)
            account_uuid = str(info.get("uuid", info.get("id", "")))
            username = str(info.get("username", ""))
            if not account_uuid or not username:
                raise AuthError("Ely.by profile incomplete.")

            self.succeeded.emit(
                {
                    "uuid": account_uuid,
                    "username": username,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                }
            )
        except elyby.ElyByError as exc:
            self.failed.emit(str(exc))
        except AuthError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# Mod search / installation
# ---------------------------------------------------------------------------
class MrpackInstallThread(QThread):
    """Installs a Modrinth .mrpack modpack into an instance directory."""

    status = pyqtSignal(str)
    progress_max = pyqtSignal(int)
    progress_value = pyqtSignal(int)
    succeeded = pyqtSignal(str)   # launchable version id
    failed = pyqtSignal(str)

    def __init__(self, pack_path: str, minecraft_directory: str, parent=None) -> None:
        super().__init__(parent)
        self._pack_path = pack_path
        self._mc_dir = minecraft_directory

    def run(self) -> None:  # noqa: D102
        try:
            from minecraft_launcher_lib import mrpack as mll_mrpack

            mll_mrpack.install_mrpack(
                self._pack_path,
                self._mc_dir,
                callback={
                    "setStatus": lambda t: self.status.emit(str(t)),
                    "setProgress": lambda v: self.progress_value.emit(int(v)),
                    "setMax": lambda v: self.progress_max.emit(max(1, int(v))),
                },
            )
            self.succeeded.emit(
                mll_mrpack.get_mrpack_launch_version(self._pack_path)
            )
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ModSearchThread(QThread):
    """Searches Modrinth or CurseForge for mods matching the instance."""

    ready = pyqtSignal(int, list)      # generation, list[dict] result rows
    failed = pyqtSignal(int, str)      # generation, error text

    def __init__(
        self,
        source: str,
        query: str,
        game_version: str,
        loader: str,
        curseforge_api_key: str = "",
        limit: int = 20,
        offset: int = 0,
        project_type: str = "mod",
        parent=None,
        generation: int = 0,
    ) -> None:
        super().__init__(parent)
        self._source = source
        self._query = query
        self._game_version = game_version
        self._loader = loader
        self._cf_key = curseforge_api_key
        self._limit = limit
        self._offset = offset
        self._project_type = project_type
        # Cooperative cancellation: retire() sets this before quit()+wait()
        # so a replaced search abandons its result instead of emitting it.
        self._cancelled = False
        self.generation = generation

    def cancel(self) -> None:
        """Ask the search to stop; honoured before emitting any result."""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # noqa: D102
        try:
            if self._source == SOURCE_CURSEFORGE:
                results = moddb.search_curseforge(
                    self._cf_key, self._query, self._game_version, self._loader,
                    limit=self._limit, offset=self._offset
                )
            else:
                # SOURCE_PLUGINS (and modrinth) go through the same call;
                # project_type is forced into the API facets so a plugin
                # search can never return a mod (and vice versa).
                results = moddb.search_modrinth(
                    self._query, self._game_version, self._loader,
                    limit=self._limit, offset=self._offset,
                    project_type=self._project_type,
                )
            if self._cancelled:
                return  # superseded -- the new search owns the UI now
            self.ready.emit(self.generation, results)
        except Exception as exc:
            if self._cancelled:
                return
            self.failed.emit(self.generation, f"{type(exc).__name__}: {exc}")


class ModInstallThread(QThread):
    """Downloads a mod (plus Modrinth dependencies) into an instance."""

    status = pyqtSignal(str)
    succeeded = pyqtSignal(list)  # installed file names
    failed = pyqtSignal(str)

    def __init__(
        self,
        source: str,
        project_id: str,
        game_version: str,
        loader: str,
        mods_dir: str,
        curseforge_api_key: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._source = source
        self._project_id = project_id
        self._game_version = game_version
        self._loader = loader
        self._mods_dir = mods_dir
        self._cf_key = curseforge_api_key

    def run(self) -> None:  # noqa: D102
        try:
            os.makedirs(self._mods_dir, exist_ok=True)
            if self._source == SOURCE_CURSEFORGE:
                installed = moddb.install_curseforge(
                    self._project_id,
                    self._game_version,
                    self._cf_key,
                    self._mods_dir,
                    progress=self.status.emit,
                )
            else:
                installed = moddb.install_modrinth(
                    self._project_id,
                    self._game_version,
                    self._loader,
                    self._mods_dir,
                    progress=self.status.emit,
                )
            self.succeeded.emit(installed)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# AI mod installer
# ---------------------------------------------------------------------------

import re as _re

# Words that carry no topical signal when scoring how well a catalogue row
# matches the request. Kept small and obvious; anything else counts.
_RANK_STOP = {
    "a", "an", "the", "for", "to", "of", "and", "or", "with", "that", "this",
    "it", "i", "want", "need", "please", "can", "you", "me", "my", "is", "are",
    "be", "some", "any", "add", "adds", "added", "install", "installs",
    "get", "mods", "mod", "give", "find", "recommend", "looking", "would",
    "like", "more", "better", "improve", "improves", "improved", "improving",
    "make", "makes", "into", "onto", "very", "really", "good", "nice", "cool",
    "please", "thanks", "hey", "hello", "show", "shows", "something",
    "anything", "thing", "things", "stuff", "help", "helps",
}


def _rank_by_relevance(rows: list[dict], user_text: str) -> list[dict]:
    """Order *rows* by how strongly each matches *user_text* (stable).

    Title hits weigh most, then description, then the slug. Rows with no
    keyword overlap sink to the bottom (keeping their original order among
    themselves) so the off-topic popular mods that the search APIs insist on
    returning fall out of the capped catalogue the model sees.

    A request with no usable words (\"install mods\") leaves the order alone.
    """
    words = {
        w for w in _re.findall(r"[a-z0-9]+", (user_text or "").lower())
        if len(w) > 2 and w not in _RANK_STOP
    }
    if not words:
        return rows

    def score(row: dict) -> int:
        title = str(row.get("title") or "").lower()
        desc = str(row.get("description") or "").lower()
        slug = str(row.get("slug") or "").lower()
        total = 0
        for w in words:
            if w in title:
                total += 6
            if w in slug:
                total += 3
            if w in desc:
                total += 1
        return total

    # Stable sort: equal scores keep the API's original (relevance) order.
    return sorted(rows, key=lambda r: -score(r))


class AiModsThread(QThread):
    """Answers one AI turn: search the mod APIs, then ask the model to choose.

    Runs entirely off the GUI thread. Two network round trips happen here --
    searching Modrinth/CurseForge and calling the LLM -- so neither can ever
    block the UI.

    The launcher searches first and hands the model a verified catalogue;
    :func:`ai_client.validate_picks` then discards any project id the model
    invented. ``ready`` therefore only ever carries mods that really exist and
    are compatible with the selected instance.
    """

    status = pyqtSignal(str)                 # progress text for the UI
    ready = pyqtSignal(str, list)            # reply text, validated mod rows
    failed = pyqtSignal(str)

    def __init__(
        self,
        config,              # ai_client.AiConfig
        user_text: str,
        history: list[dict],
        game_version: str,
        loader: str,
        instance_label: str,
        curseforge_api_key: str = "",
        installed: list[str] | None = None,
        project_type: str = PROJECT_TYPE_MOD,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._user_text = user_text
        self._history = list(history or [])
        self._game_version = game_version
        self._loader = loader
        self._instance_label = instance_label
        self._cf_key = curseforge_api_key
        # What is really in the mods/plugins folder. Without this the model
        # invents "you already have this mod" claims out of nothing.
        self._installed = list(installed or [])
        # Strict mode: "plugin" searches only Modrinth's plugin catalogue and
        # forces the prompt to only ever recommend plugins (and vice versa).
        self._project_type = (
            PROJECT_TYPE_PLUGIN
            if project_type == PROJECT_TYPE_PLUGIN
            else PROJECT_TYPE_MOD
        )
        self._cancelled = False

    @property
    def kind(self) -> str:
        """'plugins' in plugin mode, 'mods' otherwise (for status texts)."""
        return ("plugins" if self._project_type == PROJECT_TYPE_PLUGIN
                else "mods")

    def cancel(self) -> None:
        """Ask the turn to stop; honoured between the two network calls."""
        self._cancelled = True

    # -- steps ----------------------------------------------------------------
    def _search(self) -> list[dict]:
        """Query the sources, merging results (Modrinth wins ties).

        Plugin mode is strict: ONLY Modrinth's plugin catalogue is queried
        (CurseForge has no Minecraft plugin class), with project_type forced
        into the API facets so a mod can never leak into the results.

        The APIs match the whole query string, so a multi-word query like
        "more nature plants biomes" can return nothing even though each word
        alone matches plenty. When the full query comes back empty, retry
        with fewer words (word-by-word, then pairs) and merge -- this is a
        recall widen, not a type widen: every retry keeps the same version,
        loader and project_type facets.
        """
        query = ai_client.search_query(self._user_text)
        plugin_mode = self._project_type == PROJECT_TYPE_PLUGIN

        self.status.emit(
            f"Searching {self.kind} sites for '{query}'...")

        def _once(q: str) -> tuple[list[dict], list[dict]]:
            mr: list[dict] = []
            try:
                mr = moddb.search_modrinth(
                    q, self._game_version, self._loader, limit=15,
                    project_type=self._project_type)
            except Exception as exc:  # noqa: BLE001 - one source failing is fine
                self.status.emit(f"Modrinth search failed: {exc}")
            if self._cancelled:
                return [], []
            cf: list[dict] = []
            if not plugin_mode:
                # CurseForge hosts no Minecraft plugin catalogue, so plugin
                # mode deliberately skips it instead of silently searching
                # mods.
                try:
                    cf = moddb.search_curseforge(
                        self._cf_key, q, self._game_version, self._loader,
                        limit=15,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.status.emit(f"CurseForge search failed: {exc}")
            return mr, cf

        mr, cf = _once(query)
        if not mr and not cf and not self._cancelled:
            words = [w for w in query.split() if len(w) > 2]
            fallbacks: list[str] = list(words)
            fallbacks += [f"{a} {b}" for a, b in zip(words, words[1:])]
            seen_q = {query}
            for q in fallbacks:
                if q in seen_q:
                    continue
                seen_q.add(q)
                self.status.emit(f"No hits -- retrying with '{q}'...")
                mr2, cf2 = _once(q)
                mr += mr2
                cf += cf2
                if mr or cf or self._cancelled:
                    break

        if self._cancelled:
            return []

        rows: list[dict] = []
        seen: set[tuple] = set()
        for row in [*mr, *cf]:
            key = (row.get("source"), str(row.get("project_id")))
            if key in seen or not row.get("project_id"):
                continue
            seen.add(key)
            rows.append(row)

        # Re-rank by relevance to what the user actually asked for BEFORE the
        # cap. The APIs return their own relevance blend, which happily mixes
        # in hugely popular but off-topic mods (e.g. "Entity Culling" for a
        # "nature" request). Scoring title/description/categories against the
        # query words pushes genuinely matching mods up and shoves the noise
        # out of the 25-row window the model sees -- so the AI stops picking
        # performance mods when the user wanted nature.
        rows = _rank_by_relevance(rows, self._user_text)

        # Give the model a manageable catalogue.
        return rows[:25]

    def run(self) -> None:  # noqa: D102
        try:
            catalogue = self._search()
            if self._cancelled:
                self.ready.emit("", [])
                return

            if not catalogue:
                self.status.emit(
                    f"No matching {self.kind} found on the mod sites.")
                singular = ("plugin"
                            if self._project_type == PROJECT_TYPE_PLUGIN
                            else "mod")
                self.ready.emit(
                    f"I couldn't find any {self.kind} matching that. Try "
                    f"naming the {singular} or the feature you want (e.g. "
                    "'performance', 'inventory', 'shaders').", [])
                return

            self.status.emit(
                f"Asking the AI about {len(catalogue)} {self.kind}...")
            messages = ai_client.build_messages(
                self._history, self._user_text, catalogue,
                self._instance_label, self._config.max_results,
                installed=self._installed,
                project_type=self._project_type,
            )
            raw = ai_client.chat(self._config, messages)
            if self._cancelled:
                self.ready.emit("", [])
                return

            parsed = ai_client.parse_reply(raw)
            picks = ai_client.validate_picks(parsed.get("mods", []), catalogue)
            reply = parsed.get("reply") or ""

            dropped = len(parsed.get("mods", [])) - len(picks)
            if dropped > 0:
                self.status.emit(
                    f"Ignored {dropped} suggestion(s) that did not exist.")
            if not picks and not reply:
                reply = "I couldn't make a suggestion for that."
            self.ready.emit(reply, picks)
        except ai_client.AiError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - never kill the thread raw
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Description rendering helpers (run on the worker thread, never on the GUI)
# ---------------------------------------------------------------------------
def render_description(desc: str, source: str) -> str:
    """Convert a raw project description into displayable HTML.

    Modrinth returns markdown; CurseForge already returns HTML. Runs on the
    worker thread because markdown rendering is not free on long descriptions.
    """
    import html as _html

    if source == SOURCE_MODRINTH:
        try:
            import markdown as _md

            return _md.markdown(desc, extensions=["extra"])
        except Exception:  # noqa: BLE001 - markdown is optional
            return f"<pre>{_html.escape(desc)}</pre>"
    return desc or ""


def embed_images(body_html: str, timeout: int = 10,
                 max_workers: int = 6) -> str:
    """Replace remote ``<img>`` sources with inline data URIs.

    Runs off the GUI thread (see :class:`ModDescriptionThread`). Images that
    fail to download are left as-is so the browser can still try them, rather
    than blanking out the description.

    Note the executor is deliberately *not* used as a context manager: the
    ``with`` form blocks on ``shutdown(wait=True)`` until the slowest image
    finishes, which is exactly the freeze this function used to cause.
    """
    import re as _re

    imgs = _re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', body_html)
    imgs = [u for u in imgs if not u.startswith("data:")]
    if not imgs:
        return body_html

    def fetch(url: str):
        try:
            import base64

            import requests

            r = requests.get(url, timeout=timeout,
                              headers={"User-Agent": "Cove/1.1"})
            if r.status_code == 200 and r.content:
                ctype = r.headers.get("Content-Type", "image/png")
                return url, base64.b64encode(r.content).decode("ascii"), ctype
        except Exception:  # noqa: BLE001 - images are best-effort
            pass
        return url, None, None

    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = [executor.submit(fetch, u) for u in set(imgs)]
        for fut in concurrent.futures.as_completed(futures):
            try:
                url, b64, ctype = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if b64:
                body_html = body_html.replace(
                    url, f"data:{ctype};base64,{b64}"
                )
    finally:
        executor.shutdown(wait=True)
    return body_html


class ModDescriptionThread(QThread):
    """Fetches a mod's full description from Modrinth/CurseForge.

    All of the expensive work happens here, off the GUI thread: the HTTP fetch,
    the markdown->HTML conversion, and inlining every remote image as a data
    URI. :attr:`ready` therefore emits *finished HTML*, so the GUI slot only
    has to call ``setHtml`` -- previously the slot re-downloaded every image on
    the GUI thread and froze the whole app for as long as the slowest one took.
    """

    ready = pyqtSignal(str)  # fully rendered body HTML (images inlined)
    failed = pyqtSignal(str)

    def __init__(
        self,
        source: str,
        project_id: str,
        curseforge_api_key: str = "",
        slug: str = "",
        parent=None,
        generation: int = 0,
    ) -> None:
        super().__init__(parent)
        self._source = source
        self._project_id = project_id
        self._cf_key = curseforge_api_key
        self._slug = slug
        self._cancelled = False
        self.generation = generation

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:
        try:
            if self._source == SOURCE_CURSEFORGE:
                try:
                    desc = moddb.get_curseforge_description(self._cf_key, self._project_id)
                except Exception:
                    # Fallback: try scraping the public CurseForge page using slug
                    desc = ""
                    if self._slug:
                        try:
                            import requests
                            url = f"https://www.curseforge.com/minecraft/mc-mods/{self._slug}"
                            resp = requests.get(url, headers={"User-Agent": "Cove/1.1"}, timeout=15)
                            resp.raise_for_status()
                            html = resp.text
                            # Try to extract the project description container
                            m = __import__('re').search(r'<div[^>]+class=["\']?project-description["\']?[^>]*>(.*?)</div>', html, __import__('re').S)
                            if m:
                                desc = m.group(1)
                            else:
                                desc = html
                        except Exception:
                            desc = ""
            else:
                desc = moddb.get_modrinth_description(self._project_id)
            # Render + inline images here, on the worker, so the GUI slot that
            # receives this signal has nothing slow left to do.
            body = render_description(desc, self._source)
            if self._cancelled:
                return
            self.ready.emit(embed_images(body))
        except Exception as exc:
            if self._cancelled:
                return
            self.failed.emit(f"{type(exc).__name__}: {exc}")


