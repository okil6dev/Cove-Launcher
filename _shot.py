"""TEMP: render the real Settings page offscreen and save a screenshot."""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Qt ships no fonts in this install; use the Windows ones so text renders.
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication

import launcher.constants as C

root = tempfile.mkdtemp()
C.INSTANCES_DIR = os.path.join(root, "instances")
C.INSTANCE_INDEX_PATH = os.path.join(root, "instances.json")

import launcher.window as winmod
import launcher.pages.skins_page as skins


class _NoopVersionThread(QThread):
    ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def run(self):
        return


class _NoopMsProfileThread(QThread):
    ready = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, *a, **k):
        super().__init__()

    def run(self):
        return


winmod.VersionListThread = _NoopVersionThread
skins._MsProfileThread = _NoopMsProfileThread

app = QApplication(sys.argv)
win = winmod.LauncherWindow()
win.registry = __import__("launcher.instances", fromlist=["InstanceRegistry"]).InstanceRegistry(
    C.INSTANCE_INDEX_PATH)
win.show()


def snap():
    for _ in range(60):
        app.processEvents()


# Let the boot splash's settle-poller finish (it waits on real time).
deadline = time.time() + 6
while time.time() < deadline:
    app.processEvents()
    time.sleep(0.02)
    sp = getattr(win, "_splash", None)
    if sp is None or not sp.isVisible():
        break
# Belt and braces: drop it manually if the poller is still waiting.
sp = getattr(win, "_splash", None)
if sp is not None:
    try:
        sp.hide()
    except RuntimeError:
        pass
try:
    win._restore_splash_chrome()
except Exception:
    pass

snap()
win.switchTo(win.settings_page)
snap()

# Offscreen can't composite the mica/acrylic backdrop, so the dark-theme
# text lands on a white fallback viewport. Apply light styling *in memory*
# only (apply_theme_setting would write to the real vault).
from launcher import theme as theme_mod

theme_mod.apply_theme("light", win)
win._theme_applied = True
try:
    win.settings_page.refresh_theme()
except Exception:
    pass
snap()

# Objective layout dump: every control must sit inside the content column.
page = win.settings_page
from PyQt6.QtWidgets import QScrollArea

scrolls = page.findChildren(QScrollArea)
for sc in scrolls:
    vp = sc.viewport().size()
    content = sc.widget().size() if sc.widget() else None
    print(f"scroll viewport={vp.width()}x{vp.height()} content={content}")
for key, (w_spin, h_spin) in page._size_spins.items():
    pw = w_spin.mapTo(page, w_spin.rect().topLeft())
    print(f"  {key}: spin at x={pw.x()} y={pw.y()} "
          f"size={w_spin.width()}x{h_spin.height()}")
apply_btn = page.size_apply_btn.mapTo(page, page.size_apply_btn.rect().topLeft())
save_btn = page.save_button.mapTo(page, page.save_button.rect().topLeft())
print(f"  apply at x={apply_btn.x()} y={apply_btn.y()} "
      f"save at x={save_btn.x()} y={save_btn.y()}")
print(f"  page width={page.width()} save_button width={page.save_button.width()}")

pix = win.grab()
pix.save("_shot_settings.png")
print("saved", pix.width(), "x", pix.height())
sys.stdout.flush()
os._exit(0)