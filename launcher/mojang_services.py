"""
Microsoft "Minecraft Services" API client (skin + cape management).

This is the *official* Mojang/Microsoft services API used by the real
Minecraft Launcher and PrismLauncher for their built-in skin changer — it
works for any Microsoft account with **no mods required**.  An authenticated
Microsoft login yields a Minecraft bearer token (via
:meth:`launcher.accounts.AccountStore.ensure_access_token`) which authorises
these endpoints:

  * ``GET    /minecraft/profile``              -> {id, name, skins[], capes[]}
  * ``POST   /minecraft/profile/skins``        -> upload a skin (multipart file+variant)
  * ``DELETE /minecraft/profile/skins/active`` -> reset skin to Steve/Alex
  * ``PUT    /minecraft/profile/capes/active`` -> body {capeId} shows a cape
  * ``DELETE /minecraft/profile/capes/active`` -> hide the active cape

Every cape the player owns is returned by ``GET /minecraft/profile`` in the
``capes[]`` array, so the launcher can *detect* the user's capes and let them
pick one without mods.

This module is Qt-free and uses ``requests`` (same as :mod:`launcher.moddb`).
"""

from __future__ import annotations

import requests

from .constants import MINECRAFT_SERVICES_URL

_AI = "Cove-Launcher/1.1 (Minecraft launcher)"
_HEADERS = {"User-Agent": _AI, "Accept": "application/json"}


class MojangServiceError(Exception):
    """Raised when a Minecraft Services call fails (bad token / HTTP error)."""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _specimen(resp: requests.Response) -> str:
    """Best-guess user-facing reason from an error response body."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200] or resp.reason or "unknown error"
    message = (
        data.get("errorMessage")
        or data.get("developerMessage")
        or data.get("error")
        or data.get("errorType")
    )
    return str(message).strip() or f"HTTP {resp.status_code}"


def _check(resp: requests.Response, context: str) -> None:
    if resp.status_code < 200 or resp.status_code >= 300:
        raise MojangServiceError(
            f"{context} failed ({resp.status_code}): {_specimen(resp)}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_profile(token: str, timeout: float = 30) -> dict:
    """Fetch the authenticated player's profile (id, name, skins, capes).

    The ``capes`` list contains every cape the user owns, each with
    ``id`` / ``state`` (``ACTIVE`` | ``INACTIVE``) / ``url`` / ``alias``.
    """
    resp = requests.get(
        f"{MINECRAFT_SERVICES_URL}/minecraft/profile",
        headers={**_HEADERS, **_bearer(token)},
        timeout=timeout,
    )
    _check(resp, "Fetch profile")
    data = resp.json()
    return {
        "id": data.get("id", ""),
        "name": data.get("name", ""),
        "skins": data.get("skins", []) or [],
        "capes": data.get("capes", []) or [],
        "profileActions": data.get("profileActions", []) or [],
    }


def upload_skin(token: str, png_bytes: bytes, variant: str = "classic",
                timeout: float = 60) -> dict:
    """Upload *png_bytes* as the player's active skin.

    ``variant`` is ``"classic"`` (Steve, 4px arms) or ``"slim"`` (Alex).
    Returns the updated profile dict.
    """
    if variant not in ("classic", "slim"):
        variant = "classic"
    resp = requests.post(
        f"{MINECRAFT_SERVICES_URL}/minecraft/profile/skins",
        headers={**_HEADERS, **_bearer(token)},
        files={"file": ("skin.png", png_bytes, "image/png")},
        data={"variant": variant},
        timeout=timeout,
    )
    _check(resp, "Upload skin")
    return resp.json()


def reset_skin(token: str, timeout: float = 30) -> dict:
    """Reset the player's skin back to the default (Steve/Alex)."""
    resp = requests.delete(
        f"{MINECRAFT_SERVICES_URL}/minecraft/profile/skins/active",
        headers={**_HEADERS, **_bearer(token)},
        timeout=timeout,
    )
    _check(resp, "Reset skin")
    return resp.json()


def set_active_cape(token: str, cape_id: str, timeout: float = 30) -> dict:
    """Equip the cape identified by *cape_id* (from :func:`get_profile`)."""
    resp = requests.put(
        f"{MINECRAFT_SERVICES_URL}/minecraft/profile/capes/active",
        headers={**_HEADERS, **_bearer(token)},
        json={"capeId": cape_id},
        timeout=timeout,
    )
    _check(resp, "Set active cape")
    return resp.json()


def hide_cape(token: str, timeout: float = 30) -> dict:
    """Hide the currently active cape."""
    resp = requests.delete(
        f"{MINECRAFT_SERVICES_URL}/minecraft/profile/capes/active",
        headers={**_HEADERS, **_bearer(token)},
        timeout=timeout,
    )
    _check(resp, "Hide cape")
    return resp.json()


def fetch_cape_image(cape_url: str, timeout: float = 30) -> bytes:
    """Download a cape's PNG texture (for thumbnails in the UI)."""
    resp = requests.get(cape_url, headers=_HEADERS, timeout=timeout)
    _check(resp, "Download cape texture")
    return resp.content