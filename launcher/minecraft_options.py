"""Launcher-wide Minecraft options: one choice, applied to every instance.

How it works
------------
The launcher stores the *choice* in its vault (key :data:`VAULT_KEY`); the
instances only ever receive the consequence, each in its own ``options.txt``.
That split matters because Minecraft rewrites ``options.txt`` when it exits and
a fresh instance starts without the file at all: the vault is the source of
truth, re-applied before every launch, so a brand-new instance, a reset options
file, or a slider turned back up in-game all end up obeying what was chosen in
Settings -> Minecraft.

Only what the user switched *on* is enforced. An option that is off is left
completely alone, so a Music slider set by hand in-game survives untouched --
the launcher never overwrites a setting it was not asked to manage. Switching
an option off again writes vanilla's default back once, which is why that
default is part of each option's definition.

Two kinds of option
-------------------
A *toggle* (``kind="toggle"``) is a plain switch and its on-value is fixed: the
Music slider is always written as 0.0 when on. A *number* option
(``kind="number"``) is a switch plus a number box, where the number the user
chose is the value that is written -- that is how GUI scale can be pinned to 2
instead of leaving Minecraft's Auto to pick a scale that makes the interface
too big. Both kinds are left alone while off, and both get vanilla's default
written back once when switched off. A number option can also be set back to
"not managed" (stored as ``None``), which is its starting state.

Options.txt keys
----------------
Java Edition has stored the Music slider as ``music`` through 1.7.1 and as
``soundCategory_music`` from 1.7.2 onwards; both take 0.0 (muted) to 1.0 (full
volume, the vanilla default). Only the key matching the instance's own version
is written: vanilla drops options it does not recognise the next time it saves
the file, so writing both would only leave litter behind. A missing
``options.txt`` is created with just our line -- Minecraft fills every other
option with its default.

``guiScale`` is a plain integer, and ``0`` means the Auto setting vanilla
starts with (which is also what is written back when the switch is turned off).
The in-game slider only offers a scale the game window can actually fit -- about
``width/320`` by ``height/240`` -- so its maximum depends on the window: 4 at
1920x1080, 9 in a 4K window. Vanilla clamps anything larger, so a value above
that ceiling is quiet rather than harmful.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .styles import options_txt_path

#: Vault key holding the user's choices, e.g.
#: ``{"disable_music": True, "gui_scale": 2}``.
VAULT_KEY = "minecraft_options"

_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?")


def _music_txt_key(mc_version: str | None) -> str:
    """``options.txt`` key for the Music slider in *mc_version*.

    The sound sliders were renamed in 1.7.2 (``music`` -> ``soundCategory_music``).
    Anything unparseable (a snapshot id, an empty version) is treated as modern,
    which is what this launcher installs.
    """
    match = _VERSION_RE.match(str(mc_version or ""))
    if match:
        parts = tuple(int(p) if p else 0 for p in match.groups())
        if parts < (1, 7, 2):
            return "music"
    return "soundCategory_music"


#: Largest GUI scale the number box offers. 4 covers 1080p and 9 a 4K window;
#: ten keeps a round, generous ceiling without inviting edits Minecraft would
#: clamp anyway.
GUI_SCALE_MAX = 10


@dataclass(frozen=True)
class GlobalOption:
    """One launcher-wide setting and the ``options.txt`` line it writes.

    See the module docstring for the two kinds. ``enabled_value`` is only used
    by toggles; a number option writes the number the user picked.
    """

    key: str                                  # vault key, e.g. "disable_music"
    label: str                                # what the Settings row says
    description: str                          # what it does, one line
    disabled_value: str                       # vanilla default, written back on off
    txt_key: Callable[[str | None], str]      # options.txt key for a version
    kind: str = "toggle"                      # "toggle" | "number"
    enabled_value: str = ""                   # toggles: value written while on
    hint: str = ""                            # extra line under the description
    initial: int = 2                          # numbers: what the box starts at
    minimum: int = 1                          # numbers: inclusive range
    maximum: int = 10


#: Every global option the launcher can enforce, in UI order. Adding one here
#: is enough: the Settings section and the pre-launch hook both read this table.
OPTIONS: tuple[GlobalOption, ...] = (
    GlobalOption(
        key="disable_music",
        label="Disable music",
        description="Sets Minecraft's Music slider to 0 in every instance.",
        enabled_value="0.0",
        disabled_value="1.0",
        txt_key=_music_txt_key,
    ),
    GlobalOption(
        key="gui_scale",
        label="GUI scale",
        description=(
            "Pins Minecraft's GUI Scale to a number instead of Auto, so the "
            "interface cannot come out too big for the screen."
        ),
        hint=(
            "Minecraft only offers scales the game window can fit - roughly "
            "width/320 by height/240 - so 1920x1080 tops out at 4 and a 4K "
            "window at 9. Larger values are clamped by the game; 1 is the "
            "smallest interface."
        ),
        disabled_value="0",  # 0 is Auto, vanilla's own default
        txt_key=lambda _version: "guiScale",
        kind="number",
        initial=2,
        minimum=1,
        maximum=GUI_SCALE_MAX,
    ),
)

#: Nothing is enforced until the user asks for it: toggles start off, numbers
#: start unset (``None``), which leaves that Minecraft setting alone.
DEFAULTS: dict[str, Any] = {
    option.key: (False if option.kind == "toggle" else None)
    for option in OPTIONS
}

_BY_KEY = {option.key: option for option in OPTIONS}


def option(key: str) -> GlobalOption:
    """The :class:`GlobalOption` registered under *key*."""
    return _BY_KEY[key]


def coerce(opt: GlobalOption, value: Any) -> Any:
    """*value* as *opt* stores it, or ``None`` when it is not a usable choice.

    Toggles accept real booleans only: a hand-edited ``"yes"`` would otherwise
    be truthy and silently enforce a setting nobody switched on. Numbers accept
    ints only (``True`` is an ``int`` in Python, so bools are rejected on
    purpose) and are clamped into the option's range.
    """
    if opt.kind == "toggle":
        return value if isinstance(value, bool) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(opt.minimum, min(opt.maximum, value))


def is_enforced(opt: GlobalOption, value: Any) -> bool:
    """Is *value* a real choice, rather than "leave the instance alone"?"""
    if opt.kind == "toggle":
        return value is True
    return isinstance(value, int) and not isinstance(value, bool)


# ── the stored choice ────────────────────────────────────────────────────────

def normalize(raw: Any) -> dict[str, Any]:
    """Merge stored *raw* onto :data:`DEFAULTS`, dropping anything unusable."""
    out = dict(DEFAULTS)
    if isinstance(raw, Mapping):
        for opt in OPTIONS:
            if opt.key not in raw:
                continue
            value = coerce(opt, raw[opt.key])
            if value is not None:
                out[opt.key] = value
    return out


def load(vault: Any = None) -> dict[str, Any]:
    """All global options, falling back to the defaults on any failure."""
    try:
        stored = (
            vault if vault is not None else _default_vault()
        ).read().get(VAULT_KEY)
    except Exception:  # noqa: BLE001 - a locked/corrupt vault must not break a launch
        return dict(DEFAULTS)
    return normalize(stored)


def save(vault: Any, choices: Mapping[str, Any]) -> None:
    """Persist *choices* under :data:`VAULT_KEY`, leaving other secrets alone.

    A number option given ``None`` is stored as such: the switch was turned off,
    so that settings row is no longer managed at all.
    """
    data = vault.read()
    current = normalize(data.get(VAULT_KEY))
    if isinstance(choices, Mapping):
        for opt in OPTIONS:
            if opt.key not in choices:
                continue
            raw_value = choices[opt.key]
            if opt.kind == "number" and raw_value is None:
                current[opt.key] = None
                continue
            value = coerce(opt, raw_value)
            if value is not None:
                current[opt.key] = value
    data[VAULT_KEY] = current
    vault.write(data)


def _default_vault():
    from .security import Vault

    return Vault()


# ── the instances' own options.txt ───────────────────────────────────────────

def _set_keys(path: str, values: Mapping[str, str]) -> bool:
    """Set the ``key:value`` lines in *path*; True when the file changed.

    Every other line is preserved verbatim -- Minecraft's own options and the
    ``resourcePacks`` entry that :mod:`launcher.styles` maintains live in the
    same file -- and an existing line for one of our keys is replaced instead
    of duplicated. Unreadable/unwritable files are reported as "no change"
    rather than raising: this runs on the launch path.
    """
    original = ""
    lines: list[str] = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                original = fh.read()
        except OSError:
            return False
        lines = original.splitlines()

    wanted = dict(values)
    kept = [
        line for line in lines
        if line.split(":", 1)[0].strip() not in wanted
    ]
    kept.extend(f"{key}:{value}" for key, value in wanted.items())
    body = "\n".join(kept) + "\n"
    if body == original:
        return False

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError:
        return False
    return True


def _apply(instance, values: Mapping[str, str]) -> bool:
    """Write *values* into *instance*'s options.txt. True when it changed."""
    if not values:
        return False
    return _set_keys(options_txt_path(instance), values)


def enforce(instance, choices: Mapping[str, Any]) -> list[str]:
    """Make *instance* obey every enabled option in *choices*.

    Returns the labels of the options enforced -- but only when the file had to
    be touched, which keeps the pre-launch log quiet on the normal path where
    ``options.txt`` is already correct.
    """
    version = getattr(instance, "mc_version", None)
    values: dict[str, str] = {}
    labels: list[str] = []
    for opt in OPTIONS:
        value = choices.get(opt.key)
        if not is_enforced(opt, value):
            continue
        # A toggle writes its fixed on-value; a number writes what was picked.
        values[opt.txt_key(version)] = (
            opt.enabled_value if opt.kind == "toggle" else str(int(value))
        )
        labels.append(opt.label)
    return labels if _apply(instance, values) else []


def restore(instance, keys: Iterable[str]) -> list[str]:
    """Put vanilla's default back for the option *keys* (switched off).

    Called when a switch is turned off, so un-muting really un-mutes instead of
    leaving the previous 0 behind.
    """
    values: dict[str, str] = {}
    labels: list[str] = []
    for key in keys:
        opt = _BY_KEY.get(key)
        if opt is None:
            continue
        values[opt.txt_key(getattr(instance, "mc_version", None))] = opt.disabled_value
        labels.append(opt.label)
    return labels if _apply(instance, values) else []


def sync(instances: Iterable[Any], choices: Mapping[str, Any],
         *, turn_off: Iterable[str] = ()) -> list[str]:
    """Apply *choices* to every instance in *instances*.

    ``turn_off`` names the options the user just switched off; those get the
    vanilla default written back once. Returns the names of the instances whose
    ``options.txt`` actually changed.
    """
    off = tuple(turn_off)
    changed: list[str] = []
    for instance in instances or ():
        wrote = enforce(instance, choices)
        wrote += restore(instance, off)
        if wrote:
            changed.append(str(getattr(instance, "name", "?")))
    return changed
