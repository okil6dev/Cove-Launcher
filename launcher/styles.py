"""
Custom per-instance title-screen styling.

Turns a user-chosen PNG into the Minecraft main-menu background without any
mods: we build a tiny resource pack that overrides every frame of the
title-screen panorama and register it at the front of the instance's
``options.txt`` ``resourcePacks`` list.

On-disk layout (per instance):

    <instance>/title-bg.png                        normalised source image
    <instance>/resourcepacks/cove-title-style.zip
        pack.mcmeta
        assets/minecraft/textures/gui/title/background/panorama_0..9.png
    <instance>/options.txt                         resourcePacks entry added

The pack is regenerated lazily (editor save + every launch hook), so edits
to the source image propagate automatically.
"""

from __future__ import annotations

import json
import os
import re
import zipfile

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QImage


class StyleError(Exception):
    """Raised when a custom background cannot be installed."""


PACK_ZIP_NAME = "cove-title-style.zip"
_RESOURCE_PACKS_DIRNAME = "resourcepacks"
_ASSETS_DIR = "assets/minecraft/textures/gui/title/background"
_FRAME_COUNT = 10           # vanilla ships panorama_0..panorama_9
_OUTPUT_SIZE = 1024         # cube-map faces are square; 1024 keeps detail
_OVERLAY_W, _OVERLAY_H = 1920, 1080   # 'panorama_overlay.png' is stretched to screen
_OVERLAY_ASSET = f"{_ASSETS_DIR}/panorama_overlay.png"

STATIC_MODE = "static"      # flat image drawn on the screen as an overlay
PANORAMA_MODE = "panorama"  # wide image sliced across the rotating cube faces
_MODES = (STATIC_MODE, PANORAMA_MODE)


# ── paths ─────────────────────────────────────────────────────────────────────

def pack_zip_path(instance) -> str:
    return os.path.join(instance.dir, _RESOURCE_PACKS_DIRNAME, PACK_ZIP_NAME)


def options_txt_path(instance) -> str:
    return os.path.join(instance.dir, "options.txt")


# ── internals ─────────────────────────────────────────────────────────────────

def _pack_format_for(mc_version: str | None) -> int:
    """
    Best-effort resource-pack format for *mc_version*.

    A mismatch is harmless: Minecraft merely flags the pack as made for a
    different version but keeps loading it, and the pack list is managed
    programmatically anyway.
    """
    try:
        major, minor = (int(p) for p in str(mc_version).split(".")[:2])
    except (TypeError, ValueError):
        return 34
    if (major, minor) >= (1, 21):
        return 34
    return 15


def _prepare_frame_image(png_path: str) -> QImage:
    """Load *png_path*, centre-crop it square and scale it down."""
    img = QImage(png_path)
    if img.isNull():
        raise StyleError(f"Could not decode image: {png_path}")
    return _scaled_square(_centre_square(img))


def _centre_square(img: QImage) -> QImage:
    side = min(img.width(), img.height())
    left = (img.width() - side) // 2
    top = (img.height() - side) // 2
    return img.copy(left, top, side, side)


def _scaled_square(cropped: QImage) -> QImage:
    return cropped.scaled(
        _OUTPUT_SIZE, _OUTPUT_SIZE,
        aspectRatioMode=Qt.AspectRatioMode.IgnoreAspectRatio,
        transformMode=Qt.TransformationMode.SmoothTransformation,
    )


def _transparent_square(size: int = _OUTPUT_SIZE) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    return img


def _prepare_frames(png_path: str, mode: str) -> list[QImage]:
    """
    Build the cube-map faces for *mode*.

    ``panorama`` -- treats the image as a 360° strip and slices it into
                    :data:`_FRAME_COUNT` consecutive tiles so the title
                    screen pans across your image like vanilla.
    ``static``   -- cube faces are fully *transparent*; the picture is
                    served instead by an opaque ``panorama_overlay.png``,
                    which guarantees a flat (non-rotating) look regardless
                    of the client's draw order.
    """
    if mode == PANORAMA_MODE:
        img = QImage(png_path)
        if img.isNull():
            raise StyleError(f"Could not decode image: {png_path}")
        tile_h = 768
        tile_w = 768
        strip_w = tile_w * _FRAME_COUNT
        strip = img.scaled(
            strip_w, tile_h,
            aspectRatioMode=Qt.AspectRatioMode.IgnoreAspectRatio,
            transformMode=Qt.TransformationMode.SmoothTransformation,
        )
        return [
            strip.copy(i * tile_w, 0, tile_w, tile_h)
            for i in range(_FRAME_COUNT)
        ]
    return [_transparent_square() for _ in range(_FRAME_COUNT)]


def _cover_to(img: QImage, target_w: int, target_h: int) -> QImage:
    """Scale *img* to cover *target_w x target_h*, centre-cropping overflow."""
    scale = max(target_w / img.width(), target_h / img.height())
    w, h = int(img.width() * scale), int(img.height() * scale)
    scaled = img.scaled(
        w, h,
        aspectRatioMode=Qt.AspectRatioMode.IgnoreAspectRatio,
        transformMode=Qt.TransformationMode.SmoothTransformation,
    )
    return scaled.copy((w - target_w) // 2, (h - target_h) // 2,
                       target_w, target_h)


def _prepare_overlay(png_path: str) -> QImage:
    """
    Build the flat ``panorama_overlay.png`` that is composited *on top of*
    the rotating panorama (this is what the title screen actually shows
    most clearly).
    """
    img = QImage(png_path)
    if img.isNull():
        raise StyleError(f"Could not decode image: {png_path}")
    return _cover_to(img, _OVERLAY_W, _OVERLAY_H)


def _transparent_overlay() -> QImage:
    """Fully transparent 16:9 image so the rotating panorama stays visible."""
    img = QImage(_OVERLAY_W, _OVERLAY_H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    return img


def _png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise StyleError("Failed to encode the background as PNG.")
    return bytes(buffer.data())


def _write_pack(instance, frame_pngs: list[bytes], mc_version: str | None,
                mode: str, overlay_png: bytes) -> str:
    """(Re)create the style resource pack. Returns the zip path."""
    out_dir = os.path.join(instance.dir, _RESOURCE_PACKS_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)
    meta = {
        "pack": {
            "pack_format": _pack_format_for(mc_version),
            "description": "Cove custom title background",
        }
    }
    zip_path = pack_zip_path(instance)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        zf.comment = mode.encode("ascii")   # lets ensure_applied detect changes
        zf.writestr("pack.mcmeta", json.dumps(meta, indent=2))
        folder = f"{_ASSETS_DIR}/"
        for i, png in enumerate(frame_pngs):
            zf.writestr(f"{folder}panorama_{i}.png", png)
        zf.writestr(_OVERLAY_ASSET, overlay_png)
    return zip_path


_ENTRY = f"file/{PACK_ZIP_NAME}"
_LINE_RE = re.compile(r"^resourcePacks:\[(.*)\]\s*$")
_TOKEN_RE = re.compile(r'"([^"]*)"')


def _read_resource_packs(instance) -> tuple[list[str] | None, list[str]]:
    """
    Return ``(entries, other_lines)``. ``entries`` is ``None`` when the
    options.txt does not exist yet; a missing key yields ``[]``.
    """
    path = options_txt_path(instance)
    if not os.path.isfile(path):
        return None, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    other: list[str] = []
    entries: list[str] = []
    seen = False
    for line in lines:
        match = _LINE_RE.match(line.strip())
        if match and not seen:
            seen = True
            entries = _TOKEN_RE.findall(match.group(1))
        else:
            other.append(line)
    if not seen:
        entries = []
    return entries, other


def _write_resource_packs(instance, entries: list[str],
                          other_lines: list[str]) -> None:
    parts = ",".join(f'"{e}"' for e in entries)
    line = f"resourcePacks:[{parts}]"
    body = "\n".join(other_lines + [line]) + "\n"
    with open(options_txt_path(instance), "w", encoding="utf-8") as fh:
        fh.write(body)


def _set_pack_enabled(instance, enabled: bool) -> None:
    entries, other = _read_resource_packs(instance)
    if entries is None and not enabled:
        return                     # nothing configured anywhere yet
    entries = entries or []
    cleaned = [e for e in entries if e != _ENTRY]
    if enabled:
        # Right after vanilla so the custom look wins immediately.
        if "vanilla" in cleaned:
            cleaned.insert(cleaned.index("vanilla") + 1, _ENTRY)
        else:
            cleaned.insert(0, _ENTRY)
    if not enabled and cleaned == entries:
        return                     # nothing changed
    _write_resource_packs(instance, cleaned, other)


# ── public API ────────────────────────────────────────────────────────────────

def set_title_background(instance, png_path: str) -> str:
    """
    Install *png_path* as this instance's main-menu background.

    The mapping style comes from ``instance.custom_bg_mode``
    (:data:`STATIC_MODE` or :data:`PANORAMA_MODE`). Returns the zip path.
    """
    if not os.path.isfile(png_path):
        raise StyleError(f"Background image missing: {png_path}")
    mode = getattr(instance, "custom_bg_mode", None)
    if mode not in _MODES:
        mode = STATIC_MODE
    frames = [_png_bytes(f) for f in _prepare_frames(png_path, mode)]
    if mode == PANORAMA_MODE:
        # Transparent overlay lets the rotating cube show through.
        overlay = _png_bytes(_transparent_overlay())
    else:
        # Flat full-screen overlay = the actual static look the user asked for.
        overlay = _png_bytes(_prepare_overlay(png_path))
    zip_path = _write_pack(instance, frames, instance.mc_version, mode,
                           overlay)
    _set_pack_enabled(instance, True)
    return zip_path


def clear_title_background(instance) -> None:
    """Remove the pack from disk and from the resourcePacks list."""
    _set_pack_enabled(instance, False)
    zip_path = pack_zip_path(instance)
    if os.path.isfile(zip_path):
        try:
            os.remove(zip_path)
        except OSError:
            pass                   # locked by a running game - retried later


def _zip_mode(zip_path: str) -> str | None:
    """Mode recorded when the pack was built, or ``None`` if unreadable."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            tag = zf.comment.decode("ascii", errors="replace")
    except Exception:  # noqa: BLE001 - corrupt/locked zip
        return None
    return tag if tag in _MODES else None


def ensure_applied(instance) -> None:
    """
    Idempotent hook run before launch (and after editor saves).

    * configured & reachable  -> pack present + enabled
    * nothing configured      -> pack removed + unlisted

    Rebuilds not only when the source image changed but also when the
    selected static/panorama mapping no longer matches the stored pack.
    """
    wanted = getattr(instance, "custom_bg_png", None)
    wanted_ok = bool(wanted) and os.path.isfile(str(wanted))
    zip_path = pack_zip_path(instance)
    has_zip = os.path.isfile(zip_path)
    if wanted_ok:
        mode = getattr(instance, "custom_bg_mode", None)
        needs_rebuild = (
            not has_zip
            or _zip_mode(zip_path) != (mode if mode in _MODES else STATIC_MODE)
            or os.path.getmtime(zip_path) < os.path.getmtime(str(wanted))
        )
        if needs_rebuild:
            set_title_background(instance, str(wanted))
        else:
            _set_pack_enabled(instance, True)
    elif has_zip:
        clear_title_background(instance)
    else:
        # Options may still reference us from a hand-deleted zip.
        _set_pack_enabled(instance, False)
