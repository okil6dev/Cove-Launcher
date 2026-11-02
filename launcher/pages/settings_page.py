"""Settings page: an icon rail beside one section per group of settings.

Layout
------
[ rail ] | [ Settings                                    ]
[icon  ] | [ <Section heading>                           ]
[icons ] | [ ...that section's controls, scrollable...   ]
[icons ] | [ Save settings                               ]

Same idea as the Mods page rail (``ModsPage``), but compact and icon-only:
36px buttons in a 44px column instead of ~120px of text labels. The name of
each section lives in its tooltip and in the heading the section paints, so
nothing is lost by dropping the labels -- and the settings themselves are
grouped instead of being one long scroll of unrelated rows.

Backend contract unchanged: the page still exposes the same widget
attributes (``theme_combo``, ``java_edit``, ``size_apply_btn``, ...), the
same methods (``load``, ``_on_save``, ``system_java``, ``refresh_theme``)
and the same object name, so LauncherWindow and the window-size tests work
without knowing that a rail exists.
"""

from __future__ import annotations

import os
import shutil
import webbrowser
from typing import Any

import requests

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QFontMetrics, QPainter
from PyQt6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    FluentIcon,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    SubtitleLabel,
    SwitchButton,
    ToolButton,
    setThemeColor,
    themeColor,
)


from .. import minecraft_options
from ..constants import MS_REDIRECT_URI
from ..instances import validate_java_executable
from ..page_scroll import scrollable_page_root
from ..security import Vault
from ..theme import THEME_OPTIONS, normalize
from ..window_sizes import (
    MAX_HEIGHT,
    MAX_WIDTH,
    VAULT_KEY,
    WINDOW_SPECS,
    clamp_size,
    default_sizes,
    load_sizes,
    save_sizes,
)

_DEFAULT_ACCENT = "#89DCFF"  # baby blue

#: Padding either side of a global option's number box, on top of the widest
#: number that option allows: room for the spinner's buttons and the field's own
#: margins, so the number is never clipped however the row is sized.
_NUMBER_BOX_PADDING = 48

# Where a CurseForge API key is obtained today. There is no self-serve dashboard:
# 3rd party developers apply to Overwolf via this support article (form -> review -> key by email).
CURSEFORGE_API_KEY_INFO_URL = "https://support.curseforge.com/en/support/solutions/articles/9000208346-about-the-curseforge-api-and-how-to-apply-for-a-key"
CURSEFORGE_API = "https://api.curseforge.com/v1"

#: (key, heading, icon, one-line description) in rail order. The key is what
#: ``show_section`` takes; the heading is both the section title and the rail
#: button's tooltip, since the rail itself is icon-only.
SECTIONS: tuple[tuple[str, str, object, str], ...] = (
    ("appearance", "Appearance", FluentIcon.PALETTE,
     "Theme and accent colour for the whole launcher."),
    ("minecraft", "Minecraft", FluentIcon.GAME,
     "Game options this launcher enforces in every instance."),
    ("keys", "API Keys", FluentIcon.GLOBE,
     "Optional CurseForge and Ely.by credentials."),
    ("java", "Java", FluentIcon.CODE,
     "The Java runtime used as fallback and by modloader installers."),
    ("window", "Window Sizes", FluentIcon.FIT_PAGE,
     "Starting size of each window, in pixels."),
)


def section_spec(key: str) -> tuple:
    """The :data:`SECTIONS` row for *key*.

    Sections are looked up by key rather than by position: a builder that used
    ``SECTIONS[1]`` silently kept the wrong heading (and the wrong controls)
    the moment a new entry was inserted into the rail.
    """
    for spec in SECTIONS:
        if spec[0] == key:
            return spec
    raise KeyError(key)


def _build_qss() -> str:
    """Page/rail transparency (rebuilt by :meth:`SettingsPage.refresh_theme`).

    Deliberately narrow selectors: a broad ``QWidget`` rule here would blank
    the input fields and spin boxes the sections are built from. The active
    button's pill is *not* styled here -- see :class:`_RailButton` for why.
    """
    return """
#settingsPage {
    background: transparent;
    border: none;
}
#settingsRail {
    background: transparent;
    border: none;
}
QToolButton#railIcon {
    background: transparent;
    border: none;
}
"""


def _rail_pill_colour() -> QColor:
    """Tint behind the active rail button, derived from the live accent colour.

    Computed at paint time rather than cached, so a theme or accent change is
    picked up by the next repaint.

    Alpha is per-theme on purpose (measured on the real window, default accent):
    blended over a dark surface an opaque accent leaves the near-white icon at
    only 2.4:1, while 90 keeps it at 3.9:1 and still stands out from the page
    (2.9:1). Over a light surface the default pale accent at any useful alpha is
    invisible (1.1:1), so there the pill is the accent itself -- which the dark
    icon reads at 11:1.
    """
    from ..theme import is_dark_now

    colour = QColor(themeColor())
    colour.setAlpha(90 if is_dark_now() else 255)
    return colour


class _RailButton(ToolButton):
    """Icon-only rail button whose active pill is painted, not styled.

    A ``QToolButton#railIcon:checked { background-color: ... }`` rule looks
    right and is what the Mods page rail does for text, but it does not paint
    here: the Mica/Acrylic themes put a window-wide
    ``QWidget { background: transparent; border: none; }`` sheet over every
    page, and toggling ``checked`` then left every rail pixel byte-identical
    (measured on the real window -- the stylesheet background never appeared,
    while Fluent's own painted states did). Painting the pill keeps the active
    section visible in every theme.
    """

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.isChecked():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_rail_pill_colour())
            painter.drawRoundedRect(self.rect(), 8, 8)
            painter.end()
        super().paintEvent(event)


class SettingsPage(QWidget):
    """Global launcher settings; secrets persist only in the vault."""

    def __init__(
        self,
        vault: Vault,
        log,
        toast,
        get_instances=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.setStyleSheet(_build_qss())
        self._vault = vault
        self._log = log
        self._toast = toast
        # callable -> list[Instance] (the window owns the registry). Optional so
        # the page can still be built standalone (tests, dialogs).
        self._get_instances = get_instances
        self._current_accent: str = _DEFAULT_ACCENT
        self._section_key: str = SECTIONS[0][0]

        # The rail and the Save button sit outside the per-section scroll areas,
        # so switching section or scrolling a long one never moves them.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(12, 16, 20, 12)
        body.setSpacing(14)
        outer.addLayout(body, stretch=1)

        # -- left icon rail ------------------------------------------------------
        self._rail_buttons: dict[str, ToolButton] = {}
        rail = QWidget()
        rail.setObjectName("settingsRail")
        rail.setFixedWidth(44)  # 36px button + 4px margins: compact by design
        rail_lay = QVBoxLayout(rail)
        rail_lay.setContentsMargins(4, 4, 4, 4)
        rail_lay.setSpacing(6)
        for key, heading, icon, _description in SECTIONS:
            button = self._make_rail_button(key, heading, icon)
            self._rail_buttons[key] = button
            rail_lay.addWidget(button)
        rail_lay.addStretch(1)
        body.addWidget(rail)

        # -- content column ------------------------------------------------------
        column = QVBoxLayout()
        column.setSpacing(10)
        body.addLayout(column, stretch=1)

        column.addWidget(SubtitleLabel("Settings"))

        # One page per section; the rail switches between them.
        self.section_stack = QStackedWidget()
        self._section_index: dict[str, int] = {}
        column.addWidget(self.section_stack, stretch=1)

        self._build_appearance_section()
        self._build_minecraft_section()
        self._build_keys_section()
        self._build_java_section()
        self._build_window_section()

        # Save stays below the stack on purpose: it stores every section at
        # once, whichever one happens to be on screen.
        save_row = QHBoxLayout()
        self.save_button = PrimaryPushButton(FluentIcon.SAVE, "Save settings")
        save_row.addWidget(self.save_button)
        save_row.addStretch(1)
        column.addLayout(save_row)

        self.java_browse.clicked.connect(self._on_browse)
        self.java_test.clicked.connect(self._on_test_java)
        self.java_detect_btn.clicked.connect(self._on_detect_java)
        self.save_button.clicked.connect(self._on_save)
        self.cf_test_btn.clicked.connect(self._on_test_cf_key)
        self.cf_getkey_btn.clicked.connect(self._on_get_cf_key)
        self.size_apply_btn.clicked.connect(self._on_apply_sizes)
        self.size_reset_btn.clicked.connect(self._on_reset_sizes)
        self.load()
        self.show_section(self._section_key)

    # -- rail -----------------------------------------------------------------
    def _make_rail_button(self, key: str, heading: str, icon) -> ToolButton:
        """One icon-only rail button; *heading* becomes its tooltip."""
        button = _RailButton(icon)
        button.setObjectName("railIcon")
        button.setCheckable(True)
        button.setChecked(key == self._section_key)
        button.setFixedSize(36, 36)
        button.setIconSize(QSize(18, 18))
        button.setToolTip(heading)
        button.setAccessibleName(heading)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _checked=False, _key=key: self.show_section(_key))
        return button

    def show_section(self, key: str) -> None:
        """Show the section *key* and mark its rail button.

        Checked state is set here rather than by toggling buttons, so clicking
        the already-active section cannot leave the rail with nothing lit.
        """
        index = self._section_index.get(key)
        if index is None:
            return
        self._section_key = key
        self.section_stack.setCurrentIndex(index)
        for name, button in self._rail_buttons.items():
            button.setChecked(name == key)

    def current_section(self) -> str:
        """Key of the section currently on screen."""
        return self._section_key

    def _add_section(self, spec) -> QVBoxLayout:
        """Register the section *spec* (a :data:`SECTIONS` row) and return its layout.

        Each section gets its own scroll area (transparent, like every other
        page): a section taller than the window scrolls on its own instead of
        dragging the rail and Save button off-screen with it.
        """
        key, heading, _icon, description = spec
        page = QWidget()
        page.setObjectName("settingsSection")
        self._section_index[key] = self.section_stack.addWidget(page)
        lay = scrollable_page_root(page, margins=(2, 2, 8, 2), spacing=10)
        lay.addWidget(SubtitleLabel(heading))
        if description:
            lay.addWidget(BodyLabel(description))
        return lay

    # -- sections ----------------------------------------------------------------
    def _build_appearance_section(self) -> None:
        lay = self._add_section(section_spec("appearance"))

        # Theme ------------------------------------------------------------------
        lay.addWidget(BodyLabel(
            "Theme — applied across the entire launcher UI "
            "(Mica / Acrylic need Windows 11)"
        ))
        self.theme_combo = ComboBox()
        for theme_label, theme_key in THEME_OPTIONS:
            self.theme_combo.addItem(theme_label, userData=theme_key)
        self.theme_combo.setMinimumWidth(220)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        theme_row = QHBoxLayout()
        theme_row.addWidget(self.theme_combo)
        theme_row.addStretch(1)
        lay.addLayout(theme_row)

        # Accent Colour ----------------------------------------------------------
        lay.addWidget(BodyLabel("Accent colour — applied across the entire launcher UI"))
        accent_row = QHBoxLayout()

        # Colour swatch
        self._swatch = QFrame()
        self._swatch.setFixedSize(36, 36)
        self._swatch.setFrameShape(QFrame.Shape.Box)
        self._swatch.setFrameShadow(QFrame.Shadow.Plain)
        self._swatch.setLineWidth(1)
        accent_row.addWidget(self._swatch)

        self._accent_label = BodyLabel(_DEFAULT_ACCENT)
        self._accent_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        accent_row.addWidget(self._accent_label, stretch=1)

        self._pick_btn = PushButton(FluentIcon.PALETTE, "Pick Colour")
        self._pick_btn.clicked.connect(self._on_pick_accent)
        accent_row.addWidget(self._pick_btn)

        self._reset_btn = PushButton(FluentIcon.SYNC, "Reset")
        self._reset_btn.setToolTip(f"Reset to default: {_DEFAULT_ACCENT}")
        self._reset_btn.clicked.connect(self._on_reset_accent)
        accent_row.addWidget(self._reset_btn)

        lay.addLayout(accent_row)
        self._apply_accent(_DEFAULT_ACCENT, live=False)
        lay.addStretch(1)

    def _build_minecraft_section(self) -> None:
        lay = self._add_section(section_spec("minecraft"))
        lay.addWidget(BodyLabel(
            "These are written into every instance's own options.txt and "
            "re-applied each time a game starts, so a new instance -- or one "
            "whose options.txt was reset -- still obeys them. Nothing is "
            "touched while a switch is off."
        ))

        # One row per entry in the shared OPTIONS table: adding a global option
        # there is all it takes for it to show up here. A "number" option also
        # gets a box beside its switch, holding the value to write.
        self._global_switches: dict[str, SwitchButton] = {}
        self._global_spins: dict[str, SpinBox] = {}
        for option in minecraft_options.OPTIONS:
            row = QHBoxLayout()
            text = QVBoxLayout()
            text.setSpacing(2)
            name = BodyLabel(option.label)
            hint = BodyLabel(option.description)
            hint.setWordWrap(True)
            text.addWidget(name)
            text.addWidget(hint)
            if option.hint:
                extra = BodyLabel(option.hint)
                extra.setWordWrap(True)
                text.addWidget(extra)
            row.addLayout(text, stretch=1)

            switch = SwitchButton()
            switch.setToolTip(option.description)
            switch.setAccessibleName(option.label)
            self._global_switches[option.key] = switch

            if option.kind == "number":
                # The number sits between the text and the switch, so the
                # control that turns the option on stays put on every row.
                spin = self._make_number_box(option)
                self._global_spins[option.key] = spin
                row.addWidget(spin, alignment=Qt.AlignmentFlag.AlignVCenter)
                switch.checkedChanged.connect(
                    lambda on, key=option.key: self._on_global_switch(key, on))

            row.addWidget(switch, alignment=Qt.AlignmentFlag.AlignVCenter)
            lay.addLayout(row)

        apply_row = QHBoxLayout()
        self.global_apply_btn = PushButton(
            FluentIcon.SYNC, "Apply to all instances")
        self.global_apply_btn.setToolTip(
            "Write the switches above into every instance's options.txt now "
            "(Save settings does this too)"
        )
        self.global_apply_btn.clicked.connect(self._on_apply_global_options)
        apply_row.addWidget(self.global_apply_btn)
        self.global_status_label = BodyLabel("")
        self.global_status_label.setWordWrap(True)
        apply_row.addWidget(self.global_status_label, stretch=1)
        lay.addLayout(apply_row)
        lay.addStretch(1)

    def _make_number_box(self, option) -> SpinBox:
        """The number box for a numeric global option.

        Deliberately roomy. The value is one or two digits, but a clipped field
        is exactly the annoyance this box exists to remove, and it has to stay
        readable with a larger system font or with the spinner buttons over its
        right-hand end -- so the width comes from the widest number the option
        allows plus :data:`_NUMBER_BOX_PADDING`, and is never below what the
        style itself asks for.
        """
        spin = SpinBox()
        spin.setRange(option.minimum, option.maximum)
        spin.setValue(option.initial)
        spin.setAccessibleName(option.label)
        spin.setToolTip(
            f"{option.label}: {option.minimum}-{option.maximum} "
            "(used while its switch is on)"
        )
        needed = (
            QFontMetrics(spin.font()).horizontalAdvance(str(option.maximum))
            + _NUMBER_BOX_PADDING
        )
        spin.setMinimumWidth(max(spin.sizeHint().width(), needed))
        return spin

    def _on_global_switch(self, key: str, checked: bool) -> None:
        """A number box only means something while its switch is on."""
        spin = getattr(self, "_global_spins", {}).get(key)
        if spin is not None:
            spin.setEnabled(bool(checked))

    def _build_keys_section(self) -> None:
        lay = self._add_section(section_spec("keys"))

        # CurseForge ------------------------------------------------------------
        lay.addWidget(BodyLabel(
            "CurseForge API Key (optional) — PrismLauncher's built-in key is "
            "used automatically, so CurseForge works with no setup. "
            "Set your own only to override it."
        ))
        cf_row = QHBoxLayout()
        self.cf_key_edit = PasswordLineEdit()
        self.cf_key_edit.setPlaceholderText("blank = use built-in key")
        cf_row.addWidget(self.cf_key_edit, stretch=1)
        self.cf_test_btn = PushButton(FluentIcon.INFO, "Test key")
        cf_row.addWidget(self.cf_test_btn)
        self.cf_getkey_btn = PushButton(FluentIcon.LINK, "Get a key")
        cf_row.addWidget(self.cf_getkey_btn)
        lay.addLayout(cf_row)
        self.cf_result_label = BodyLabel("")
        self.cf_result_label.setWordWrap(True)
        lay.addWidget(self.cf_result_label)

        # Ely.by ----------------------------------------------------------------
        lay.addWidget(BodyLabel(
            "Ely.by client ID (optional) — only needed if you registered a "
            "custom OAuth app at account.ely.by"
        ))
        self.ely_client_id_edit = LineEdit()
        self.ely_client_id_edit.setPlaceholderText(
            "blank = built-in default"
        )
        lay.addWidget(self.ely_client_id_edit)
        lay.addStretch(1)

    def _build_java_section(self) -> None:
        lay = self._add_section(section_spec("java"))

        # Default Java -----------------------------------------------------------
        lay.addWidget(BodyLabel(
            "System Java executable — used as fallback and for modloader installers"
        ))
        java_row = QHBoxLayout()
        self.java_edit = LineEdit()
        self.java_edit.setPlaceholderText(find_java_hint())
        self.java_browse = PushButton(FluentIcon.FOLDER, "Browse...")
        self.java_test = PushButton(FluentIcon.INFO, "Test")
        self.java_detect_btn = PushButton(FluentIcon.SEARCH, "Detect...")
        self.java_detect_btn.setToolTip(
            "Scan this machine for all Java installations (like PrismLauncher)"
        )
        java_row.addWidget(self.java_edit, stretch=1)
        java_row.addWidget(self.java_detect_btn)
        java_row.addWidget(self.java_browse)
        java_row.addWidget(self.java_test)
        lay.addLayout(java_row)

        self.java_result_label = BodyLabel("")
        self.java_result_label.setWordWrap(True)
        lay.addWidget(self.java_result_label)
        lay.addStretch(1)

    def _build_window_section(self) -> None:
        lay = self._add_section(section_spec("window"))
        lay.addWidget(BodyLabel(
            "\"Apply sizes\" stores them and resizes the main window right away; "
            "the editors pick their size up the next time they open."
        ))
        # key -> (width spin box, height spin box)
        self._size_spins: dict[str, tuple[SpinBox, SpinBox]] = {}

        # Compute initial sizes BEFORE creating spin boxes so they never paint
        # minimums (900) then jump to defaults (1280) — that's the flicker.
        initial_sizes = load_sizes(self._vault)

        for spec in WINDOW_SPECS:
            row = QHBoxLayout()
            name = BodyLabel(spec.label)
            name.setMinimumWidth(150)
            row.addWidget(name)

            # Use the pre-loaded size (or default) as the initial value so the
            # spin box paints the correct number on its very first frame.
            init_w, init_h = initial_sizes.get(spec.key, (spec.default_w, spec.default_h))

            w_spin = SpinBox()
            w_spin.setRange(spec.min_w, MAX_WIDTH)
            w_spin.setFixedWidth(150)  # wider so even large numbers fit comfortably
            w_spin.setValue(init_w)
            h_spin = SpinBox()
            h_spin.setRange(spec.min_h, MAX_HEIGHT)
            h_spin.setFixedWidth(150)
            h_spin.setMinimumHeight(30)  # taller so numbers aren't cramped vertically
            h_spin.setValue(init_h)
            w_spin.setValue(init_w)

            row.addWidget(w_spin)
            row.addWidget(BodyLabel("×"))
            row.addWidget(h_spin)
            row.addWidget(BodyLabel("px"))
            row.addWidget(BodyLabel(
                f"default {spec.default_w} × {spec.default_h}"
            ))
            row.addStretch(1)
            lay.addLayout(row)
            self._size_spins[spec.key] = (w_spin, h_spin)

        size_buttons = QHBoxLayout()
        self.size_apply_btn = PushButton(FluentIcon.SAVE, "Apply sizes")
        self.size_reset_btn = PushButton(FluentIcon.SYNC, "Reset sizes")
        self.size_reset_btn.setToolTip(
            "Put the built-in defaults back in the boxes (still needs Apply sizes)"
        )
        size_buttons.addWidget(self.size_apply_btn)
        size_buttons.addWidget(self.size_reset_btn)
        size_buttons.addStretch(1)
        lay.addLayout(size_buttons)
        lay.addStretch(1)

    # -- persistence ----------------------------------------------------------
    def load(self) -> None:
        data = self._vault.read()
        self.cf_key_edit.setText(str(data.get("curseforge_api_key", "") or ""))
        self.ely_client_id_edit.setText(str(data.get("ely_client_id", "") or ""))
        self.java_edit.setText(str(data.get("system_java", "") or ""))
        saved_colour = str(data.get("accent_colour", "") or "")
        colour = saved_colour if saved_colour else _DEFAULT_ACCENT
        self._apply_accent(colour, live=True)

        saved_theme = normalize(str(data.get("theme", "") or ""))
        index = self.theme_combo.findData(saved_theme)
        if index >= 0:
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentIndex(index)
            self.theme_combo.blockSignals(False)

        self._set_size_spins(load_sizes(self._vault))

        self._set_global_switches(minecraft_options.load(self._vault))
        self._update_global_status()

    # -- theme helpers -----------------------------------------------------------
    def refresh_theme(self) -> None:
        """Re-apply theme-dependent styling (called on live theme change)."""
        self.setStyleSheet(_build_qss())
        # The pill colour comes from the live theme/accent, so it has to be
        # repainted rather than restyled.
        for button in self._rail_buttons.values():
            button.update()
        self._apply_accent(self._current_accent, live=False)

    def _on_theme_changed(self, index: int) -> None:
        """Apply the newly selected theme to the whole launcher immediately."""
        key = self.theme_combo.itemData(index)
        if not key:
            return
        window = self.window()
        if hasattr(window, "apply_theme_setting"):
            window.apply_theme_setting(str(key))

    def _current_theme(self) -> str:
        key = self.theme_combo.currentData()
        return normalize(str(key) if key else "")

    def _on_save(self) -> None:
        data = self._vault.read()
        data["curseforge_api_key"] = self.cf_key_edit.text().strip()
        data["ely_client_id"] = self.ely_client_id_edit.text().strip()
        if not self.java_edit.text().strip():
            import shutil

            which = shutil.which("java")
            data["system_java"] = which or ""
        else:
            data["system_java"] = self.java_edit.text().strip()
        data["accent_colour"] = self._current_accent
        data["theme"] = self._current_theme()
        data[VAULT_KEY] = {
            key: [w, h] for key, (w, h) in self._current_sizes().items()
        }
        self._vault.write(data)
        self._log("Settings saved to encrypted vault.")
        # The Minecraft tab keeps its own choices under its own vault key and
        # pushes them into the instances. Done *after* the write above so the
        # two read-modify-write passes cannot clobber each other.
        self._apply_global_options()
        self._toast("success", "Saved", "Settings stored securely.", 4000)

    # -- global Minecraft options ----------------------------------------------
    def _set_global_switches(self, choices) -> None:
        """Show the stored *choices* without firing change handlers."""
        for option in minecraft_options.OPTIONS:
            switch = getattr(self, "_global_switches", {}).get(option.key)
            if switch is None:
                continue
            value = minecraft_options.coerce(option, choices.get(option.key))
            switch.blockSignals(True)
            switch.setChecked(minecraft_options.is_enforced(option, value))
            switch.blockSignals(False)

            spin = getattr(self, "_global_spins", {}).get(option.key)
            if spin is None:
                continue
            if value is not None:
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
            spin.setEnabled(switch.isChecked())

    def _global_choices(self) -> dict[str, Any]:
        """The rows as they are on screen right now.

        A number option whose switch is off reports ``None``: that means "stop
        managing this setting", which is not the same as "manage it with 0".
        """
        choices: dict[str, Any] = {}
        for option in minecraft_options.OPTIONS:
            switch = getattr(self, "_global_switches", {}).get(option.key)
            if switch is None:
                continue
            if option.kind == "toggle":
                choices[option.key] = switch.isChecked()
            else:
                spin = self._global_spins[option.key]
                choices[option.key] = spin.value() if switch.isChecked() else None
        return choices

    def _instances(self) -> list:
        """Every configured instance (empty when no registry was supplied)."""
        if self._get_instances is None:
            return []
        try:
            return list(self._get_instances() or [])
        except Exception:  # noqa: BLE001 - a registry hiccup must not break Save
            return []

    def _update_global_status(self, changed: list | None = None) -> None:
        """Say what the switches mean for the instances that exist right now."""
        total = len(self._instances())
        if not total:
            text = "No instances yet — these apply as soon as you launch one."
        elif changed:
            text = f"Applied to {len(changed)} of {total} instances."
        else:
            text = f"All {total} instances already match."
        self.global_status_label.setText(text)

    def _apply_global_options(self) -> list:
        """Store the rows and write them into every instance's options.txt.

        Returns the names of the instances whose file changed. Options that have
        been switched off since the last save get vanilla's default written back,
        so un-muting really un-mutes and a pinned GUI scale really goes back to
        Auto.
        """
        previous = minecraft_options.load(self._vault)
        choices = self._global_choices()
        # Same rule for both kinds of option: "switched off" means it was
        # managed before this save and is not managed now.
        turned_off = [
            option.key for option in minecraft_options.OPTIONS
            if minecraft_options.is_enforced(option, previous.get(option.key))
            and not minecraft_options.is_enforced(option, choices.get(option.key))
        ]
        minecraft_options.save(self._vault, choices)
        instances = self._instances()
        changed = minecraft_options.sync(instances, choices, turn_off=turned_off)
        self._update_global_status(changed)
        return changed

    def _on_apply_global_options(self) -> None:
        """Apply to all instances on demand, with the outcome reported."""
        changed = self._apply_global_options()
        total = len(self._instances())
        self._log(
            "Global Minecraft options applied to "
            f"{len(changed)} of {total} instance(s)."
        )
        if not total:
            message = "No instances yet - these apply when you launch one."
        elif changed:
            message = (f"Updated {len(changed)} instance(s); the rest were "
                       "already set.")
        else:
            message = "Every instance already matches these settings."
        self._toast(
            "success" if changed else "info",
            "Minecraft settings",
            message,
            5000,
        )

    # -- window sizes -----------------------------------------------------------
    def _current_sizes(self) -> dict[str, tuple[int, int]]:
        """Spin-box values, clamped to each window's limits."""
        return {
            key: clamp_size(key, w_spin.value(), h_spin.value())
            for key, (w_spin, h_spin) in self._size_spins.items()
        }

    def _set_size_spins(self, sizes) -> None:
        """Push *sizes* into the spin boxes (unknown keys are ignored)."""
        for key, (w, h) in sizes.items():
            spins = self._size_spins.get(key)
            if spins is None:
                continue
            spins[0].setValue(w)
            spins[1].setValue(h)

    def _on_apply_sizes(self) -> None:
        """Persist the sizes and resize the main window immediately."""
        sizes = self._current_sizes()
        self._set_size_spins(sizes)  # show the clamped values back to the user
        try:
            save_sizes(self._vault, sizes)
        except Exception as exc:  # noqa: BLE001 - vault may be locked
            self._log(f"Could not save window sizes: {exc}", "WARNING")
            self._toast("error", "Window sizes", f"Could not save: {exc}", 8000)
            return
        window = self.window()
        if hasattr(window, "apply_window_sizes"):
            window.apply_window_sizes(sizes)
        self._log(
            "Window sizes applied: "
            + ", ".join(f"{k} {w}x{h}" for k, (w, h) in sizes.items())
        )
        self._toast(
            "success",
            "Window sizes",
            "Main window resized now; editors use theirs when reopened.",
            5000,
        )

    def _on_reset_sizes(self) -> None:
        """Fill the boxes with the built-in defaults (still needs Apply)."""
        self._set_size_spins(default_sizes())
        self._toast(
            "info",
            "Window sizes",
            "Defaults restored — click Apply sizes to use them.",
            5000,
        )

    # -- accent colour helpers -------------------------------------------------
    def _apply_accent(self, hex_colour: str, *, live: bool = True) -> None:
        """Update the swatch, label, internal state and (optionally) the live theme."""
        from ..theme import hairline

        self._current_accent = hex_colour
        self._accent_label.setText(hex_colour.upper())
        # Swatch background
        self._swatch.setStyleSheet(
            f"background-color: {hex_colour}; border-radius: 4px; border: 1px solid {hairline()};"
        )
        if live:
            setThemeColor(QColor(hex_colour))

    def _on_pick_accent(self) -> None:
        """Open the system colour picker and apply the selection immediately."""
        initial = QColor(self._current_accent)
        colour = QColorDialog.getColor(
            initial, self, "Choose Accent Colour",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if colour.isValid():
            self._apply_accent(colour.name(), live=True)

    def _on_reset_accent(self) -> None:
        """Reset accent colour to the default baby blue."""
        self._apply_accent(_DEFAULT_ACCENT, live=True)

    def _get_system_java(self):  # used by window via callback
        return self.java_edit.text().strip() or None

    # -- java helpers -----------------------------------------------------------
    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Java executable", "",
            "Executables (*.exe *.jar);;All files (*)" if os.name == "nt"
            else "All files (*)",
        )
        if path:
            self.java_edit.setText(path)

    def _on_detect_java(self) -> None:
        """Open the Java detection dialog (PrismLauncher-style) and auto-fill the path."""
        from ..java_detect_dialog import JavaDetectDialog
        # Parent to the top-level window, not this page: the page lives inside
        # the launcher's QStackedWidget, and a modal dialog parented to it makes
        # Qt warn "StackedWidgetClassWindow must be a top level window" (the
        # stacked widget owns a native window under the mica/acrylic backdrop).
        dlg = JavaDetectDialog(self.window())
        if dlg.exec() and dlg.selected_path:
            self.java_edit.setText(dlg.selected_path)
            self.java_result_label.setText(
                f"Selected: {dlg.selected_path}"
            )
            self._toast("success", "Java detected",
                        "Path auto-filled. Click Save to persist.", 5000)

    def _on_test_java(self) -> None:
        path = self.java_edit.text().strip()
        if not path:
            import shutil

            path = shutil.which("java") or ""
        ok, detail = validate_java_executable(path) if path else (False, "No path set")
        self.java_result_label.setText(
            ("OK:  " if ok else "FAILED:  ") + detail
        )
        self._log(f"Java test {'passed' if ok else 'failed'}: {detail}")


    def _on_get_cf_key(self) -> None:
        """Open the official CurseForge API-key application article."""
        try:
            webbrowser.open(CURSEFORGE_API_KEY_INFO_URL)
            self._log("Opened CurseForge API-key info in the browser.")
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"Could not open CurseForge info URL: {exc}", "WARNING")

    def _on_test_cf_key(self) -> None:
        """Live-test the entered CurseForge key against the real API."""
        key = self.cf_key_edit.text().strip()
        if not key:
            self.cf_result_label.setText("Enter a key to test it.")
            return
        self.cf_result_label.setText("Testing…")
        try:
            resp = requests.get(
                f"{CURSEFORGE_API}/mods/search",
                params={"gameId": 432, "searchFilter": "jei", "pageSize": 1},
                headers={"x-api-key": key},
                timeout=15,
            )
            if resp.status_code == 200:
                self.cf_result_label.setText("OK: key accepted by CurseForge.")
                self._toast("success", "CurseForge key", "Key is valid.", 5000)
            elif resp.status_code in (401, 403):
                self.cf_result_label.setText(
                    "FAILED: CurseForge rejected this key (401/403). Check it or apply for a new one."
                )
                self._toast("error", "CurseForge key", "CurseForge rejected this key.", 8000)
            else:
                self.cf_result_label.setText(
                    f"Unexpected response: HTTP {resp.status_code}. Try again."
                )
        except requests.RequestException as exc:
            self.cf_result_label.setText(f"Could not reach CurseForge: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            self.cf_result_label.setText(f"Error testing key: {exc}")


    def system_java(self) -> str | None:
        """Effective system java path (setting or PATH), or None."""
        candidate = self.java_edit.text().strip()
        if candidate and os.path.isfile(candidate):
            return candidate
        import shutil

        return shutil.which("java")


def find_java_hint() -> str:
    found = shutil.which("java")
    return f"Auto-detected on PATH: {found}" if found else "No java found on PATH"
