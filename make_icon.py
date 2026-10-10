"""
Generate the Cove Launcher icon — minimal chill waves.

A flat, minimal mark: a calm seafoam plate with three smooth wave layers
in graduated teals.  Saves:

    assets/cove_icon.png   (1024 px master + used for the window icon)
    assets/cove_icon.ico   (256 px, for the Windows exe / title bar)

Run from the project root:
    python make_icon.py
"""

from __future__ import annotations

import math
import os
import sys

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
)

SIZE = 1024
RADIUS = 220          # rounded-corner radius for the icon plate
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# Minimal palette
PLATE = QColor("#e8f4f2")        # soft seafoam background
WAVE_FAR = QColor("#57b8ac")     # light calm teal
WAVE_MID = QColor("#2a9d8f")     # mid teal
WAVE_NEAR = QColor("#1d6f74")    # deep teal


def rounded_path(size: int) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), RADIUS, RADIUS)
    return path


def wave_path(size: int, base_y: float, amplitude: float,
              wavelength: float, phase: float) -> QPainterPath:
    """A filled wave: sine crest line down to the bottom of the icon."""
    path = QPainterPath()
    path.moveTo(0, size)
    path.lineTo(0, base_y)
    x = 0.0
    step = size / 64.0
    while x <= size:
        y = base_y + amplitude * math.sin((x / wavelength) * 2 * math.pi + phase)
        path.lineTo(x, y)
        x += step
    path.lineTo(size, size)
    path.closeSubpath()
    return path


def draw_icon(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = float(size)
    p.setClipPath(rounded_path(size))

    # ── flat plate ─────────────────────────────────────────────────────────
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(PLATE)
    p.drawRect(QRectF(0, 0, s, s))

    # ── three smooth wave layers, no outlines ──────────────────────────────
    for colour, base, amp, wl, phase in (
        (WAVE_FAR,  0.520, s * 0.034, s * 0.85, 0.4),
        (WAVE_MID,  0.655, s * 0.040, s * 0.62, 2.6),
        (WAVE_NEAR, 0.800, s * 0.046, s * 0.95, 4.4),
    ):
        p.setBrush(colour)
        p.drawPath(wave_path(size, s * base, amp, wl, phase))

    p.setClipping(False)
    p.end()
    return pm


def main() -> None:
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv)
    os.makedirs(OUT_DIR, exist_ok=True)

    master = draw_icon(SIZE)
    png_path = os.path.join(OUT_DIR, "cove_icon.png")
    master.save(png_path, "PNG")
    print("wrote", png_path)

    # ICO: multi-size (16-256 px) so every taskbar / pin size is crisp.
    ico_path = os.path.join(OUT_DIR, "cove_icon.ico")
    ok = write_multi_size_ico(master, ico_path)
    print("wrote" if ok else "FAILED to write", ico_path)

    # Preview render at taskbar-ish sizes for a quick sanity check.
    for px in (16, 32, 48, 64, 128):
        draw_icon(px).save(os.path.join(OUT_DIR, f"cove_icon_{px}.png"), "PNG")
    print("previews written")


def write_multi_size_ico(master: QPixmap, path: str) -> bool:
    """
    Downscale the 1024 px master with high-quality filtering and embed all
    standard Windows icon sizes into one .ico (needs Pillow).
    """
    try:
        from PIL import Image
        from PyQt6.QtCore import QBuffer, QIODevice

        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        master.save(buf, "PNG")
        payload = bytes(buf.data())
        import io as _io

        img = Image.open(_io.BytesIO(payload))
        img.save(
            path,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                   (64, 64), (128, 128), (256, 256)],
        )
        return True
    except Exception as exc:  # noqa: BLE001 - fall back to Qt's single-size ico
        print("multi-size ico failed (%s); falling back to Qt writer" % exc)
        icon = QIcon(master)
        return icon.pixmap(256, 256).save(path, "ICO")


if __name__ == "__main__":
    main()