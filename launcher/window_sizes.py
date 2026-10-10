"""Configurable window sizes, persisted in the encrypted vault.

Settings -> "Window Sizes" lets the user choose the starting size of each
launcher window. Everything is clamped on the way *in* and *out* of the vault,
so a hand-edited or stale entry can never produce a zero, negative or
absurdly large window. The main window additionally re-clamps against the
available screen area when sizes are applied live (see
``LauncherWindow.apply_window_sizes``).

Reading is deliberately best-effort: a locked, corrupt or missing vault falls
back to the defaults rather than aborting window construction -- sizing must
never be the reason the launcher fails to open.

Configurable windows (key -> label, default, minimum):

* ``main``            -> Main window,     1280 x 820, min 900 x 620
* ``instance_editor`` -> Instance editor,  900 x 620, min 760 x 520
* ``server_editor``   -> Server editor,    900 x 620, min 760 x 520
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple

from .security import Vault

# Vault key holding ``{"main": [w, h], "instance_editor": [w, h], ...}``.
VAULT_KEY = "window_sizes"

# Absolute upper bounds. These are deliberately generous and independent of
# the screen: the screen clamp happens where the size is actually applied.
MAX_WIDTH = 3840
MAX_HEIGHT = 2160


class WindowSpec(NamedTuple):
    """Describes one configurable window."""

    key: str
    label: str
    default_w: int
    default_h: int
    min_w: int
    min_h: int


# Display order == Settings order.
WINDOW_SPECS: tuple[WindowSpec, ...] = (
    WindowSpec("main", "Main window", 1280, 820, 900, 620),
    WindowSpec("instance_editor", "Instance editor", 900, 620, 760, 520),
    WindowSpec("server_editor", "Server editor", 900, 620, 760, 520),
)

_SPECS: dict[str, WindowSpec] = {spec.key: spec for spec in WINDOW_SPECS}


def clamp_size(key: str, width: Any, height: Any) -> tuple[int, int]:
    """Return ``(width, height)`` clamped to *key*'s limits.

    Non-numeric input falls back to that window's default rather than raising:
    the vault is untrusted input.
    """
    spec = _SPECS[key]
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return spec.default_w, spec.default_h
    w = max(spec.min_w, min(w, MAX_WIDTH))
    h = max(spec.min_h, min(h, MAX_HEIGHT))
    return w, h


def default_sizes() -> dict[str, tuple[int, int]]:
    """The built-in size of every configurable window."""
    return {spec.key: (spec.default_w, spec.default_h) for spec in WINDOW_SPECS}


def normalize(raw: Any) -> dict[str, tuple[int, int]]:
    """Merge an arbitrary vault value onto the defaults, clamping every entry.

    Unknown keys are dropped and malformed entries keep their default, so a
    corrupt ``window_sizes`` blob can never take the settings page down.
    """
    out = default_sizes()
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        if key not in _SPECS:
            continue
        try:
            width, height = value  # expects a 2-item sequence
        except (TypeError, ValueError):
            continue
        out[key] = clamp_size(key, width, height)
    return out


def load_sizes(vault: Any = None) -> dict[str, tuple[int, int]]:
    """All configured sizes, falling back to the defaults on any failure."""
    try:
        raw = (vault if vault is not None else Vault()).read().get(VAULT_KEY)
    except Exception:  # noqa: BLE001 - locked/corrupt vault must not abort boot
        return default_sizes()
    return normalize(raw)


def get_size(key: str, vault: Any = None) -> tuple[int, int]:
    """The configured size of the single window *key*."""
    if key not in _SPECS:
        raise KeyError(key)
    return load_sizes(vault)[key]


def save_sizes(vault: Any, sizes: Mapping[str, Any]) -> None:
    """Persist *sizes* under :data:`VAULT_KEY`, leaving other secrets alone.

    Only known keys are written and every value is clamped, so callers can
    hand this raw widget state straight from the UI.
    """
    data = vault.read()
    current = normalize(data.get(VAULT_KEY))
    for key, value in sizes.items():
        if key not in _SPECS:
            continue
        try:
            width, height = value
        except (TypeError, ValueError):
            continue
        current[key] = clamp_size(key, width, height)
    data[VAULT_KEY] = {k: [w, h] for k, (w, h) in current.items()}
    vault.write(data)
