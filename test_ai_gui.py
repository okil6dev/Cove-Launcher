"""
Constructs the AI Installer widgets for real.

Run: python test_ai_gui.py

Unit tests on the pure-Python client cannot catch a wrong qfluentwidgets API
call -- a nonexistent method only fails when the widget is actually built --
so this file builds every piece of the UI and pokes its real methods.

This is what caught ComboBox.setEditable() not existing.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

APP = QApplication.instance() or QApplication([])

from launcher.ai_client import AiConfig, Endpoint  # noqa: E402
from launcher.pages.ai_settings_dialog import AiSettingsDialog  # noqa: E402
from launcher.pages.ai_mods_view import AiModsView, ModBubble  # noqa: E402


class Vault:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def read(self):
        return dict(self.data)

    def write(self, secrets):
        self.data = dict(secrets)


class FakeInst:
    def __init__(self, name="FabricPack", loader="fabric"):
        self.name = name
        self.loader_id = loader
        self.mc_version = "1.21.1"
        self.mods_dir = "mods"


_ALIVE: list = []   # Qt objects must stay referenced or their C++ halves die


def _host():
    """A parent widget that outlives the test.

    Qt children die with their parent in C++, so every host (and the thing
    built on it) is parked here; otherwise assertions hit "wrapped C/C++
    object has been deleted".
    """
    w = QWidget()
    w.resize(900, 600)
    _ALIVE.append(w)
    return w


def _configured(url="http://localhost:11434/v1", key="", models=("llama3.1",),
                model="llama3.1"):
    """A vault already holding one endpoint, as if the user added one."""
    ep = Endpoint(url=url, api_key=key, models=list(models))
    return {"ai_endpoint": url, "ai_api_key": key, "ai_model": model,
            "ai_endpoints": [{"url": url, "api_key": key, "label": "",
                              "models": list(models)}]}


def _view(inst, seeded=None):
    # A single Vault instance, not the class: the view calls get_vault() each
    # time it reads or writes, so a fresh instance per call would lose saves.
    vault = Vault(seeded or {})
    v = AiModsView(
        get_instance=lambda: inst, log=lambda *a: None,
        toast=lambda *a, **k: None, get_cf_key=lambda: "",
        get_vault=lambda: vault, parent=_host())
    _ALIVE.append(v)
    v._vault_for_test = vault
    return v


def _endpoint_free(inst):
    """A view with a saved endpoint ready (so gating is about the instance)."""
    return _view(inst, seeded=_configured())


def _dialog(cfg=None):
    dlg = AiSettingsDialog(cfg or AiConfig(), _host())
    _ALIVE.append(dlg)
    return dlg


def _add_dialog():
    from launcher.pages.ai_add_endpoint import AddEndpointDialog
    dlg = AddEndpointDialog(_host())
    _ALIVE.append(dlg)
    return dlg


# ------------------------------------------------------ add endpoint dialog --
def test_add_dialog_starts_empty_and_disables_add():
    """Nothing to add yet, so Add must not be clickable."""
    dlg = _add_dialog()
    assert dlg.url_edit.text() == ""
    assert dlg.manual.text() == ""
    assert dlg.yesButton.isEnabled() is False
    assert dlg.validate() is False


def test_add_dialog_is_landscape_sized():
    dlg = _add_dialog()
    assert dlg.widget.minimumWidth() >= 720
    assert dlg.widget.minimumWidth() > dlg.widget.minimumHeight()
    assert dlg.model_list.minimumHeight() >= 240


def test_add_dialog_typing_a_model_enables_add():
    dlg = _add_dialog()
    dlg.url_edit.setText("http://localhost:11434/v1")
    dlg.manual.setText("llama3.1")
    assert dlg.yesButton.isEnabled() is True
    assert dlg.validate() is True


def test_add_dialog_model_can_be_typed_without_fetching():
    """No /models support must not block a hand-typed model."""
    dlg = _add_dialog()
    dlg.url_edit.setText("http://my-server:9999/v1")
    dlg.manual.setText("some-private-model")
    assert dlg._selected_models() == ["some-private-model"]
    assert dlg.result_endpoint().models == ["some-private-model"]


def test_add_dialog_url_hint_reflects_local_or_online():
    dlg = _add_dialog()
    dlg.url_edit.setText("http://localhost:1234/v1")
    assert "chat/completions" in dlg.hint.text()
    assert "Local server" in dlg.hint.text()
    dlg.url_edit.setText("https://api.openai.com/v1")
    assert "Online endpoint" in dlg.hint.text()


def test_add_dialog_bad_url_is_reported_not_raised():
    dlg = _add_dialog()
    dlg.url_edit.setText("http://")
    dlg.manual.setText("m")
    assert dlg.validate() is False
    assert dlg.status.text()


def test_add_dialog_rejects_a_url_with_no_model():
    dlg = _add_dialog()
    dlg.url_edit.setText("http://localhost:11434/v1")
    assert dlg.validate() is False
    assert "model" in dlg.status.text().lower()


def test_add_dialog_fetched_models_populate_the_list():
    dlg = _add_dialog()
    dlg._on_fetch_done(True, ["llama3.1", "qwen2.5-coder:7b", "mistral"], "")
    assert dlg.model_list.count() == 3
    assert dlg.count_label.text() == "3 discovered"
    # Nothing ticked yet -> Add stays disabled
    assert dlg.yesButton.isEnabled() is False
    dlg.model_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert dlg.result_endpoint().models == []


def test_add_dialog_only_saves_ticked_models():
    """Fetching dumps 3 models; only the ticked ones may be saved."""
    dlg = _add_dialog()
    dlg.url_edit.setText("http://localhost:11434/v1")
    dlg._on_fetch_done(True, ["llama3.1", "qwen2.5-coder:7b", "mistral"], "")
    dlg.model_list.item(1).setCheckState(Qt.CheckState.Checked)     # only qwen2.5-coder:7b
    assert dlg._selected_models() == ["qwen2.5-coder:7b"]
    assert dlg.result_endpoint().models == ["qwen2.5-coder:7b"]
    assert dlg.validate() is True


def test_add_dialog_check_all_and_none():
    dlg = _add_dialog()
    dlg._on_fetch_done(True, ["a", "b", "c"], "")
    dlg._check_all()
    assert dlg._selected_models() == ["a", "b", "c"]
    dlg._check_none()
    assert dlg._selected_models() == []


def test_add_dialog_manual_adds_to_the_tick():
    dlg = _add_dialog()
    dlg.url_edit.setText("http://localhost:11434/v1")
    dlg._on_fetch_done(True, ["llama3.1"], "")
    dlg.model_list.item(0).setCheckState(Qt.CheckState.Checked)
    dlg.manual.setText("phi4-extra")
    assert dlg._selected_models() == ["llama3.1", "phi4-extra"]
    assert dlg.result_endpoint().models == ["llama3.1", "phi4-extra"]


def test_add_dialog_search_filters_the_model_list():
    dlg = _add_dialog()
    dlg._on_fetch_done(True, ["llama3.1", "qwen2.5-coder:7b", "mistral"], "")
    dlg.search.setText("qwen")
    visible = [dlg.model_list.item(i).text() for i in range(3)
               if not dlg.model_list.item(i).isHidden()]
    assert visible == ["qwen2.5-coder:7b"]


def test_add_dialog_search_clearing_restores_every_model():
    dlg = _add_dialog()
    dlg._on_fetch_done(True, ["a", "b", "c"], "")
    dlg.search.setText("a")
    dlg.search.setText("")
    hidden = [dlg.model_list.item(i).isHidden() for i in range(3)]
    assert not any(hidden)


def test_add_dialog_check_all_respects_the_search_filter():
    """All must only tick what is visible, so a filtered search can't be
    overrun by clicking All on the full set."""
    dlg = _add_dialog()
    dlg._on_fetch_done(True, ["llama3.1", "qwen2.5-coder:7b", "mistral"], "")
    dlg.search.setText("w")                # just qwen2.5-coder:7b visible
    dlg._check_all()
    assert dlg._selected_models() == ["qwen2.5-coder:7b"]


def test_add_dialog_clicking_a_model_uses_it():
    dlg = _add_dialog()
    dlg._on_fetch_done(True, ["llama3.1", "qwen2.5-coder:7b"], "")
    dlg.model_list.setCurrentRow(1)
    dlg.model_list.item(1).setCheckState(Qt.CheckState.Checked)
    assert dlg._selected_models() == ["qwen2.5-coder:7b"]


def test_add_dialog_failed_fetch_still_allows_manual_entry():
    dlg = _add_dialog()
    dlg.url_edit.setText("http://localhost:9/v1")
    dlg._on_fetch_done(False, None, "Cannot reach http://localhost:9/v1.")
    assert "type a model name" in dlg.status.text()
    dlg.manual.setText("qwen2.5-coder:7b")
    assert dlg.validate() is True


def test_add_dialog_keeps_the_api_key_out_of_sight():
    dlg = _add_dialog()
    dlg.key_edit.setText("sk-secret")
    assert dlg.key_edit.echoMode().name == "Password"
    assert dlg.result_endpoint().api_key == "sk-secret"


# ------------------------------------------------------- settings dialog ---
def test_settings_dialog_builds_with_no_endpoints():
    dlg = _dialog()
    assert dlg.yesButton.text() == "Save"
    assert dlg.endpoint_list.count() == 0
    assert dlg.validate() is True        # saving nothing is legal


def test_settings_dialog_is_landscape_sized():
    dlg = _dialog()
    assert dlg.widget.minimumWidth() >= 720
    assert dlg.widget.minimumWidth() > dlg.widget.minimumHeight()
    assert dlg.model_list.minimumHeight() >= 240


def test_settings_dialog_lists_saved_endpoints():
    cfg = AiConfig(endpoints=[Endpoint(url="http://a/v1",
                                                 label="Alpha"),
                              Endpoint(url="http://b/v1")])
    dlg = _dialog(cfg)
    assert dlg.endpoint_list.count() == 2
    assert "Alpha" in dlg.endpoint_list.item(0).text()


def test_settings_dialog_marks_the_active_endpoint():
    cfg = AiConfig(endpoint="http://b/v1",
                   endpoints=[Endpoint(url="http://a/v1"),
                              Endpoint(url="http://b/v1")])
    dlg = _dialog(cfg)
    assert dlg.endpoint_list.item(1).text().startswith(">")


def test_settings_dialog_key_field_is_masked_and_editable():
    cfg = AiConfig(endpoints=[Endpoint(url="http://a/v1",
                                                 api_key="old-key")])
    dlg = _dialog(cfg)
    assert dlg.key_edit.echoMode().name == "Password"
    assert dlg.key_edit.text() == "old-key"
    dlg.key_edit.setText("new-key")
    assert dlg.result_config().endpoints[0].api_key == "new-key"


def test_settings_dialog_can_rename_an_endpoint():
    dlg = _dialog(AiConfig(endpoints=[Endpoint(url="http://a/v1")]))
    dlg.label_edit.setText("My local LLM")
    assert dlg.result_config().endpoints[0].label == "My local LLM"


def test_settings_dialog_forget_removes_an_endpoint():
    dlg = _dialog(AiConfig(endpoints=[
        Endpoint(url="http://a/v1"),
        Endpoint(url="http://b/v1")]))
    dlg.endpoint_list.setCurrentRow(0)
    dlg._on_forget()
    assert dlg.endpoint_list.count() == 1
    assert "http://b/v1" in dlg.result_config().endpoints[0].url


def test_settings_dialog_deleting_active_endpoint_repairs_config():
    cfg = AiConfig(
        endpoint="http://a/v1",
        api_key="key-a",
        model="m-a",
        endpoints=[
            Endpoint(url="http://a/v1", api_key="key-a", models=["m-a"]),
            Endpoint(url="http://b/v1", api_key="key-b", models=["m-b"]),
        ],
    )
    dlg = _dialog(cfg)
    dlg.endpoint_list.setCurrentRow(0)
    dlg._on_forget()
    out = dlg.result_config()
    assert out.endpoint == "http://b/v1"
    assert out.api_key == "key-b"
    assert out.model == "m-b"


def test_settings_dialog_forget_the_last_one_is_safe():
    dlg = _dialog(AiConfig(endpoints=[Endpoint(url="http://a/v1")]))
    dlg._on_forget()
    assert dlg.endpoint_list.count() == 0
    assert dlg.result_config().endpoints == []


def test_settings_dialog_cancel_does_not_mutate_the_original():
    cfg = AiConfig(endpoints=[Endpoint(url="http://a/v1")])
    dlg = _dialog(cfg)
    dlg._on_forget()
    assert len(cfg.endpoints) == 1        # the source config is untouched


def test_settings_dialog_shows_models_of_the_selected_connection():
    dlg = _dialog(AiConfig(endpoints=[
        Endpoint(url="http://a/v1", models=["llama3.1", "qwen2.5"]),
        Endpoint(url="http://b/v1", models=["mistral"])]))
    dlg.endpoint_list.setCurrentRow(0)
    assert dlg.model_list.count() == 2
    dlg.endpoint_list.setCurrentRow(1)
    assert dlg.model_list.count() == 1
    assert dlg.model_list.item(0).text() == "mistral"


def test_settings_dialog_model_search_filters():
    dlg = _dialog(AiConfig(endpoints=[
        Endpoint(url="http://a/v1", models=["llama3.1", "qwen2.5", "mistral"])]))
    dlg.model_search.setText("qwen")
    visible = [dlg.model_list.item(i).text() for i in range(3)
               if not dlg.model_list.item(i).isHidden()]
    assert visible == ["qwen2.5"]


def test_settings_dialog_deletes_a_single_model():
    dlg = _dialog(AiConfig(endpoints=[
        Endpoint(url="http://a/v1", models=["llama3.1", "qwen2.5"])]))
    dlg.endpoint_list.setCurrentRow(0)
    dlg.model_list.setCurrentRow(1)
    dlg._on_delete_model()
    assert dlg.result_config().endpoints[0].models == ["llama3.1"]
    assert "Removed" in dlg.status.text()


def test_settings_dialog_delete_model_needs_a_selection():
    dlg = _dialog(AiConfig(endpoints=[
        Endpoint(url="http://a/v1", models=["llama3.1"])]))
    assert dlg.delete_model_button.isEnabled() is False
    dlg.model_list.setCurrentRow(0)
    assert dlg.delete_model_button.isEnabled() is True
    dlg.model_list.clearSelection()
    dlg._on_delete_model()
    assert dlg.result_config().endpoints[0].models == ["llama3.1"]


def test_settings_dialog_deleting_a_model_leaves_other_connections_alone():
    dlg = _dialog(AiConfig(endpoints=[
        Endpoint(url="http://a/v1", models=["m1", "m2"]),
        Endpoint(url="http://b/v1", models=["m3"])]))
    dlg.endpoint_list.setCurrentRow(0)
    dlg.model_list.setCurrentRow(0)
    dlg._on_delete_model()
    out = dlg.result_config()
    assert out.endpoints[0].models == ["m2"]
    assert out.endpoints[1].models == ["m3"]


def test_settings_dialog_with_no_models_says_so():
    dlg = _dialog(AiConfig(endpoints=[Endpoint(url="http://a/v1")]))
    assert dlg.model_list.count() == 0
    assert "no models" in dlg.status.text()
    assert dlg.delete_model_button.isEnabled() is False


def test_settings_dialog_carries_sampling_through_unchanged():
    out = _dialog(AiConfig(temperature=0.7, max_results=9)).result_config()
    assert out.temperature == 0.7
    assert out.max_results == 9


def test_settings_dialog_has_no_sampling_controls():
    """Temp / mod-count are not user-facing here."""
    dlg = _dialog()
    assert not hasattr(dlg, "temp_spin")
    assert not hasattr(dlg, "max_spin")


def test_settings_dialog_empty_state_is_readable():
    dlg = _dialog()
    assert dlg.endpoint_list.count() == 0
    assert dlg.forget_button.isEnabled() is False
    assert dlg.delete_model_button.isEnabled() is False
    assert dlg.key_edit.isEnabled() is False


# ----V2----
# -------------------------------------------------------------- mod bubble --
def test_mod_bubble_builds_with_a_real_row():
    row = {"source": "modrinth", "project_id": "AANobbMI", "title": "Sodium",
           "icon_url": "", "ai_reason": "Fast"}
    b = ModBubble(row)
    assert b.name.text() == "Sodium"
    assert b.button.text() == "Install"


def test_mod_bubble_install_is_gated_and_locks_after_installing():
    b = ModBubble({"source": "modrinth", "project_id": "x", "title": "T"})
    b.set_installable(False)
    assert b.button.isEnabled() is False
    b.set_installable(True)
    assert b.button.isEnabled() is True
    b.mark_installed()
    assert b.button.text() == "Installed"
    assert b.button.isEnabled() is False


def test_mod_bubble_emits_the_row_on_install():
    row = {"source": "curseforge", "project_id": "238222", "title": "JEI"}
    b = ModBubble(row)
    got = []
    b.installRequested.connect(got.append)
    b.set_installable(True)
    b._on_install()
    assert got == [row]


# ------------------------------------------------------------------- view ---
def test_view_starts_with_no_endpoint_and_no_model():
    """Fresh install: nothing guessed, everything gated until you add one."""
    v = _view(FakeInst())
    assert v._config.endpoint == ""
    assert v._config.model == ""
    assert v.endpoint_combo.currentText() == "No endpoint yet"
    assert v.endpoint_combo.isEnabled() is False
    assert v.model_combo.isEnabled() is False
    assert v.input.isEnabled() is False
    assert "Add an AI endpoint" in v.input.placeholderText()


def test_view_has_an_add_button_and_a_gear():
    v = _view(FakeInst())
    assert v.add_button is not None
    assert v.gear_button is not None
    assert v.add_button.toolTip()


def test_long_endpoint_url_does_not_steal_width_from_the_model_box():
    """Regression: a raw URL made the endpoint ComboBox ~314px wide, which
    squeezed the model box down to its minimum and made its drop menu spill
    across the whole window."""
    long_url = "https://api.cline.bot/api/v1/chat/completions"
    v = _view(FakeInst(), seeded=_configured(url=long_url,
                                             models=("deepseek/deepseek-v4-flash",
                                                     "x/y", "z/w")))
    v.set_instance(FakeInst())
    assert v.endpoint_combo.width() <= 220
    assert v.model_combo.width() > v.endpoint_combo.width()


def test_endpoint_combo_label_is_shortened():
    """The raw URL must not be painted in full or the box balloons again."""
    long_url = "https://api.cline.bot/api/v1/chat/completions"
    v = _view(FakeInst(), seeded=_configured(url=long_url))
    shown = v.endpoint_combo.currentText()
    assert len(shown) <= 30
    assert shown.endswith("…")
    # The full URL is still available on hover.
    assert v.endpoint_combo.toolTip() == long_url


def test_short_endpoint_label_is_left_alone():
    v = _view(FakeInst(), seeded=_configured(url="http://localhost:11434/v1"))
    assert v.endpoint_combo.currentText() == "http://localhost:11434/v1"
    assert not v.endpoint_combo.currentText().endswith("…")


def test_endpoint_combo_menu_cannot_exceed_the_box():
    """RoundMenu inherits the combo width, so a capped box means a menu that
    stays inside the window."""
    v = _view(FakeInst(), seeded=_configured(
        url="https://api.cline.bot/api/v1/chat/completions"))
    assert v.endpoint_combo.maximumWidth() <= 220


def test_model_box_stays_wide_even_with_a_long_endpoint():
    v = _view(FakeInst(), seeded=_configured(
        url="https://api.cline.bot/api/v1/chat/completions",
        models=("deepseek/deepseek-v4-flash",)))
    v.set_instance(FakeInst())
    assert v.model_combo.width() >= 200
    assert v.model_combo.maximumWidth() <= 560


def test_six_mod_cards_fit_inside_a_narrow_panel():
    """Regression: cards were laid out in one fixed row, so the ones past the
    right edge were clipped and unreachable (no horizontal scrollbar)."""
    host = _host()
    host.resize(760, 600)
    host.show()
    v = _endpoint_free(FakeInst())
    v.set_instance(FakeInst())
    rows = [{"source": "modrinth", "project_id": f"p{i}", "title": f"Mod {i}",
             "ai_reason": "reason"} for i in range(6)]
    v._add_mod_bubbles(rows)
    for _ in range(6):
        APP.processEvents()
    strip = v._bubbles[0].parent()
    right = max(b.geometry().right() for b in v._bubbles)
    assert right <= strip.width(), (
        f"cards overflow: right={right} strip={strip.width()}")
    tops = {b.geometry().top() for b in v._bubbles}
    assert len(tops) > 1, "cards should have wrapped onto a second row"
    # And they must not all be stacked at the origin.
    assert len({b.geometry().x() for b in v._bubbles}) > 1, \
        "cards are stacked on top of each other"


def test_mod_cards_are_all_reachable():
    """Every card must sit inside the strip, never off-screen."""
    v = _endpoint_free(FakeInst())
    rows = [{"source": "modrinth", "project_id": f"p{i}", "title": f"M{i}"}
            for i in range(6)]
    v._add_mod_bubbles(rows)
    for b in v._bubbles:
        assert b.geometry().width() > 0
        assert b.isVisibleTo(v)


def test_view_lists_a_saved_endpoint_and_its_models():
    v = _view(FakeInst(), seeded=_configured(models=("llama3.1", "qwen2.5")))
    assert v.endpoint_combo.currentText() == "http://localhost:11434/v1"
    assert v.endpoint_combo.isEnabled() is True
    assert v.model_combo.count() == 2
    assert v.model_combo.currentText() == "llama3.1"
    assert v.model_combo.isEnabled() is True


def test_view_typing_a_model_commits_on_finish_not_immediately():
    v = _endpoint_free(FakeInst())
    v.model_combo.setText("my-local-model")
    assert v._config.model == "my-local-model"
    assert "ai_recent_models" not in v._get_vault().read()
    v._commit_model()
    assert "my-local-model" in v._get_vault().read()["ai_recent_models"]


def test_typing_in_the_model_box_does_not_pollute_the_dropdown():
    """The bug this guards: every keystroke rebuilt the list and appended the
    half-typed string, so the dropdown filled with junk like 'llama3.1q'."""
    v = _endpoint_free(FakeInst())
    before = v.model_combo.count()
    for ch in "abcde":
        v.model_combo.setText("llama3.1" + ch)
    assert v.model_combo.count() == before


def test_typing_does_not_write_to_the_vault_until_committed():
    v = _endpoint_free(FakeInst())
    v.model_combo.setText("llama3.1q")
    assert "ai_recent_models" not in v._get_vault().read()
    v._commit_model()
    assert v._get_vault().read()["ai_model"] == "llama3.1q"


def test_commit_saves_a_hand_typed_model():
    v = _endpoint_free(FakeInst())
    v.model_combo.setText("my-private-model")
    v._commit_model()
    assert v._get_vault().read()["ai_model"] == "my-private-model"


def test_picking_from_the_dropdown_saves_that_model():
    v = _view(FakeInst(), seeded=_configured(models=("llama3.1", "qwen2.5")))
    v.model_combo._onItemClicked(1)
    v._commit_model()
    assert v._config.model == "qwen2.5"
    assert v._get_vault().read()["ai_model"] == "qwen2.5"


def test_a_hand_typed_model_stays_selectable_afterwards():
    """EditableComboBox adds typed text as an item; the reload must keep it."""
    v = _endpoint_free(FakeInst())
    v.model_combo.setText("my-private-model")
    v.model_combo._onReturnPressed()
    v._commit_model()
    assert v.model_combo.findText("my-private-model") >= 0


def test_clearing_the_box_falls_back_to_a_known_model():
    v = _endpoint_free(FakeInst())
    v.model_combo.setText("")
    v._commit_model()
    assert v._config.model != ""


def test_switching_endpoint_reloads_the_model_list():
    """_persist(reload_models=True) must still fire on an endpoint switch."""
    seed = _configured(url="http://a/v1", models=("m-a",))
    seed["ai_endpoints"].append({"url": "http://b/v1", "api_key": "key-b",
                                 "label": "B", "models": ["m-b"]})
    v = _view(FakeInst(), seeded=seed)
    v.endpoint_combo.setCurrentIndex(1)
    assert v._config.model == "m-b"
    assert v.model_combo.count() == 1
    assert v.model_combo.text() == "m-b"


def test_view_switching_endpoint_swaps_key_and_models():
    seed = _configured(url="http://a/v1", key="key-a", models=("m-a",))
    seed["ai_endpoints"].append({"url": "http://b/v1", "api_key": "key-b",
                                 "label": "B", "models": ["m-b"]})
    v = _view(FakeInst(), seeded=seed)
    v._config.endpoint = "http://b/v1"
    v._config.api_key = "key-b"
    v._config.model = "m-b"
    v._reload_endpoints()
    v._reload_models()
    v.endpoint_combo.setCurrentIndex(1)
    assert v._config.endpoint == "http://b/v1"
    assert v._config.api_key == "key-b"
    assert v._config.model == "m-b"
    assert v.model_combo.currentText() == "m-b"


def test_view_no_instance_blocks_prompt_and_send():
    v = _endpoint_free(None)
    assert v.input.isEnabled() is False
    assert v.send_button.isEnabled() is False
    assert "Select an instance" in v.input.placeholderText()


def test_view_fabric_instance_enables_prompt():
    v = _endpoint_free(FakeInst())
    assert v.input.isEnabled() is True
    assert v.send_button.isEnabled() is True


def test_view_vanilla_instance_blocks_prompt():
    v = _endpoint_free(FakeInst(loader="vanilla"))
    assert v.input.isEnabled() is False
    assert "Fabric or NeoForge" in v.input.placeholderText()


def test_existing_bubbles_are_gated_by_instance_state():
    v = _endpoint_free(FakeInst())
    v._add_mod_bubbles([{"source": "modrinth", "project_id": "a", "title": "A"},
                        {"source": "modrinth", "project_id": "b", "title": "B"}])
    assert all(b.button.isEnabled() for b in v._bubbles)

    v.set_instance(None)
    assert not any(b.button.isEnabled() for b in v._bubbles)

    v.set_instance(FakeInst())
    assert all(b.button.isEnabled() for b in v._bubbles)


def test_install_still_works_without_an_endpoint():
    """Install needs an instance, not an AI connection."""
    v = _view(FakeInst())
    v._add_mod_bubbles([{"source": "modrinth", "project_id": "a", "title": "A"}])
    assert v._bubbles[0].button.isEnabled() is True


def test_view_adds_bubbles_and_tracks_history():
    v = _endpoint_free(FakeInst())
    v._add_bubble("hello there", mine=True)
    v._on_reply("hi back", [])
    assert v._history == [{"role": "assistant", "content": "hi back"}]


def test_view_send_without_instance_toasts_and_does_nothing():
    toasts = []
    v = _endpoint_free(None)
    v._toast = lambda sev, title, content="", *a, **k: toasts.append(title)
    v.input.setText("give me mods")
    v._on_send()
    assert toasts, "user got no feedback"
    assert v._history == [], "must not start a thread with no instance"
    assert v._thread is None


def test_view_send_without_endpoint_toasts_and_does_nothing():
    toasts = []
    v = _view(FakeInst())
    v._toast = lambda sev, title, content="", *a, **k: toasts.append(title)
    v.input.setText("give me mods")
    v._on_send()
    assert any("endpoint" in t.lower() for t in toasts)
    assert v._thread is None


def test_view_send_refused_on_vanilla():
    toasts = []
    v = _endpoint_free(FakeInst(loader="vanilla"))
    v._toast = lambda sev, title, content="", *a, **k: toasts.append(title)
    v.input.setText("give me mods")
    v._on_send()
    assert any("Vanilla" in t for t in toasts)
    assert v._thread is None


def test_mark_mod_installed_finds_the_right_bubble():
    v = _endpoint_free(FakeInst())
    v._add_mod_bubbles([{"source": "modrinth", "project_id": "abc",
                         "title": "A"}])
    v.mark_mod_installed("abc")
    assert v._bubbles[0].button.text() == "Installed"


def test_mark_mod_installed_ignores_unknown_ids():
    v = _endpoint_free(FakeInst())
    v._add_mod_bubbles([{"source": "modrinth", "project_id": "abc",
                         "title": "A"}])
    v.mark_mod_installed("does-not-exist")
    assert v._bubbles[0].button.text() == "Install"


def test_clear_chat_resets_history_and_bubbles():
    v = _endpoint_free(FakeInst())
    v._add_mod_bubbles([{"source": "modrinth", "project_id": "a", "title": "A"}])
    v._history.append({"role": "user", "content": "x"})
    v.clear_chat()
    assert v._history == []
    assert v._bubbles == []


def test_busy_state_disables_input():
    v = _endpoint_free(FakeInst())
    v.set_busy(True)
    assert v.input.isEnabled() is False
    assert v.send_button.isEnabled() is False
    v.set_busy(False)
    assert v.input.isEnabled() is True


# ------------------------------------------------------- strict type dropdown --
def test_type_combo_exists_and_defaults_to_mods():
    v = _endpoint_free(FakeInst())
    assert v.type_combo.count() == 2
    assert v.type_combo.itemText(0) == "Mods only"
    assert v.type_combo.itemText(1) == "Plugins only"
    assert v.project_type() == "mod"


def test_switching_to_plugins_re_gates_on_server():
    v = _endpoint_free(FakeInst())
    assert v.input.isEnabled() is True       # instance + endpoint -> chatable
    v.type_combo.setCurrentIndex(1)          # Plugins only
    assert v.project_type() == "plugin"
    # No server selected: plugins mode must block the prompt even though a
    # perfectly good instance is selected.
    assert v.input.isEnabled() is False
    assert "server" in v.input.placeholderText().lower()
    v.type_combo.setCurrentIndex(0)          # back to Mods only
    assert v.project_type() == "mod"
    assert v.input.isEnabled() is True


def test_plugin_mode_enabled_by_a_capable_server():
    v = _endpoint_free(None)                 # plugins need no instance

    class PaperServer:
        name = "My Server"
        supports_plugins = True
        mc_version = "1.21.1"
        server_type = "paper"

    v.set_server(PaperServer())
    v.type_combo.setCurrentIndex(1)
    assert v.input.isEnabled() is True


def test_plugin_mode_blocked_for_pluginless_server():
    v = _endpoint_free(FakeInst())

    class VanillaServer:
        name = "Vanilla Box"
        supports_plugins = False
        mc_version = "1.21.1"
        server_type = "vanilla"

    v.set_server(VanillaServer())
    v.type_combo.setCurrentIndex(1)
    assert v.input.isEnabled() is False
    assert "cannot run plugins" in v.input.placeholderText()


def test_mods_mode_accepts_dual_loader_server_without_instance():
    v = _endpoint_free(None)                   # no instance selected

    class MohistServer:
        name = "Hybrid"
        supports_mods = True
        supports_plugins = True
        mc_version = "1.20.1"
        server_type = "mohist"
        mods_dir = "mods"
        plugins_dir = "plugins"

    v.set_server(MohistServer())               # Mods only (default index 0)
    assert v.project_type() == "mod"
    assert v.input.isEnabled() is True
    assert "Hybrid" in v.input.placeholderText()
    kind, target = v._mod_target(None)
    assert kind == "server" and target.name == "Hybrid"


def test_mods_mode_prefers_instance_over_server():
    v = _endpoint_free(FakeInst())

    class MohistServer:
        name = "Hybrid"
        supports_mods = True
        mc_version = "1.20.1"
        server_type = "mohist"

    v.set_server(MohistServer())
    kind, target = v._mod_target(v._instance)
    assert kind == "instance" and target.name == "FabricPack"
    assert "FabricPack" in v.input.placeholderText()


def test_mods_mode_refuses_plugin_only_server():
    v = _endpoint_free(None)

    class PaperServer:
        name = "Paper Box"
        supports_mods = False
        supports_plugins = True
        mc_version = "1.21.1"
        server_type = "paper"

    v.set_server(PaperServer())
    assert v.input.isEnabled() is False
    kind, _target = v._mod_target(None)
    assert kind is None


def test_project_type_persisted_to_vault():
    v = _endpoint_free(FakeInst())
    v.type_combo.setCurrentIndex(1)
    assert v._vault_for_test.data.get("ai_project_type") == "plugin"
    v.type_combo.setCurrentIndex(0)
    assert v._vault_for_test.data.get("ai_project_type") == "mod"


def test_installed_files_follow_the_strict_mode():
    from launcher.servers import Server  # noqa: E402

    v = _endpoint_free(FakeInst())
    assert v._installed_files() == []        # FakeInst.mods_dir = "mods" (missing dir -> [])
    server = Server(name="Srv", server_type="paper", mc_version="1.21.1",
                    root=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "__no_such_root__"))
    v.set_server(server)
    v.type_combo.setCurrentIndex(1)          # plugins mode reads plugins/
    folder_plugins = server.plugins_dir
    assert folder_plugins.endswith("plugins")
    assert v._installed_files() == []        # folder does not exist -> []


def test_ai_thread_carries_project_type():
    from launcher.ai_client import AiConfig
    from launcher.workers_net import AiModsThread

    t = AiModsThread(config=AiConfig(), user_text="hi", history=[],
                     game_version="1.21.1", loader="paper",
                     instance_label="server", project_type="plugin")
    assert t._project_type == "plugin"
    assert t.kind == "plugins"
    t2 = AiModsThread(config=AiConfig(), user_text="hi", history=[],
                      game_version="1.21.1", loader="fabric",
                      instance_label="inst")
    assert t2._project_type == "mod"
    assert t2.kind == "mods"


# ----------------------------------------------------------- plugins rail tab --
class _FakeSig:
    def connect(self, *args, **kwargs):  # noqa: D102
        pass


class _FakeSearchThread:
    """Stands in for ModSearchThread so tests never hit the network."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = dict(kwargs)
        self.ready = _FakeSig()
        self.failed = _FakeSig()

    def start(self):  # noqa: D102
        pass

    def isRunning(self):  # noqa: D102
        return False


def _mods_page():
    from launcher.constants import SOURCE_PLUGINS  # noqa: E402
    from launcher.pages.mods_page import ModsPage  # noqa: E402

    page = ModsPage(
        # None instance on purpose: a real instance would kick off a live
        # Modrinth search during construction.
        get_instance=lambda: None,
        log=lambda *a, **k: None,
        toast=lambda *a, **k: None,
        get_cf_key=lambda: "",
        get_vault=lambda: None,
        get_server=lambda: None,
        parent=_host(),
    )
    _ALIVE.append(page)
    page._toasts = []
    page._toast = lambda *a, **k: page._toasts.append(a)
    return page, SOURCE_PLUGINS


def test_plugins_rail_tab_exists_under_ai():
    page, _ = _mods_page()
    assert page.rail_plugins is not None
    # The layout order is Modrinth, CurseForge, AI Installer, Plugins.
    rail = page.rail_plugins.parentWidget()
    order = [rail.layout().itemAt(i).widget()
             for i in range(rail.layout().count())
             if rail.layout().itemAt(i).widget() is not None]
    assert order.index(page.rail_ai) < order.index(page.rail_plugins)


def test_switching_to_plugins_tab_shows_browser_and_status():
    page, SOURCE_PLUGINS = _mods_page()
    page._switch_source(SOURCE_PLUGINS)
    assert page.rail_plugins.isChecked()
    assert not page.rail_ai.isChecked()
    assert page.view_stack.currentWidget() is not page.ai_view
    assert "server" in page.status_label.text().lower()


def test_plugin_search_refuses_pluginless_server():
    page, SOURCE_PLUGINS = _mods_page()

    class VanillaServer:
        name = "V"
        supports_plugins = False
        mc_version = "1.21.1"
        server_type = "vanilla"

    page._get_server = lambda: VanillaServer()
    page._source = SOURCE_PLUGINS
    page._search_plugins()                   # must refuse, not search
    assert page._search_thread is None
    assert any("plugins" in str(t).lower()
               for t in page._toasts)  # toast recorded


def test_plugin_search_never_puts_the_server_name_in_the_query():
    """The query comes from the search box only, and the status text must
    not claim the server name is what is being searched for."""
    import launcher.pages.mods_page as mp

    page, SOURCE_PLUGINS = _mods_page()

    class PaperServer:
        name = "My Annoying Server Name"
        supports_plugins = True
        mc_version = "1.21.1"
        server_type = "paper"

    page._get_server = lambda: PaperServer()
    page._source = SOURCE_PLUGINS
    page.search_edit.setText("worldedit")

    original = mp.ModSearchThread
    mp.ModSearchThread = _FakeSearchThread
    try:
        page._search_plugins()
        assert page.status_label.text() == "Searching plugins..."
        assert "Annoying" not in page.status_label.text()
        kw = _FakeSearchThread.last_kwargs
        assert kw["query"] == "worldedit"       # search box only
        assert "My Annoying Server Name" not in str(kw)
        assert kw["project_type"] == "plugin"
        assert kw["loader"] == "paper"
        assert kw["game_version"] == "1.21.1"

        # Infinite-scroll paging must stay on the server + plugin type too
        # (it used to fall back to the instance and the default "mod" type).
        page._offset = 20
        page._load_next_page()
        kw2 = _FakeSearchThread.last_kwargs
        assert kw2["project_type"] == "plugin"
        assert kw2["offset"] == 20
        assert kw2["game_version"] == "1.21.1"
        assert kw2["loader"] == "paper"
        assert page.status_label.text() == "Loading more plugins..."
        assert "My Annoying Server Name" not in str(kw2)
    finally:
        mp.ModSearchThread = original
        page._is_loading_more = False
        page.set_busy(False)


# ------------------------------------------------- "Install to:" target picker --
class _FakeServer:
    """A server double for the mod-target picker."""

    def __init__(self, name, server_type, supports_mods=True):
        self.name = name
        self.server_type = server_type
        self.mc_version = "1.21.1"
        self.mods_dir = f"{name}/mods"
        self.plugins_dir = f"{name}/plugins"
        self.supports_mods = supports_mods
        self.supports_plugins = server_type in ("paper", "mohist")


def _target_page(inst=None, servers=None, selected=None):
    """A ModsPage wired for the 'Install to:' tests (no network on build)."""
    import launcher.pages.mods_page as mp

    page = mp.ModsPage(
        get_instance=lambda: inst,
        log=lambda *a, **k: None,
        toast=lambda *a, **k: None,
        get_cf_key=lambda: "",
        get_vault=lambda: None,
        get_server=lambda: selected,
        get_servers=lambda: list(servers or []),
        parent=_host(),
    )
    _ALIVE.append(page)
    return page


def test_install_to_lists_instance_and_mod_capable_servers():
    inst = FakeInst()
    servers = [_FakeServer("Hybrid", "mohist"), _FakeServer("PaperSrv", "paper")]
    page = _target_page(inst=inst, servers=servers)
    page.on_instance_changed()
    labels = [page.target_combo.itemText(i)
              for i in range(page.target_combo.count())]
    # Instance + the one mod-capable server (Paper cannot run mods).
    assert any("Instance: FabricPack" in t for t in labels), labels
    assert any("Hybrid" in t for t in labels), labels
    assert not any("PaperSrv" in t for t in labels), labels


def test_install_to_defaults_to_instance_when_present():
    inst = FakeInst()
    page = _target_page(inst=inst, servers=[_FakeServer("Hybrid", "mohist")])
    page.on_instance_changed()
    kind, obj = page._browser_target()
    assert kind == "instance" and obj is inst


def test_install_to_can_select_a_server_like_an_instance():
    # No instance: a mod-capable server must be selectable as the target.
    server = _FakeServer("Hybrid", "mohist")
    page = _target_page(inst=None, servers=[server], selected=server)
    page.on_instance_changed()
    # Pick the server entry explicitly.
    for i in range(page.target_combo.count()):
        if page.target_combo.itemData(i) == ("server", "Hybrid"):
            page.target_combo.setCurrentIndex(i)
            break
    kind, obj = page._browser_target()
    assert kind == "server" and obj.name == "Hybrid", (kind, obj)
    dest_dir, gv, loader = page._browser_target_dims(kind, obj)
    assert dest_dir == "Hybrid/mods"
    assert loader == "forge"          # mohist -> forge mod loader


def test_install_to_server_target_drives_the_search():
    import launcher.pages.mods_page as mp

    server = _FakeServer("Hybrid", "mohist")
    page = _target_page(inst=None, servers=[server], selected=server)
    page.on_instance_changed()
    for i in range(page.target_combo.count()):
        if page.target_combo.itemData(i) == ("server", "Hybrid"):
            page.target_combo.setCurrentIndex(i)
            break
    original = mp.ModSearchThread
    mp.ModSearchThread = _FakeSearchThread
    try:
        page._on_search()
        kw = _FakeSearchThread.last_kwargs
        assert kw["game_version"] == "1.21.1"
        assert kw["loader"] == "forge"     # server mod loader, not the instance's
        assert page.status_label.text() == "Searching..."
    finally:
        mp.ModSearchThread = original
        page.set_busy(False)


def test_install_to_server_target_drives_install_destination():
    server = _FakeServer("Hybrid", "mohist")
    page = _target_page(inst=None, servers=[server], selected=server)
    page.on_instance_changed()
    for i in range(page.target_combo.count()):
        if page.target_combo.itemData(i) == ("server", "Hybrid"):
            page.target_combo.setCurrentIndex(i)
            break
    target = page._install_target({"project_id": "x", "source": "modrinth"})
    assert target is not None
    dest_dir, _gv, loader = target
    assert dest_dir == "Hybrid/mods"
    assert loader == "forge"


def test_install_to_empty_when_no_target():
    page = _target_page(inst=None, servers=[], selected=None)
    page.on_instance_changed()
    assert page.target_combo.count() == 0
    kind, obj = page._browser_target()
    assert kind is None and obj is None
    assert "No mod target" in page.status_label.text()


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


