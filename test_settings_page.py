"""Settings page rail: icon-only categories switching one section at a time.

Run: python -u test_settings_page.py

Guards the redesigned Settings page: a compact icon rail like the Mods page
one, one section per category, the painted "you are here" pill, and the rule
that every setting is still reachable and still saves from wherever the user
happens to be.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QFontMetrics  # noqa: E402
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

APP = QApplication.instance() or QApplication([])

from launcher import minecraft_options as mo  # noqa: E402
from launcher import window_sizes as ws  # noqa: E402
from launcher.pages.settings_page import SECTIONS, SettingsPage  # noqa: E402

_ALIVE: list = []
_LOGS: list = []
_TOASTS: list = []


class FakeVault:
    """dict-backed stand-in for launcher.security.Vault."""

    def __init__(self, data=None) -> None:
        self._data = dict(data or {})

    def read(self) -> dict:
        return dict(self._data)

    def write(self, data) -> None:
        self._data = dict(data)


class HostWindow(QWidget):
    """Top-level window standing in for LauncherWindow."""

    def __init__(self) -> None:
        super().__init__()
        self.applied: dict | None = None

    def apply_window_sizes(self, sizes) -> None:
        self.applied = dict(sizes)


class FakeInstance:
    """Just enough of launcher.instances.Instance for the Minecraft tab."""

    def __init__(self, name: str, root: str, mc_version: str = "1.20.1") -> None:
        self.name = name
        self.mc_version = mc_version
        self.dir = os.path.join(root, name)
        os.makedirs(self.dir, exist_ok=True)

    def options_text(self) -> str:
        path = os.path.join(self.dir, "options.txt")
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def seed_options(self, *lines: str) -> None:
        with open(os.path.join(self.dir, "options.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def _instances(*names: str) -> list:
    root = tempfile.mkdtemp(prefix="cove_settings_inst_")
    return [FakeInstance(name, root) for name in names]


def _page(vault=None, instances=None) -> SettingsPage:
    host = HostWindow()
    page = SettingsPage(
        vault=vault if vault is not None else FakeVault(),
        log=lambda *a, **k: _LOGS.append(a),
        toast=lambda *a, **k: _TOASTS.append(a),
        # None (the default) means no registry was supplied at all, which is
        # how the other suites build this page.
        get_instances=(lambda: list(instances)) if instances is not None else None,
        parent=host,
    )
    _ALIVE.extend([host, page])
    return page


def _pump(times: int = 20) -> None:
    for _ in range(times):
        APP.processEvents()


def _keys() -> list[str]:
    return [key for key, _h, _i, _d in SECTIONS]


def _rail_button_image(page: SettingsPage, key: str):
    """The rendered pixels of one rail button."""
    return page._rail_buttons[key].grab().toImage()


def _pixels(image) -> bytes:
    return bytes(image.constBits().asstring(image.sizeInBytes()))


# ------------------------------------------------------------------ the rail --
def test_the_rail_has_one_icon_button_per_section_in_order():
    page = _page()
    rail = page._rail_buttons["appearance"].parentWidget()
    assert list(page._rail_buttons) == _keys()

    layout = rail.layout()
    placed = [layout.itemAt(i).widget() for i in range(layout.count())
              if layout.itemAt(i).widget() is not None]
    assert placed == [page._rail_buttons[key] for key in _keys()]


def test_the_rail_is_compact_and_icon_only():
    page = _page()
    rail = page._rail_buttons["appearance"].parentWidget()
    # Compact: a 44px column, not the Mods page's 120px text rail.
    assert rail.width() == 44
    assert rail.width() < 120
    for key, button in page._rail_buttons.items():
        assert button.text() == "", f"{key} rail button still carries text"
        # Icon-only means the icon IS the label: a missing one is a blank button.
        assert not button.icon().isNull(), f"{key} rail button has no icon"
        assert button.size().width() == 36 and button.size().height() == 36


def test_icon_only_buttons_still_name_their_section():
    """No visible label means the tooltip is the only affordance."""
    page = _page()
    for key, heading, _icon, _desc in SECTIONS:
        button = page._rail_buttons[key]
        assert button.toolTip() == heading
        assert button.accessibleName() == heading


def test_the_rail_is_laid_out_vertically_and_stays_narrow():
    page = _page()
    # Geometry only exists once the layout has run, so show the host window.
    page.window().show()
    _pump()
    rail = page._rail_buttons["appearance"].parentWidget()
    tops = [page._rail_buttons[key].mapTo(
        rail, page._rail_buttons[key].rect().topLeft()).y() for key in _keys()]
    assert tops == sorted(tops), "rail buttons should stack top to bottom"
    # A single-row icon strip would be a different (horizontal) design.
    assert len(set(tops)) == len(tops)


# -------------------------------------------------------------- the sections --
def test_exactly_one_section_is_active_and_lit():
    page = _page()
    assert page.current_section() == _keys()[0]
    for key in _keys():
        page.show_section(key)
        _pump()
        checked = [k for k, b in page._rail_buttons.items() if b.isChecked()]
        assert checked == [key], f"{key} left the rail lit as {checked}"
        assert page.section_stack.currentIndex() == page._section_index[key]


def test_clicking_a_rail_button_switches_section():
    page = _page()
    assert page.section_stack.currentWidget() is not None
    before = page.section_stack.currentWidget()

    page._rail_buttons["window"].click()
    _pump()

    assert page.current_section() == "window"
    assert page.section_stack.currentWidget() is not before


def test_clicking_the_active_button_keeps_it_lit():
    """A toggle that unchecks itself would leave the rail with nothing lit."""
    page = _page()
    page.show_section("java")
    page._rail_buttons["java"].click()
    _pump()
    assert page._rail_buttons["java"].isChecked()
    assert page.current_section() == "java"


def test_an_unknown_section_is_ignored():
    page = _page()
    page.show_section("java")
    page.show_section("no-such-section")   # must not crash or blank the stack
    assert page.current_section() == "java"
    assert page.section_stack.currentWidget() is not None


def test_every_setting_is_reachable_from_its_own_section():
    """Each control must live inside the section that claims it."""
    page = _page()
    expected = {
        "appearance": [page.theme_combo, page._pick_btn, page._reset_btn],
        "keys": [page.cf_key_edit, page.cf_test_btn, page.ely_client_id_edit],
        "java": [page.java_edit, page.java_browse, page.java_detect_btn],
        "window": [page.size_apply_btn, page.size_reset_btn,
                   page._size_spins["main"][0]],
    }
    for key, widgets in expected.items():
        page.show_section(key)
        _pump()
        shown = page.section_stack.currentWidget()
        for widget in widgets:
            assert shown.isAncestorOf(widget), f"{key} does not contain {widget}"
    # ...and only there: the Java field is not on the Appearance section.
    page.show_section("appearance")
    _pump()
    assert not page.section_stack.currentWidget().isAncestorOf(page.java_edit)


def test_section_headings_are_on_the_page():
    from qfluentwidgets import SubtitleLabel

    page = _page()
    titles = [w.text() for w in page.findChildren(SubtitleLabel)]
    assert "Settings" in titles
    for _key, heading, _icon, _desc in SECTIONS:
        assert heading in titles


def test_the_save_button_is_outside_the_stack():
    """Save stores every section, so it cannot live inside one of them."""
    page = _page()
    for index in range(page.section_stack.count()):
        assert not page.section_stack.widget(index).isAncestorOf(page.save_button)


# --------------------------------------------------- the active-section pill --
def _channel_means(image) -> dict:
    totals = {"r": 0, "g": 0, "b": 0}
    for j in range(image.height()):
        for i in range(image.width()):
            colour = image.pixelColor(i, j)
            totals["r"] += colour.red()
            totals["g"] += colour.green()
            totals["b"] += colour.blue()
    n = max(image.width() * image.height(), 1)
    return {channel: value / n for channel, value in totals.items()}


def test_the_active_section_paints_a_pill():
    """The indicator is painted onto the button, not left to a stylesheet.

    Checked with a known colour, so no native/theme state can fake it: an
    active button has to be dominated by the pill colour. A ``:checked``
    background rule instead of the paint would leave the button its normal
    colour here (that rule never renders under the Mica/Acrylic backdrops).
    """
    import launcher.pages.settings_page as sp

    page = _page()
    page.window().show()
    _pump()

    original = sp._rail_pill_colour
    try:
        sp._rail_pill_colour = lambda: QColor(255, 0, 0)
        page.show_section("appearance")          # ...so 'keys' is idle
        _pump()
        idle = _channel_means(_rail_button_image(page, "keys"))

        page.show_section("keys")                # the same button, now active
        _pump()
        active = _channel_means(_rail_button_image(page, "keys"))
    finally:
        sp._rail_pill_colour = original

    # Redness: how far the worn colour is from neutral. Comparing one button
    # with itself leaves the icon out of the equation entirely.
    def redness(means):
        return means["r"] - max(means["g"], means["b"])

    assert redness(active) - redness(idle) > 40, (
        f"activating the button did not paint the pill: idle={idle} active={active}")


def test_the_pill_colour_follows_the_theme():
    """A dark surface and a light one need different pills, so it cannot be fixed."""
    import launcher.pages.settings_page as sp
    from launcher import theme

    original = theme.current_theme()
    try:
        theme.apply_theme("dark", None)
        dark = sp._rail_pill_colour()
        theme.apply_theme("light", None)
        light = sp._rail_pill_colour()
    finally:
        theme.apply_theme(original, None)

    as_tuple = lambda c: (c.red(), c.green(), c.blue(), c.alpha())
    assert as_tuple(dark) != as_tuple(light), (
        f"the pill colour ignores the theme: dark={as_tuple(dark)} light={as_tuple(light)}")


def test_the_pill_is_repainted_when_the_theme_changes():
    """The paint reads the colour function, so a palette change shows up."""
    import launcher.pages.settings_page as sp

    page = _page()
    page.window().show()
    page.show_section("appearance")
    _pump()

    original = sp._rail_pill_colour
    try:
        sp._rail_pill_colour = lambda: QColor("#ff0000")
        page.refresh_theme()
        _pump()
        red = _pixels(_rail_button_image(page, "appearance"))

        sp._rail_pill_colour = lambda: QColor("#00ff00")
        page.refresh_theme()
        _pump()
        green = _pixels(_rail_button_image(page, "appearance"))
    finally:
        sp._rail_pill_colour = original

    assert red != green, "the pill is cached instead of read per paint"


def test_the_page_stylesheet_stays_narrow():
    """A broad QWidget rule would blank the fields the sections are built from."""
    page = _page()
    page.refresh_theme()
    qss = page.styleSheet()
    assert "QToolButton#railIcon" in qss
    assert "QWidget {" not in qss
    # The swatch border is theme-dependent, so applying it must not raise.
    assert page._swatch.styleSheet()


# ------------------------------------------------------- the Minecraft tab --
def test_the_minecraft_tab_owns_its_controls():
    from qfluentwidgets import SubtitleLabel

    page = _page()
    assert "minecraft" in _keys()
    titles = [w.text() for w in page.findChildren(SubtitleLabel)]
    assert "Minecraft" in titles

    page.show_section("minecraft")
    _pump()
    shown = page.section_stack.currentWidget()
    assert shown.isAncestorOf(page.global_apply_btn)
    assert shown.isAncestorOf(page.global_status_label)
    # One switch per global option the launcher knows about.
    assert set(page._global_switches) == set(mo.DEFAULTS)
    for switch in page._global_switches.values():
        assert shown.isAncestorOf(switch)
    # A number option also carries a box, in the same section.
    assert set(page._global_spins) == {"gui_scale"}
    assert shown.isAncestorOf(page._global_spins["gui_scale"])


def test_the_switches_show_what_was_stored():
    seeded = _page(FakeVault({mo.VAULT_KEY: {"disable_music": True}}))
    assert seeded._global_switches["disable_music"].isChecked()

    fresh = _page()
    assert not fresh._global_switches["disable_music"].isChecked()


def test_applying_writes_every_instance():
    instances = _instances("Alpha", "Beta")
    vault = FakeVault()
    page = _page(vault, instances=instances)
    page._global_switches["disable_music"].setChecked(True)

    page.global_apply_btn.click()

    for instance in instances:
        assert "soundCategory_music:0.0" in instance.options_text(), instance.name
    stored = vault.read()[mo.VAULT_KEY]
    assert stored["disable_music"] is True
    assert stored["gui_scale"] is None, "a row left off stays unmanaged"
    assert any("applied" in str(entry).lower() for entry in _LOGS)
    assert "2" in page.global_status_label.text()


def test_saving_the_page_stores_and_applies_the_switch():
    instances = _instances("Gamma")
    vault = FakeVault()
    page = _page(vault, instances=instances)
    page._global_switches["disable_music"].setChecked(True)

    page._on_save()

    assert vault.read()[mo.VAULT_KEY]["disable_music"] is True
    assert "soundCategory_music:0.0" in instances[0].options_text()
    # The plain Save path still stores everything else it used to.
    assert ws.VAULT_KEY in vault.read()
    assert mo.load(vault)["disable_music"] is True


def test_turning_the_switch_off_again_gives_music_back():
    instance = _instances("Delta")[0]
    vault = FakeVault({mo.VAULT_KEY: {"disable_music": True}})
    page = _page(vault, instances=[instance])
    page._global_switches["disable_music"].setChecked(True)
    page.global_apply_btn.click()
    assert "soundCategory_music:0.0" in instance.options_text()

    page._global_switches["disable_music"].setChecked(False)
    page.global_apply_btn.click()

    assert "soundCategory_music:1.0" in instance.options_text()
    assert mo.load(vault)["disable_music"] is False
    # ...and nothing is enforced from then on.
    page.global_apply_btn.click()
    assert "soundCategory_music:1.0" in instance.options_text()


def test_an_option_that_stays_off_never_touches_an_instance():
    instance = _instances("Epsilon")[0]
    instance.seed_options("fov:0.5", "soundCategory_music:0.4")
    page = _page(FakeVault(), instances=[instance])

    page.global_apply_btn.click()

    assert instance.options_text().count("soundCategory_music:") == 1
    assert "soundCategory_music:0.4" in instance.options_text()


def test_the_tab_works_without_a_registry():
    page = _page()
    page._global_switches["disable_music"].setChecked(True)

    page.global_apply_btn.click()          # must not raise
    page._on_save()

    assert page._instances() == []
    assert "No instances" in page.global_status_label.text()


def test_an_empty_registry_says_so():
    page = _page(instances=[])
    assert "No instances" in page.global_status_label.text()


# ------------------------------------------------------ the GUI scale number box --
def test_the_gui_scale_box_is_roomier_than_the_number_it_holds():
    """The box is sized from the widest number it can hold, never clipped."""
    page = _page()
    spin = page._global_spins["gui_scale"]
    opt = mo.option("gui_scale")

    assert spin.minimum() == opt.minimum
    assert spin.maximum() == opt.maximum
    assert spin.value() == opt.initial
    widest = QFontMetrics(spin.font()).horizontalAdvance(str(opt.maximum))
    assert spin.minimumWidth() >= widest + 40, (spin.minimumWidth(), widest)
    # ...and never squeezed below what the style itself asks for.
    assert spin.minimumWidth() >= spin.sizeHint().width()


def test_the_laid_out_box_leaves_the_number_room():
    """Same claim, measured on the widget as it is actually laid out."""
    page = _page()
    host = page.parentWidget()
    box = QVBoxLayout(host)
    box.addWidget(page)
    host.resize(900, 660)
    host.show()
    page.show_section("minecraft")
    _pump()
    try:
        spin = page._global_spins["gui_scale"]
        spin.setValue(spin.maximum())
        _pump()
        widest = QFontMetrics(spin.font()).horizontalAdvance(str(spin.maximum()))
        assert spin.width() >= widest + 40, (spin.width(), widest)
        # The field the number is painted into is wider than the number: the
        # spinner buttons sit on top of the right-hand end.
        assert spin.lineEdit().width() >= widest, (spin.lineEdit().width(), widest)
    finally:
        page.hide()
        host.hide()


def test_the_gui_scale_box_needs_its_switch():
    page = _page()
    switch = page._global_switches["gui_scale"]
    spin = page._global_spins["gui_scale"]

    assert not switch.isChecked()
    assert not spin.isEnabled(), "an off row must not look editable"

    switch.setChecked(True)
    assert spin.isEnabled(), "the switch's own signal enables the box"

    switch.setChecked(False)
    assert not spin.isEnabled()


def test_a_stored_gui_scale_fills_the_box():
    page = _page(FakeVault({mo.VAULT_KEY: {"gui_scale": 4}}))
    assert page._global_switches["gui_scale"].isChecked()
    assert page._global_spins["gui_scale"].value() == 4
    assert page._global_spins["gui_scale"].isEnabled()

    # A value outside the range is clamped on the way in, not shown as-is.
    clamped = _page(FakeVault({mo.VAULT_KEY: {"gui_scale": 99}}))
    assert clamped._global_spins["gui_scale"].value() == mo.GUI_SCALE_MAX

    # An untouched row stays off and at its starting value.
    fresh = _page()
    assert not fresh._global_switches["gui_scale"].isChecked()
    assert fresh._global_spins["gui_scale"].value() == mo.option("gui_scale").initial


def test_the_box_reports_the_number_it_shows():
    page = _page()
    spin = page._global_spins["gui_scale"]
    spin.setValue(3)

    assert page._global_choices()["gui_scale"] is None, "off means unmanaged"

    page._global_switches["gui_scale"].setChecked(True)
    assert page._global_choices()["gui_scale"] == 3
    spin.setValue(5)
    assert page._global_choices()["gui_scale"] == 5, "the box is read live"


def test_applying_a_gui_scale_pins_every_instance():
    instances = _instances("Alpha", "Beta")
    vault = FakeVault()
    page = _page(vault, instances=instances)
    page._global_switches["gui_scale"].setChecked(True)
    page._global_spins["gui_scale"].setValue(3)

    page.global_apply_btn.click()

    for instance in instances:
        assert "guiScale:3" in instance.options_text(), instance.name
    assert vault.read()[mo.VAULT_KEY]["gui_scale"] == 3
    assert "2" in page.global_status_label.text()


def test_turning_the_gui_scale_off_gives_auto_back():
    instance = _instances("Delta")[0]
    instance.seed_options("fov:0.5")
    vault = FakeVault()
    page = _page(vault, instances=[instance])
    page._global_switches["gui_scale"].setChecked(True)
    page._global_spins["gui_scale"].setValue(4)
    page.global_apply_btn.click()

    text = instance.options_text()
    assert "guiScale:4" in text
    assert "fov:0.5" in text, "the instance's own options stay untouched"

    page._global_switches["gui_scale"].setChecked(False)
    page.global_apply_btn.click()

    assert "guiScale:0" in instance.options_text()
    assert mo.load(vault)["gui_scale"] is None
    # ...and with the switch off nothing is enforced from then on.
    page.global_apply_btn.click()
    assert "guiScale:0" in instance.options_text()


def test_both_rows_save_and_apply_together():
    instance = _instances("Both")[0]
    vault = FakeVault()
    page = _page(vault, instances=[instance])
    page._global_switches["disable_music"].setChecked(True)
    page._global_switches["gui_scale"].setChecked(True)
    page._global_spins["gui_scale"].setValue(2)

    page._on_save()

    text = instance.options_text()
    assert "soundCategory_music:0.0" in text
    assert "guiScale:2" in text
    assert mo.load(vault)["gui_scale"] == 2


# ------------------------------------------------------------------ behaviour --
def test_saving_from_one_section_persists_the_others():
    vault = FakeVault()
    page = _page(vault)
    page.show_section("appearance")          # deliberately not the edited section

    page.cf_key_edit.setText("cf-key-123")
    page.ely_client_id_edit.setText("ely-42")
    page.java_edit.setText("C:/jdk/bin/java.exe")
    page._size_spins["main"][0].setValue(1340)
    page._on_save()

    data = vault.read()
    assert data["curseforge_api_key"] == "cf-key-123"
    assert data["ely_client_id"] == "ely-42"
    assert data["system_java"] == "C:/jdk/bin/java.exe"
    assert data[ws.VAULT_KEY]["main"] == [1340, 820]
    assert data["theme"]


def test_section_switching_keeps_what_the_user_typed():
    page = _page()
    page.cf_key_edit.setText("keep-me")
    page.show_section("window")
    page.show_section("keys")
    assert page.cf_key_edit.text() == "keep-me"


def test_values_load_into_their_own_sections():
    vault = FakeVault({
        "curseforge_api_key": "stored-key",
        "ely_client_id": "stored-ely",
        "system_java": "C:/stored/java.exe",
        "accent_colour": "#123456",
    })
    page = _page(vault)
    assert page.cf_key_edit.text() == "stored-key"
    assert page.ely_client_id_edit.text() == "stored-ely"
    assert page.java_edit.text() == "C:/stored/java.exe"
    assert page._accent_label.text() == "#123456"


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
