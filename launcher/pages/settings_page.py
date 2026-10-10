"""Settings page: Azure client id, CurseForge key, default Java, accent colour."""

from __future__ import annotations

import os
import shutil
import webbrowser

import requests

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QColorDialog, QFileDialog, QFrame, QHBoxLayout, QWidget
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
    setThemeColor,
)


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

# Where a CurseForge API key is obtained today. There is no self-serve dashboard:
# 3rd party developers apply to Overwolf via this support article (form -> review -> key by email).
CURSEFORGE_API_KEY_INFO_URL = "https://support.curseforge.com/en/support/solutions/articles/9000208346-about-the-curseforge-api-and-how-to-apply-for-a-key"
CURSEFORGE_API = "https://api.curseforge.com/v1"


class SettingsPage(QWidget):
    """Global launcher settings; secrets persist only in the vault."""

    def __init__(
        self,
        vault: Vault,
        log,
        toast,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._vault = vault
        self._log = log
        self._toast = toast

        # Scrollable root: same margins/spacing as the old QVBoxLayout, so it
        # looks identical when everything fits. The page carries a lot of
        # sections now (Window Sizes included); without the scroll area a short
        # window would clip the bottom rows instead of scrolling to them.
        root = scrollable_page_root(self, margins=(24, 24, 24, 16), spacing=12)

        root.addWidget(SubtitleLabel("Settings"))

        # Theme ------------------------------------------------------------------
        root.addWidget(BodyLabel(
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
        root.addLayout(theme_row)

        # Accent Colour ----------------------------------------------------------
        root.addWidget(BodyLabel("Accent colour — applied across the entire launcher UI"))
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

        root.addLayout(accent_row)

        self._current_accent: str = _DEFAULT_ACCENT
        self._apply_accent(_DEFAULT_ACCENT, live=False)


        # CurseForge ------------------------------------------------------------
        root.addWidget(BodyLabel(
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
        root.addLayout(cf_row)
        self.cf_result_label = BodyLabel("")
        root.addWidget(self.cf_result_label)

        # Ely.by ----------------------------------------------------------------
        root.addWidget(BodyLabel(
            "Ely.by client ID (optional) — only needed if you registered a "
            "custom OAuth app at account.ely.by"
        ))
        self.ely_client_id_edit = LineEdit()
        self.ely_client_id_edit.setPlaceholderText(
            "blank = built-in default"
        )
        root.addWidget(self.ely_client_id_edit)

        # Default Java -----------------------------------------------------------
        root.addWidget(BodyLabel(
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
        root.addLayout(java_row)

        self.java_result_label = BodyLabel("")
        root.addWidget(self.java_result_label)

        # Window Sizes -----------------------------------------------------------
        root.addWidget(SubtitleLabel("Window Sizes"))
        root.addWidget(BodyLabel(
            "Starting size of each window, in pixels. \"Apply sizes\" stores them "
            "and resizes the main window right away; the editors pick their size "
            "up the next time they open."
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
            root.addLayout(row)
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
        root.addLayout(size_buttons)

        # Save ----------------------------------------------------------------------
        self.save_button = PrimaryPushButton(FluentIcon.SAVE, "Save settings")
        root.addWidget(self.save_button)
        root.addStretch(1)

        self.java_browse.clicked.connect(self._on_browse)
        self.java_test.clicked.connect(self._on_test_java)
        self.java_detect_btn.clicked.connect(self._on_detect_java)
        self.save_button.clicked.connect(self._on_save)
        self.cf_test_btn.clicked.connect(self._on_test_cf_key)
        self.cf_getkey_btn.clicked.connect(self._on_get_cf_key)
        self.size_apply_btn.clicked.connect(self._on_apply_sizes)
        self.size_reset_btn.clicked.connect(self._on_reset_sizes)
        self.load()

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

    # -- theme helpers -----------------------------------------------------------
    def refresh_theme(self) -> None:
        """Re-apply theme-dependent styling (called on live theme change)."""
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
        self._toast("success", "Saved", "Settings stored securely.", 4000)

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

