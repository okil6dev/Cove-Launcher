"""
Skins tab: a local skin & cape editor + library.

The tab stores every uploaded skin/cape in the launcher's data folder
(``%APPDATA%\\Cove\\skins``) with an index (active skin / active cape / model)
in ``manifest.json`` and applies the selection depending on the active account:

  * **Microsoft**  -- uses the official `Minecraft Services` API
    (``api.minecraftservices.com``) so skins & capes change *server-side* with
    **no mods**, exactly like the real launcher / PrismLauncher.  Every cape
    the player already owns is detected via ``GET /minecraft/profile`` and can
    be switched on/off (``PUT``/``DELETE .../capes/active``).
  * **Ely.by**     -- uploads the skin to the account skinsystem
    (best-effort; ely.by can reject unregistered apps).
  * **Offline**    -- no server profile exists, so the skin is applied by
    writing CustomSkinLoader ``LocalSkin`` files into the selected instance
    (CustomSkinLoader mod required).
"""

from __future__ import annotations

import json
import os
import shutil
import uuid as uuidlib
from dataclasses import dataclass, asdict

from PyQt6.QtCore import QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    HorizontalSeparator,
    LineEdit,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)

from ..constants import (
    CAPES_DIR,
    CUSTOM_SKIN_LOADER_SLUG,
    SKINS_DIR,
    SKINS_MANIFEST_PATH,
)
from ..page_scroll import scrollable_page_root
from ..workers_net import SOURCE_MODRINTH, ModInstallThread
from .. import mojang_services as ms


class _RenameDialog(MessageBoxBase):
    """Modal to rename a saved skin in the library."""

    def __init__(self, parent, current: str) -> None:
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("Rename skin"))
        self.name_edit = LineEdit()
        self.name_edit.setText(current)
        self.name_edit.selectAll()
        self.viewLayout.addWidget(self.name_edit)
        self.yesButton.setText("Rename")
        self.cancelButton.setText("Cancel")

    def validate(self) -> bool:  # noqa: N802 - qfw API
        return bool(self.name_edit.text().strip())


# ---------------------------------------------------------------------------
# Persistence: local skin/cape library
# ---------------------------------------------------------------------------
@dataclass
class _Entry:
    id: str
    file: str      # basename inside SKINS_DIR / CAPES_DIR
    name: str
    added: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict) -> "_Entry":
        return _Entry(
            id=str(raw.get("id", "")),
            file=str(raw.get("file", "")),
            name=str(raw.get("name", "")),
            added=str(raw.get("added", "")),
        )


class SkinLibrary:
    """Tiny JSON-backed store of the user's skins & capes on disk."""

    def __init__(self) -> None:
        self._skins: list[_Entry] = []
        self._capes: list[_Entry] = []
        self._active_skin: str | None = None
        self._active_cape: str | None = None
        self.model: str = "classic"
        self._load()

    # -- disk ---------------------------------------------------------------
    def _load(self) -> None:
        os.makedirs(CAPES_DIR, exist_ok=True)
        data: dict = {}
        if os.path.isfile(SKINS_MANIFEST_PATH):
            try:
                with open(SKINS_MANIFEST_PATH, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
        self._skins = [_Entry.from_dict(e) for e in data.get("skins", [])]
        self._capes = [_Entry.from_dict(e) for e in data.get("capes", [])]
        self._active_skin = data.get("active_skin")
        self._active_cape = data.get("active_cape")
        self.model = str(data.get("model", "classic") or "classic")
        if self.model not in ("classic", "slim"):
            self.model = "classic"
        self._prune()

    def _save(self) -> None:
        os.makedirs(SKINS_DIR, exist_ok=True)
        data = {
            "active_skin": self._active_skin,
            "active_cape": self._active_cape,
            "model": self.model,
            "skins": [e.to_dict() for e in self._skins],
            "capes": [e.to_dict() for e in self._capes],
        }
        tmp = SKINS_MANIFEST_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, SKINS_MANIFEST_PATH)
        except OSError:
            pass

    def _prune(self) -> None:
        self._skins = [e for e in self._skins if os.path.isfile(self._skin_path(e))]
        self._capes = [e for e in self._capes if os.path.isfile(self._cape_path(e))]
        if self._active_skin and not any(e.id == self._active_skin for e in self._skins):
            self._active_skin = self._skins[0].id if self._skins else None
        if self._active_cape and not any(e.id == self._active_cape for e in self._capes):
            self._active_cape = self._capes[0].id if self._capes else None

    # -- paths --------------------------------------------------------------
    @staticmethod
    def _skin_path(entry: _Entry) -> str:
        return os.path.join(SKINS_DIR, entry.file)

    @staticmethod
    def _cape_path(entry: _Entry) -> str:
        return os.path.join(CAPES_DIR, entry.file)
# -- accessors -----------------------------------------------------------
    def skins(self) -> list[_Entry]:
        return list(self._skins)

    def capes(self) -> list[_Entry]:
        return list(self._capes)

    def active_skin(self) -> _Entry | None:
        for e in self._skins:
            if e.id == self._active_skin:
                return e
        return self._skins[0] if self._skins else None

    def active_cape(self) -> _Entry | None:
        for e in self._capes:
            if e.id == self._active_cape:
                return e
        return self._capes[0] if self._capes else None

    def active_skin_path(self) -> str | None:
        e = self.active_skin()
        return self._skin_path(e) if e else None

    def active_cape_path(self) -> str | None:
        e = self.active_cape()
        return self._cape_path(e) if e else None

    # -- mutations ------------------------------------------------------------
    def add_skin(self, src_path: str) -> _Entry:
        os.makedirs(SKINS_DIR, exist_ok=True)
        name = os.path.splitext(os.path.basename(src_path))[0] or "skin"
        entry = _Entry(id=uuidlib.uuid4().hex, file=f"{uuidlib.uuid4().hex}.png", name=name)
        shutil.copyfile(src_path, self._skin_path(entry))
        self._skins.append(entry)
        self._active_skin = entry.id
        self._save()
        return entry

    def remove_skin(self, entry_id: str) -> None:
        self._skins = [e for e in self._skins if e.id != entry_id]
        if self._active_skin == entry_id:
            self._active_skin = self._skins[0].id if self._skins else None
        self._save()

    def set_active_skin(self, entry_id: str) -> None:
        if any(e.id == entry_id for e in self._skins):
            self._active_skin = entry_id
            self._save()

    def rename_skin(self, entry_id: str, name: str) -> None:
        """Rename a saved skin (the file keeps its stored id-based name)."""
        name = str(name).strip()
        if not name:
            return
        for e in self._skins:
            if e.id == entry_id:
                e.name = name
                self._save()
                return
# ---------------------------------------------------------------------------
# Skin image helpers (64x64 coordinate space, matching PrismLauncher)
# ---------------------------------------------------------------------------
def load_skin(path: str) -> QImage | None:
    """Load a skin PNG, normalising legacy 64x32 skins up to 64x64."""
    img = QImage(path)
    if img.isNull():
        return None
    if img.width() != 64 or img.height() not in (32, 64):
        return None
    if img.format() not in (
        QImage.Format.Format_ARGB32,
        QImage.Format.Format_ARGB32_Premultiplied,
    ):
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
    if img.height() == 64:
        return img
    new_img = QImage(64, 64, QImage.Format.Format_ARGB32)
    new_img.fill(Qt.GlobalColor.transparent)
    p = QPainter(new_img)
    p.drawImage(0, 0, img)
    faces = [
        (4, 16, 16, 32, 4, 4), (8, 16, 16, 32, 4, 4),
        (0, 20, 24, 32, 4, 12), (4, 20, 16, 32, 4, 12),
        (8, 20, 8, 32, 4, 12), (12, 20, 16, 32, 4, 12),
        (44, 16, -8, 32, 4, 4), (48, 16, -8, 32, 4, 4),
        (40, 20, 0, 32, 4, 12), (44, 20, -8, 32, 4, 12),
        (48, 20, -16, 32, 4, 12), (52, 20, -8, 32, 4, 12),
    ]
    for (x, y, ox, oy, w, h) in faces:
        region = new_img.copy(x, y, w, h).mirrored(True, False)
        p.drawImage(x + ox, y + oy, region)
    p.end()
    return new_img


def render_head_thumb(skin: QImage, size: int = 40) -> QPixmap:
    """Head + hat thumbnail for QListWidget icons."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    w = 8 * (size // 8)
    off = (size - w) // 2
    p.drawImage(off, 0, skin.copy(8, 8, 8, 8).scaled(w, w))
    p.drawImage(off, 0, skin.copy(40, 8, 8, 8).scaled(w, w))
    p.end()
    return pm


def render_cape_thumb(img: QImage, zoom: int = 6) -> QPixmap:
    """Crisp pixel-art thumbnail of the cape's front (decorated) face.

    The profile URL serves the whole texture sheet (64x32 legacy, or 64x64
    since 1.19.1 where the elytra design lives to the right).  The cape is
    a 10x16x1 box unwrapped at the sheet's top-left; the face visible in
    game -- the decorated front -- is the 10x16 region at (1, 1).  We crop
    exactly that face and enlarge it with an INTEGER zoom factor using
    nearest-neighbour (Fast) scaling, so every texture pixel stays sharp.
    """
    s = max(1, round(img.width() / 64.0))  # HD sheets: all coords scale
    src = img.copy(1 * s, 1 * s, 10 * s, 16 * s)
    if _image_fully_transparent(src):
        # Fallback: inner face, in case a texture stores the design there.
        src = img.copy(12 * s, 1 * s, 10 * s, 16 * s)
    if _image_fully_transparent(src):
        src = img  # degenerate sheet: show everything rather than nothing
    w, h = 10 * zoom, 16 * zoom
    pm = QPixmap(w, h)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.drawImage(0, 0, src.scaled(
        w, h,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation))  # nearest-neighbour
    p.end()
    return pm


def _image_fully_transparent(img: QImage) -> bool:
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 0:
                return False
    return True
# ---------------------------------------------------------------------------
# Background workers (network stays off the GUI thread)
# ---------------------------------------------------------------------------
class _ApplyThread(QThread):
    """Run a no-Qt callable off the GUI thread."""

    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: D102
        try:
            msg = self._fn()
            self.done.emit(str(msg or "Done."))
        except Exception as exc:  # noqa: BLE001 - surface any error to the UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _MsProfileThread(QThread):
    """Fetch a Microsoft account's profile (skins + owned capes)."""

    ready = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, store, account, parent=None):
        super().__init__(parent)
        self._store = store
        self._account = account

    def run(self) -> None:  # noqa: D102
        try:
            _, _, token = self._store.ensure_access_token(self._account)
            profile = ms.get_profile(token)
            self.ready.emit(profile)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _CapeThumbThread(QThread):
    """Download cape textures sequentially, emitting a thumbnail per cape."""

    thumb = pyqtSignal(int, QImage)

    def __init__(self, urls: list, parent=None):
        super().__init__(parent)
        self._urls = list(urls)

    def run(self) -> None:  # noqa: D102
        for index, url in enumerate(self._urls):
            if not url:
                continue
            try:
                data = ms.fetch_cape_image(str(url))
                img = QImage.fromData(data)
                if not img.isNull():
                    self.thumb.emit(index, img)
            except Exception:  # noqa: BLE001 - thumbnail failure is cosmetic
                continue
class SkinsPage(QWidget):
    """Skin editor: local library + account-aware apply."""

    # Rendered cape zoom factor (thumb = 22*z x 17*z, native pixel art).
    _CAPE_ZOOM = 6

    def __init__(self, store, get_instance, log, toast, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("skinsPage")
        self._store = store
        self._get_instance = get_instance
        self._log = log
        self._toast = toast

        self._lib = SkinLibrary()
        self._profile: dict | None = None
        self._detected_capes: list[dict] = []

        self._ms_thread: _MsProfileThread | None = None
        self._thumb_thread: _CapeThumbThread | None = None
        self._apply_thread: _ApplyThread | None = None
        self._csl_thread: ModInstallThread | None = None

        self._build_ui()
        self.refresh_all()

    # ------------------------------------------------------------------ ui --
    # Two theme-aware cards (a slim header, then Library | Apply), mirroring
    # the Instances page: plain QFrames styled in QSS via _apply_theme() so
    # the mica/acrylic backdrops keep working.
    def _build_ui(self) -> None:
        # Scrollable content: the two cards side by side demand ~1200px, which
        # would otherwise clamp the boot window wider than its intended size.
        root = scrollable_page_root(self, margins=(24, 20, 24, 16), spacing=12)

        # ---- header card: page title + active account ----------------------
        self._header = QFrame()
        self._header.setObjectName("skinsHeader")
        header_lay = QHBoxLayout(self._header)
        header_lay.setContentsMargins(16, 10, 12, 10)
        header_lay.setSpacing(10)

        title = SubtitleLabel("Skins & Capes")
        title.setStyleSheet("font-size: 20px;")
        header_lay.addWidget(title)
        header_lay.addStretch(1)

        self._account_label = BodyLabel("")
        self._account_label.setObjectName("accountPill")
        self._account_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_lay.addWidget(self._account_label)
        root.addWidget(self._header)

        # ---- body: two cards ---------------------------------------------
        self._lib_card = self._build_library()
        self._apply_card = self._build_apply()

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._lib_card, stretch=5)
        body.addWidget(self._apply_card, stretch=4)
        root.addLayout(body, stretch=1)

        self._apply_theme()

    @staticmethod
    def _section_title(text: str) -> BodyLabel:
        label = BodyLabel(text)
        label.setStyleSheet("font-size: 14px; font-weight: 600;")
        return label

    @staticmethod
    def _make_card(object_name: str) -> tuple[QFrame, QVBoxLayout]:
        """A round-cornered card ready to be filled and styled later."""
        card = QFrame()
        card.setObjectName(object_name)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        return card, lay

    def _build_library(self) -> QFrame:
        card, lay = self._make_card("skinsLibraryCard")

        lay.addWidget(self._section_title("Skin Library"))

        self.skin_list = QListWidget()
        self.skin_list.setIconSize(QSize(40, 40))
        self.skin_list.setWordWrap(True)
        self.skin_list.currentItemChanged.connect(
            lambda *_: self._refresh_skin_buttons())
        self.skin_list.itemDoubleClicked.connect(
            lambda *_: self._on_set_active_skin())
        self._style_list(self.skin_list)
        lay.addWidget(self.skin_list, stretch=1)

        skin_row = QHBoxLayout()
        skin_row.setSpacing(8)
        self.upload_btn = PushButton(FluentIcon.ADD, "Upload skin…")
        self.upload_btn.clicked.connect(self._on_upload_skin)
        skin_row.addWidget(self.upload_btn, 1)
        self.set_active_btn = PushButton(FluentIcon.ACCEPT, "Set active")
        self.set_active_btn.clicked.connect(self._on_set_active_skin)
        skin_row.addWidget(self.set_active_btn, 1)
        self.rename_btn = PushButton(FluentIcon.EDIT, "Rename…")
        self.rename_btn.clicked.connect(self._on_rename_skin)
        skin_row.addWidget(self.rename_btn, 1)
        lay.addLayout(skin_row)

        skin_row2 = QHBoxLayout()
        skin_row2.setSpacing(8)
        self.export_btn = PushButton(FluentIcon.SAVE, "Export PNG…")
        self.export_btn.clicked.connect(self._on_export_skin)
        skin_row2.addWidget(self.export_btn, 1)
        self.delete_skin_btn = PushButton(FluentIcon.DELETE, "Delete")
        self.delete_skin_btn.clicked.connect(self._on_delete_skin)
        skin_row2.addWidget(self.delete_skin_btn, 1)
        skin_row2.addStretch(1)
        lay.addLayout(skin_row2)

        return card

    def _build_apply(self) -> QFrame:
        card, lay = self._make_card("skinsApplyCard")

        lay.addWidget(self._section_title("Capes"))

        self._capes_note = CaptionLabel(
            "Profile capes are detected for Microsoft accounts.")
        self._capes_note.setWordWrap(True)
        lay.addWidget(self._capes_note)

        self.detected_cape_list = QListWidget()
        self.detected_cape_list.setViewMode(QListWidget.ViewMode.IconMode)
        # Crisp 10x16 cape front face at integer zoom, name below.
        self.detected_cape_list.setIconSize(QSize(10 * self._CAPE_ZOOM, 16 * self._CAPE_ZOOM))
        self.detected_cape_list.setGridSize(QSize(10 * self._CAPE_ZOOM + 16, 16 * self._CAPE_ZOOM + 40))
        self.detected_cape_list.setWordWrap(True)
        self.detected_cape_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.detected_cape_list.setMovement(QListWidget.Movement.Static)
        self.detected_cape_list.setUniformItemSizes(True)
        self.detected_cape_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detected_cape_list.currentItemChanged.connect(
            lambda *_: self._refresh_detected_cape_buttons())
        self._style_list(self.detected_cape_list)
        lay.addWidget(self.detected_cape_list, stretch=1)

        cape_actions = QHBoxLayout()
        cape_actions.setSpacing(6)
        self.cape_set_active_btn = PushButton(FluentIcon.ACCEPT, "Set active")
        self.cape_set_active_btn.clicked.connect(self._on_detected_cape_set_active)
        cape_actions.addWidget(self.cape_set_active_btn, 1)
        self.cape_hide_btn = PushButton("Hide cape")
        self.cape_hide_btn.clicked.connect(self._on_detected_cape_hide)
        cape_actions.addWidget(self.cape_hide_btn, 1)
        self.cape_refresh_btn = PushButton(FluentIcon.SYNC, "Refresh")
        self.cape_refresh_btn.clicked.connect(self._on_refresh_detected_capes)
        cape_actions.addWidget(self.cape_refresh_btn, 1)
        lay.addLayout(cape_actions)

        lay.addWidget(HorizontalSeparator())

        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        model_row.addWidget(CaptionLabel("Skin model"))
        self.model_combo = ComboBox()
        self.model_combo.addItem("Steve (classic)", userData="classic")
        self.model_combo.addItem("Alex (slim)", userData="slim")
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        model_row.addWidget(self.model_combo)
        model_row.addStretch()
        lay.addLayout(model_row)

        self.apply_btn = PrimaryPushButton(FluentIcon.GAME, "Apply skin")
        self.apply_btn.clicked.connect(self._on_apply)
        lay.addWidget(self.apply_btn)

        self.csl_btn = PushButton("Install CustomSkinLoader…")
        self.csl_btn.clicked.connect(self._on_install_csl)
        self.csl_btn.setVisible(False)  # shown only for offline accounts
        lay.addWidget(self.csl_btn)

        self._status_label = CaptionLabel("")
        self._status_label.setWordWrap(True)
        lay.addWidget(self._status_label)

        return card

    @staticmethod
    def _style_list(list_widget: QListWidget) -> None:
        """Opaque-free QListWidget with theme-aware selection/hover colours."""
        from ..theme import item_hover_bg, item_sel_bg

        list_widget.setStyleSheet(
            "QListWidget { background: transparent; outline: none;"
            " border: none; }"
            "QListWidget::item { border-radius: 8px; margin: 2px 0; }"
            f"QListWidget::item:selected {{ background: {item_sel_bg()}; }}"
            f"QListWidget::item:hover:!selected {{ background: {item_hover_bg()}; }}"
        )

    def _apply_theme(self) -> None:
        """Re-style the header / library / apply cards for the theme."""
        from ..theme import fg, hairline, icon_tile_bg, is_dark_now

        card_bg = (
            "rgba(255, 255, 255, 0.05)" if is_dark_now()
            else "rgba(255, 255, 255, 0.6)"
        )
        border = hairline()

        def card_qss(name: str) -> str:
            return (
                f"QFrame#{name} {{ background: {card_bg};"
                f" border: 1px solid {border}; border-radius: 11px; }}"
                f"QFrame#{name} QLabel {{ background: transparent; }}"
            )

        self._header.setStyleSheet(
            card_qss("skinsHeader")
            + f"QLabel#accountPill {{ background: {icon_tile_bg()};"
            f" border: 1px solid {border}; border-radius: 9px;"
            f" padding: 3px 12px; color: {fg()}; }}"
        )
        self._lib_card.setStyleSheet(card_qss("skinsLibraryCard"))
        self._apply_card.setStyleSheet(card_qss("skinsApplyCard"))
# ---------------------------------------------------------- refresh --
    def refresh_all(self) -> None:
        self._refresh_library()
        self._refresh_account()
        self._refresh_detected_capes()

    def _refresh_library(self) -> None:
        self.skin_list.blockSignals(True)
        self.skin_list.clear()
        active = self._lib.active_skin()
        for e in self._lib.skins():
            text = e.name + ("  ● Active" if active and e.id == active.id else "")
            it = QListWidgetItem(text)
            img = load_skin(self._lib._skin_path(e))
            if img is not None:
                it.setIcon(QIcon(render_head_thumb(img)))
            self.skin_list.addItem(it)
        self.skin_list.blockSignals(False)

        self._refresh_skin_buttons()

    def _refresh_skin_buttons(self) -> None:
        has = self._selected_skin_id() is not None
        self.set_active_btn.setEnabled(has)
        self.rename_btn.setEnabled(has)
        self.export_btn.setEnabled(self._lib.active_skin() is not None)
        self.delete_skin_btn.setEnabled(has)

    def _refresh_detected_cape_buttons(self) -> None:
        has = self._selected_detected_cape() is not None
        self.cape_set_active_btn.setEnabled(has)
        self.cape_hide_btn.setEnabled(has)

    def _refresh_account(self) -> None:
        account = self._current_account()
        if account is None:
            self._account_label.setText("No account selected")
            self._account_label.setToolTip(
                "Add & select an account in the Accounts tab.")
            self._refresh_apply_section()
            return
        kind = {"msa": "Microsoft", "ely": "Ely.by", "offline": "Offline"}.get(
            account.type, account.type)
        self._account_label.setText(f"{account.name} · {kind}")
        self._account_label.setToolTip(
            f"Active account: {account.name} ({kind})")
        self._refresh_apply_section()

    def _refresh_apply_section(self) -> None:
        """The single Apply button adapts to the active account:
        Microsoft -> server-side upload, Ely.by -> skinsystem upload,
        Offline -> CustomSkinLoader LocalSkin files in the selected instance.
        """
        account = self._current_account()
        if account is None:
            self.apply_btn.setText("Apply skin")
            self.apply_btn.setEnabled(False)
            self.csl_btn.setVisible(False)
            return
        if account.type == "msa":
            self.apply_btn.setText("Apply to Microsoft")
            self.csl_btn.setVisible(False)
        elif account.type == "ely":
            self.apply_btn.setText("Apply to Ely.by")
            self.csl_btn.setVisible(False)
        else:
            self.apply_btn.setText("Apply to game (LocalSkin)")
            self.csl_btn.setVisible(True)
        self.apply_btn.setEnabled(True)

    def _on_model_changed(self, _index: int) -> None:
        self._lib.model = str(self.model_combo.currentData() or "classic")
        self._lib._save()

    # ------------------------------------------------------ selection --
    def _selected_skin_id(self) -> str | None:
        item = self.skin_list.currentItem()
        row = self.skin_list.row(item)
        if row < 0:
            return None
        skins = self._lib.skins()
        return skins[row].id if row < len(skins) else None

    def _selected_detected_cape(self) -> dict | None:
        item = self.detected_cape_list.currentItem()
        idx = item.data(Qt.ItemDataRole.UserRole) if item else None
        if idx is None or not (0 <= idx < len(self._detected_capes)):
            return None
        return self._detected_capes[idx]

    def _current_account(self):
        if self._store is None:
            return None
        return self._store.current()

    def _current_player_name(self) -> str:
        acc = self._current_account()
        return acc.name if acc else ""

    def on_account_changed(self) -> None:
        """Called by the main window when the active account changes."""
        self._refresh_account()
        self._refresh_detected_capes()

    def refresh_theme(self) -> None:
        """Re-apply theme-dependent styling (called on live theme change)."""
        self._apply_theme()
        for list_widget in (self.skin_list, self.detected_cape_list):
            self._style_list(list_widget)
# ---------------------------------------------------------- capes --
    def _refresh_detected_capes(self) -> None:
        account = self._current_account()
        if self._store is None or account is None or account.type != "msa":
            self.detected_cape_list.clear()
            self._detected_capes = []
            if account is not None and account.type != "msa":
                self._capes_note.setText(
                    "Capes are managed on your Microsoft profile.")
            else:
                self._capes_note.setText("No Microsoft account selected.")
            self._refresh_detected_cape_buttons()
            return
        self._capes_note.setText("Detecting capes…")
        from ..threading_utils import retire
        retire(self._ms_thread)
        retire(self._thumb_thread)
        self._ms_thread = _MsProfileThread(self._store, account, self)
        self._ms_thread.ready.connect(self._on_profile_ready)
        self._ms_thread.failed.connect(self._on_profile_failed)
        self._ms_thread.start()

    def _on_profile_ready(self, profile: dict) -> None:
        from ..threading_utils import retire
        retire(self._ms_thread)
        self._ms_thread = None
        self._profile = profile
        self._populate_detected_capes()

    def _on_profile_failed(self, message: str) -> None:
        from ..threading_utils import retire
        retire(self._ms_thread)
        self._ms_thread = None
        self._capes_note.setText(f"Could not load profile: {message[:120]}")
        self._log(f"Skin/cape profile load failed: {message}", "WARN")

    def _populate_detected_capes(self) -> None:
        self.detected_cape_list.blockSignals(True)
        self.detected_cape_list.clear()
        self._detected_capes = list((self._profile or {}).get("capes", []) or [])
        if not self._detected_capes:
            self._capes_note.setText(
                "No capes found on the profile — upload a skin or "
                "bring capes via Minecraft.")
        else:
            self._capes_note.setText(
                f"{len(self._detected_capes)} cape(s) detected on the profile.")
        for idx, cape in enumerate(self._detected_capes):
            alias = str(cape.get("alias") or cape.get("id") or f"Cape {idx + 1}")
            active = "  ● Active" if cape.get("state") == "ACTIVE" else ""
            it = QListWidgetItem(alias + active)
            it.setData(Qt.ItemDataRole.UserRole, idx)
            # Pin the selection box to the full tile (thumb + name), instead
            # of letting it hug the icon/text via the default sizeHint.
            it.setSizeHint(self.detected_cape_list.gridSize())
            self.detected_cape_list.addItem(it)
        self.detected_cape_list.blockSignals(False)
        self._refresh_detected_cape_buttons()

        urls = [c.get("url") for c in self._detected_capes]
        if any(urls):
            self._thumb_thread = _CapeThumbThread(urls, self)
            self._thumb_thread.thumb.connect(self._on_cape_thumb)
            self._thumb_thread.start()

    def _on_cape_thumb(self, index: int, img: QImage) -> None:
        item = self.detected_cape_list.item(index)
        if item is None:
            return
        # Crisp nearest-neighbour render of the full 22x17 cape region.
        item.setIcon(QIcon(render_cape_thumb(img, self._CAPE_ZOOM)))

    def _on_refresh_detected_capes(self) -> None:
        self._refresh_detected_capes()

    def _on_detected_cape_set_active(self) -> None:
        cape = self._selected_detected_cape()
        account = self._current_account()
        if not cape or not account:
            return
        label = str(cape.get("alias") or cape.get("id") or "cape")

        def fn():
            _, _, token = self._store.ensure_access_token(account)
            ms.set_active_cape(token, str(cape["id"]))
            return f"'{label}' is now your active cape."

        self._run_apply(fn, refresh_ms=True)

    def _on_detected_cape_hide(self) -> None:
        account = self._current_account()
        if not account:
            return

        def fn():
            _, _, token = self._store.ensure_access_token(account)
            ms.hide_cape(token)
            return "Active cape hidden."

        self._run_apply(fn, refresh_ms=True)
# ------------------------------------------------------ library slots --
    def _on_upload_skin(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a Minecraft skin", "",
            "PNG images (*.png);;All files (*)")
        if not path:
            return
        img = load_skin(path)
        if img is None:
            self._toast("error", "Invalid skin",
                        "Not a 64×64 (or legacy 64×32) Minecraft skin PNG.", 6000)
            return
        entry = self._lib.add_skin(path)
        self._log(f"Skin '{entry.name}' saved to the library.")
        self._refresh_library()
        self._toast("success", "Skin uploaded", f"'{entry.name}' is now active.", 4000)

    def _on_set_active_skin(self) -> None:
        entry_id = self._selected_skin_id()
        if not entry_id:
            return
        self._lib.set_active_skin(entry_id)
        self._refresh_library()

    def _on_rename_skin(self) -> None:
        from PyQt6.QtCore import QTimer

        def _perform() -> None:
            entry_id = self._selected_skin_id()
            if not entry_id:
                return
            entry = next(
                (e for e in self._lib.skins() if e.id == entry_id), None)
            if entry is None:
                return
            # Hide the list so the modal's Fluent fade effect can't grab a
            # pixmap of it mid-repaint (same pattern as the accounts page).
            self.skin_list.setVisible(False)
            try:
                dialog = _RenameDialog(self.window(), entry.name)
                if dialog.exec():
                    new_name = dialog.name_edit.text().strip()
                    self._lib.rename_skin(entry_id, new_name)
                    self._log(f"Skin renamed to '{new_name}'.")
                    self._refresh_library()
            finally:
                self.skin_list.setVisible(True)

        QTimer.singleShot(0, _perform)

    def _on_export_skin(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        entry = self._lib.active_skin()
        if not entry:
            self._toast("info", "No skin", "Upload a skin first.", 4000)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export active skin", f"{entry.name}.png", "PNG images (*.png)")
        if not path:
            return
        try:
            shutil.copyfile(self._lib._skin_path(entry), path)
        except OSError as exc:
            self._toast("error", "Export failed", str(exc), 6000)
            return
        self._toast("success", "Exported", f"Saved to {path}", 5000)

    def _on_delete_skin(self) -> None:
        entry_id = self._selected_skin_id()
        if not entry_id:
            return
        self._lib.remove_skin(entry_id)
        self._refresh_library()

    # ----------------------------------------------------------- apply --
    def _run_apply(self, fn, refresh_ms: bool = False) -> None:
        from ..threading_utils import retire
        retire(self._apply_thread)
        thread = _ApplyThread(fn, self)

        def on_done(message: str) -> None:
            self._log(message)
            self._toast("success", "Applied", message, 5000)
            if refresh_ms:
                self._refresh_detected_capes()
            retire(self._apply_thread)
            self._apply_thread = None

        def on_failed(message: str) -> None:
            self._log(message, "ERROR")
            self._toast("error", "Apply failed", message[:250], 8000)
            retire(self._apply_thread)
            self._apply_thread = None

        thread.done.connect(on_done)
        thread.failed.connect(on_failed)
        self._apply_thread = thread
        thread.start()

    def _on_apply(self) -> None:
        account = self._current_account()
        if not account:
            self._toast("warning", "No account",
                        "Add & select an account in the Accounts tab first.", 6000)
            return
        path = self._lib.active_skin_path()
        if not path:
            self._toast("warning", "No skin", "Upload a skin first.", 6000)
            return

        if account.type == "msa":
            def fn_msa():
                _, _, token = self._store.ensure_access_token(account)
                with open(path, "rb") as fh:
                    data = fh.read()
                ms.upload_skin(token, data, self._lib.model)
                return "Skin uploaded to your Microsoft profile."
            self._run_apply(fn_msa, refresh_ms=True)
            return

        if account.type == "ely":
            def fn_ely():
                from .. import elyby
                _, _, token = self._store.ensure_access_token(account)
                with open(path, "rb") as fh:
                    data = fh.read()
                elyby.upload_skin(token, account.uuid, data, self._lib.model)
                return "Skin uploaded to your Ely.by profile."
            self._run_apply(fn_ely)
            return

        self._on_apply_to_game()

    def _on_apply_to_game(self) -> None:
        inst = self._get_instance() if self._get_instance else None
        if inst is None:
            self._toast("warning", "No instance",
                        "Select an instance first (Instances tab).", 6000)
            return
        player = self._current_player_name()
        if not player:
            self._toast("warning", "No account",
                        "An account is needed to name the LocalSkin files.", 6000)
            return
        skin_path = self._lib.active_skin_path()
        cape_path = self._lib.active_cape_path()
        if not skin_path and not cape_path:
            self._toast("warning", "Nothing to apply",
                        "Upload a skin and/or cape first.", 6000)
            return

        def fn():
            base = os.path.join(inst.dir, "CustomSkinLoader", "LocalSkin")
            if skin_path:
                folder = os.path.join(base, "skins")
                os.makedirs(folder, exist_ok=True)
                shutil.copyfile(skin_path, os.path.join(folder, f"{player}.png"))
            if cape_path:
                folder = os.path.join(base, "capes")
                os.makedirs(folder, exist_ok=True)
                shutil.copyfile(cape_path, os.path.join(folder, f"{player}.png"))
            return (f"LocalSkin written for '{player}' in '{inst.name}'. "
                    "Rejoin/reopen to see it (CustomSkinLoader mod required).")

        self._run_apply(fn)

    def _on_install_csl(self) -> None:
        inst = self._get_instance() if self._get_instance else None
        if inst is None:
            self._toast("warning", "No instance",
                        "Select an instance first (Instances tab).", 6000)
            return
        if self._csl_thread is not None and self._csl_thread.isRunning():
            return
        from ..threading_utils import retire
        retire(self._csl_thread)
        self._status_label.setText(
            f"Installing CustomSkinLoader into '{inst.name}'…")
        self._toast("info", "Installing mod",
                    "Downloading CustomSkinLoader from Modrinth.", 4000)
        thread = ModInstallThread(
            SOURCE_MODRINTH,
            CUSTOM_SKIN_LOADER_SLUG,
            inst.mc_version,
            inst.loader_id,
            inst.mods_dir,
            parent=self,
        )

        def on_status(text: str) -> None:
            self._status_label.setText(text)

        def on_done(names) -> None:
            self._status_label.setText("CustomSkinLoader installed.")
            self._log(f"CustomSkinLoader installed into '{inst.name}': {names}")
            self._toast("success", "Installed",
                        "CustomSkinLoader is ready. Use 'Apply to game' for your skin.", 6000)
            from ..threading_utils import retire
            retire(self._csl_thread)
            self._csl_thread = None

        def on_failed(message: str) -> None:
            self._status_label.setText("Install failed.")
            self._log(message, "ERROR")
            self._toast("error", "Install failed", message[:250], 8000)
            from ..threading_utils import retire
            retire(self._csl_thread)
            self._csl_thread = None

        thread.status.connect(on_status)
        thread.succeeded.connect(on_done)
        thread.failed.connect(on_failed)
        self._csl_thread = thread
        thread.start()
