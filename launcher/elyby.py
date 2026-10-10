"""
Ely.by authentication client (OAuth2 device-code flow).

Ely.by (https://ely.by) is a Mojang-compatible authentication service.  We
log in through their OAuth2 *device code* flow -- the user opens the returned
verification URI in any browser, types the short code shown by the launcher,
and ely.by mints a bearer token for us:

    1. ``POST /api/oauth2/v1/devicecode``   (client_id, scope)
       -> ``{user_code, device_code, verification_uri, expires_in, interval}``
    2. ``POST /api/oauth2/v1/token``        poll with ``device_code``
       -> ``authorization_pending`` until the user confirms, then
          ``{access_token, refresh_token, token_type, expires_in}``
    3. ``GET  /api/account/v1/info``        with ``Authorization: Bearer``
       -> ``{id, uuid, username, ...}``

Requested with the ``minecraft_server_session`` scope, the OAuth access token
doubles as the Minecraft session token (authlib-injector is still required at
launch so the game validates it against ely.by instead of Mojang -- see
:func:`ensure_authlib_injector`).

This module is Qt-free and stdlib-only; the blocking UI lives in
:class:`launcher.workers_net.ElyLoginThread`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .constants import (
    APP_DATA_DIR,
    ELY_ACCOUNT_INFO_URL,
    ELY_AUTHLIB_INJECTOR_FILENAME,
    ELY_AUTHLIB_INJECTOR_URL,
    ELY_DEVICE_CODE_URL,
    ELY_DEVICE_GRANT,
    ELY_TOKEN_URL,
    SKINSYSTEM_URL,
)

_UA = {
    "User-Agent": "Cove-Launcher/1.1 (Minecraft launcher; +https://ely.by)",
    "Accept": "application/json",
}

# Non-terminal device-flow errors from the token endpoint: keep polling.
_PENDING_ERRORS = frozenset({"authorization_pending", "slow_down"})


class ElyByError(Exception):
    """Raised for any ely.by API / network / device-flow problem."""


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------
def _request(
    url: str,
    *,
    method: str = "GET",
    form: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30,
) -> tuple[int, dict]:
    """Perform a request and return ``(status, json_body)``.

    HTTP errors (4xx/5xx) are *not* raised -- the caller decides how to react
    to an ``authorization_pending`` versus a terminal OAuth error.  Network
    failures still raise :class:`ElyByError`.
    """
    req_headers = dict(_UA)
    if headers:
        req_headers.update(headers)
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs
            raw = resp.read().decode("utf-8", "replace")
            parsed = json.loads(raw) if raw.strip() else {}
            return resp.status, (parsed if isinstance(parsed, dict) else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {"message": raw[:300]}
        return exc.code, (parsed if isinstance(parsed, dict) else {"message": raw[:300]})
    except urllib.error.URLError as exc:
        raise ElyByError(f"Network error talking to ely.by: {exc.reason}") from exc
# ---------------------------------------------------------------------------
# OAuth2 device-code flow
# ---------------------------------------------------------------------------
def request_device_code(client_id: str, scope: str) -> dict:
    """Start a device-code login; returns the server's device code payload."""
    status, body = _request(
        ELY_DEVICE_CODE_URL,
        method="POST",
        form={"client_id": client_id, "scope": scope},
    )
    if status != 200 or not body.get("device_code"):
        raise ElyByError(
            body.get("message")
            or body.get("error_description")
            or f"Could not start Ely.by login (HTTP {status})."
        )
    return body


def poll_access_token(client_id: str, device_code: str, client_secret: str = "") -> dict | None:
    """Poll the token endpoint once.

    Returns ``None`` while the user has not finished authorizing yet
    (``authorization_pending`` / ``slow_down``) and the token dict once
    approved. Raises :class:`ElyByError` for terminal failures (expired code,
    access denied, ...).
    """
    form: dict = {
        "grant_type": ELY_DEVICE_GRANT,
        "client_id": client_id,
        "device_code": device_code,
    }
    if client_secret:
        form["client_secret"] = client_secret
    status, body = _request(ELY_TOKEN_URL, method="POST", form=form)
    if status == 200 and body.get("access_token"):
        return body
    error = str(body.get("error", "invalid_request"))
    if error in _PENDING_ERRORS:
        return None
    raise ElyByError(
        body.get("message")
        or body.get("error_description")
        or f"Ely.by login was denied ({error})."
    )


def refresh_access_token(
    client_id: str, refresh_token: str, scope: str, client_secret: str = ""
) -> dict:
    """Exchange the long-lived refresh token for a fresh access token."""
    form: dict = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
        "scope": scope,
    }
    if client_secret:
        form["client_secret"] = client_secret
    status, body = _request(ELY_TOKEN_URL, method="POST", form=form)
    if status != 200 or not body.get("access_token"):
        raise ElyByError(
            body.get("message")
            or body.get("error_description")
            or "Ely.by token refresh failed."
        )
    return body


def fetch_user_info(access_token: str) -> dict:
    """Fetch ``{id, uuid, username, ...}`` for the authorized account."""
    status, body = _request(
        ELY_ACCOUNT_INFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if status != 200 or not body.get("username"):
        raise ElyByError(
            body.get("message") or "Could not load the Ely.by profile."
        )
    return body
# ---------------------------------------------------------------------------
# authlib-injector (runtime authlib patch for the game client)
# ---------------------------------------------------------------------------
def authlib_injector_path() -> str:
    """Absolute path of the cached authlib-injector jar (may not exist yet)."""
    return os.path.join(APP_DATA_DIR, ELY_AUTHLIB_INJECTOR_FILENAME)


def _resolve_authlib_download_url() -> str:
    """Resolve the real JAR asset URL from the GitHub releases API.

    Newer releases version the asset name (e.g. ``authlib-injector-1.2.8.jar``)
    so a static ``/latest/download/<name>`` URL would 404.  We query the API
    for the latest release and grab the first ``.jar`` asset's
    ``browser_download_url``.
    """
    req = urllib.request.Request(ELY_AUTHLIB_INJECTOR_URL, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed API URL
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise ElyByError(f"Could not query authlib-injector releases: {exc}") from exc

    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(".jar"):
            return str(asset["browser_download_url"])
    raise ElyByError("No .jar asset found in authlib-injector latest release.")


def ensure_authlib_injector(status=None) -> str:
    """Return a downloaded authlib-injector jar, fetching it on first use.

    ``status`` is an optional ``callable(str)`` for progress messages (used to
    feed a Qt signal on the worker thread). Returns the jar's absolute path.
    """
    path = authlib_injector_path()
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path

    os.makedirs(APP_DATA_DIR, exist_ok=True)
    if status:
        status("Downloading authlib-injector (needed for Ely.by auth)...")
    try:
        download_url = _resolve_authlib_download_url()
    except ElyByError:
        raise
    except Exception as exc:
        raise ElyByError(f"Could not resolve authlib-injector download URL: {exc}") from exc
    req = urllib.request.Request(download_url, headers=_UA)  # noqa: S310 - GitHub release asset URL
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as fh:  # noqa: PTH123 - launcher data dir
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
    except Exception as exc:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        raise ElyByError(f"Could not download authlib-injector: {exc}") from exc

    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ElyByError("authlib-injector download produced an empty file.")
    if status:
        status("authlib-injector ready.")
    return path


# ---------------------------------------------------------------------------
# Skin & cape upload (account skinsystem API)
# ---------------------------------------------------------------------------
def _multipart_encode(fields: dict, file_field: str, filename: str,
                      file_bytes: bytes, content_type: str = "image/png") -> tuple[bytes, str]:
    """Build a minimal ``multipart/form-data`` body + content-type header.

    Ely.by's private skinsystem API expects the image under a named field
    (``skin`` or ``cape``); ``fields`` carries any extra text parts (e.g.
    ``model``).  Kept dependency-free like the rest of this module.
    """
    boundary = "----CoveBoundary" + os.urandom(8).hex()
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks += [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
            str(value).encode("utf-8") + b"\r\n",
        ]
    chunks += [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
        file_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(chunks)
    header = f"multipart/form-data; boundary={boundary}"
    return body, header


def upload_skin(access_token: str, account_uuid: str, png_bytes: bytes,
                model: str = "classic") -> None:
    """Set the authenticated Ely.by account's skin server-side.

    ``model`` is ``"classic"`` (Steve) or ``"slim"`` (Alex).  Raises
    :class:`ElyByError` when ely.by rejects the upload (this private endpoint
    can refuse without a registered OAuth app, so callers should treat it as
    best-effort).
    """
    url = f"{SKINSYSTEM_URL}/skins/{account_uuid}"
    body, ctype = _multipart_encode({"model": model}, "skin", "skin.png", png_bytes)
    req = urllib.request.Request(
        url, data=body, method="PUT",
        headers={
            **dict(_UA),
            "Authorization": f"Bearer {access_token}",
            "Content-Type": ctype,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https URL
            if resp.status >= 300:
                raise ElyByError(
                    f"Ely.by rejected the skin upload (HTTP {resp.status})."
                )
    except urllib.error.HTTPError as exc:
        raise ElyByError(
            f"Ely.by rejected the skin upload "
            f"(HTTP {exc.code}): {_specimen_text(exc)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ElyByError(f"Network error uploading skin: {exc.reason}") from exc


def upload_cape(access_token: str, account_uuid: str, png_bytes: bytes) -> None:
    """Upload a cape for the authenticated Ely.by account (best-effort)."""
    url = f"{SKINSYSTEM_URL}/capes/{account_uuid}"
    body, ctype = _multipart_encode({}, "cape", "cape.png", png_bytes)
    req = urllib.request.Request(
        url, data=body, method="PUT",
        headers={
            **dict(_UA),
            "Authorization": f"Bearer {access_token}",
            "Content-Type": ctype,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https URL
            if resp.status >= 300:
                raise ElyByError(
                    f"Ely.by rejected the cape upload (HTTP {resp.status})."
                )
    except urllib.error.HTTPError as exc:
        raise ElyByError(
            f"Ely.by rejected the cape upload "
            f"(HTTP {exc.code}): {_specimen_text(exc)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ElyByError(f"Network error uploading cape: {exc.reason}") from exc


def _specimen_text(exc: "urllib.error.HTTPError") -> str:
    try:
        raw = exc.read().decode("utf-8", "replace")
        if not raw:
            return exc.reason or ""
        try:
            data = json.loads(raw)
            return str(data.get("message") or data.get("error") or raw)[:200]
        except json.JSONDecodeError:
            return raw.strip()[:200]
    except Exception:  # pragma: no cover - defensive
        return str(exc)