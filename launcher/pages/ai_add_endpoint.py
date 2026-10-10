"""
"Add endpoint" dialog for the AI Installer.

The user starts with **no** endpoint at all. This dialog is how one gets added:

1. Type any base URL -- ``localhost:11434``, an online API, a proxy.
2. Optionally add an API key (local servers need none).
3. Press **Fetch models**: the launcher calls ``GET {base}/models`` off the GUI
   thread and lists whatever that server reports.
4. **Tick the models you actually want**. Un-ticked ones are *not* saved --
   fetching the list is just a menu, not a promise to import everything.
5. Type a name in the manual box to add a model the server forgot to list.

The saved connection keeps only the models you ticked (or typed), so the
dropdown next time shows exactly your selection, not the whole catalogue.
"""

from __future__ import annotations

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QListWidgetItem, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    LineEdit,
    ListWidget,
    PasswordLineEdit,
    PushButton,
    SearchLineEdit,
)
from .. import ai_client
from ..cove_dialog import CoveFormDialog


class _FetchThread(QThread):
    """Runs ``GET /models`` off the GUI thread."""

    done = pyqtSignal(bool, object, str)   # ok, list[str] | error text

    def __init__(self, config: ai_client.AiConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config

    def run(self) -> None:  # noqa: D102
        try:
            models = ai_client.fetch_models(self._config, timeout=15)
        except ai_client.AiError as exc:
            self.done.emit(False, None, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, None, f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(True, models, "")


class AddEndpointDialog(CoveFormDialog):
    """Collect a URL + key, then let the user pick which models to keep."""

    def __init__(self, parent=None) -> None:
        super().__init__("Add AI endpoint", parent, width=760, ok_text="Add", cancel_text="Cancel")
        self._thread: _FetchThread | None = None
        self.yesButton.setEnabled(False)

        body = QHBoxLayout()
        body.setSpacing(18)
        left = QVBoxLayout()
        left.setSpacing(10)
        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(left, 5)
        body.addLayout(right, 6)
        self.viewLayout.addLayout(body, 1)

        intro = BodyLabel(
            "Any OpenAI-compatible URL works. Local servers need no API key.")
        intro.setWordWrap(True)
        left.addWidget(intro)

        self.url_edit = LineEdit()
        self.url_edit.setPlaceholderText("http://localhost:11434/v1")
        left.addWidget(self.url_edit)

        self.hint = CaptionLabel("")
        self.hint.setObjectName("aiHint")
        self.hint.setWordWrap(True)
        left.addWidget(self.hint)

        self.key_edit = PasswordLineEdit()
        self.key_edit.setPlaceholderText("API key (optional)")
        left.addWidget(self.key_edit)

        fetch_row = QHBoxLayout()
        fetch_row.setSpacing(6)
        self.fetch_button = PushButton(FluentIcon.SYNC, "Fetch models")
        self.fetch_button.clicked.connect(self._on_fetch)
        fetch_row.addWidget(self.fetch_button)
        self.count_label = CaptionLabel("")
        self.count_label.setObjectName("aiHint")
        fetch_row.addWidget(self.count_label)
        fetch_row.addStretch(1)
        left.addLayout(fetch_row)

        self.manual = LineEdit()
        self.manual.setPlaceholderText("Or type a model name exactly")
        self.manual.textChanged.connect(self._update_add_state)
        left.addWidget(self.manual)

        self.status = CaptionLabel("")
        self.status.setObjectName("aiHint")
        self.status.setWordWrap(True)
        left.addWidget(self.status)
        left.addStretch(1)

        # Picker row so the user can grab "just the ones I want".
        pick_row = QHBoxLayout()
        pick_row.setSpacing(6)
        self.all_button = PushButton(FluentIcon.ACCEPT, "All")
        self.all_button.setToolTip("Tick every fetched model")
        self.all_button.clicked.connect(self._check_all)
        self.none_button = PushButton(FluentIcon.CLOSE, "None")
        self.none_button.setToolTip("Un-tick every fetched model")
        self.none_button.clicked.connect(self._check_none)
        pick_row.addWidget(self.all_button)
        pick_row.addWidget(self.none_button)
        pick_row.addStretch(1)
        right.addLayout(pick_row)

        # Fetched models, each with a checkbox. Scrolly so 200 models fit.
        self.search = SearchLineEdit()
        self.search.setPlaceholderText("Search collected models")
        self.search.textChanged.connect(self._filter_models)
        right.addWidget(self.search)

        self.model_list = ListWidget()
        self.model_list.setMinimumHeight(250)
        self.model_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.model_list.itemChanged.connect(self._update_add_state)
        right.addWidget(self.model_list, 1)

        self.url_edit.textChanged.connect(self._on_url_changed)
        self._on_url_changed(self.url_edit.text())

    # -- helpers --------------------------------------------------------------
    def _on_url_changed(self, text: str) -> None:
        try:
            url = ai_client.normalise_endpoint(text)
        except ai_client.AiError as exc:
            self.hint.setText(str(exc))
            return
        local = any(h in url.lower()
                    for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))
        note = "Local server - no API key needed." if local \
            else "Online endpoint."
        self.hint.setText(f"Models come from {url}   ({note})")

    def _probe_config(self) -> ai_client.AiConfig:
        """Throwaway config for the /models probe (no model needed)."""
        return ai_client.AiConfig(
            endpoint=self.url_edit.text().strip(),
            api_key=self.key_edit.text().strip(),
            model="",
        )

    def _on_fetch(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        try:
            ai_client.normalise_endpoint(self.url_edit.text())
        except ai_client.AiError as exc:
            self.status.setText(str(exc))
            return

        self.fetch_button.setEnabled(False)
        self.status.setText("Fetching models...")
        from ..threading_utils import retire
        retire(self._thread)
        self._thread = _FetchThread(self._probe_config(), self)
        self._thread.done.connect(self._on_fetch_done)
        self._thread.start()

    def _on_fetch_done(self, ok: bool, models, message: str) -> None:
        self.fetch_button.setEnabled(True)
        from ..threading_utils import retire
        retire(self._thread)
        self._thread = None
        if not ok:
            # Not fatal -- the manual box still works, so say so explicitly
            # rather than leaving the user with no way forward.
            self.status.setText(f"{message}  You can still type a model name.")
            return
        self.model_list.clear()
        for name in models or []:
            item = QListWidgetItem(name, self.model_list)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
        self.count_label.setText(f"{len(self.model_list)} discovered")
        self.status.setText("Tick the models you want to keep.")
        self._filter_models(self.search.text())
        self._update_add_state()

    def _check_all(self) -> None:
        for row in range(self.model_list.count()):
            item = self.model_list.item(row)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked)
        self._update_add_state()

    def _check_none(self) -> None:
        for row in range(self.model_list.count()):
            self.model_list.item(row).setCheckState(Qt.CheckState.Unchecked)
        self._update_add_state()

    def _filter_models(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for row in range(self.model_list.count()):
            item = self.model_list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _selected_models(self) -> list[str]:
        """The models the user actually ticked (or typed by hand)."""
        picked = [self.model_list.item(i).text() for i in
                  range(self.model_list.count())
                  if self.model_list.item(i).checkState() ==
                  Qt.CheckState.Checked]
        typed = self.manual.text().strip()
        out = picked
        if typed and typed not in out:
            out.append(typed)
        return out

    def _update_add_state(self, *_args) -> None:
        self.yesButton.setEnabled(bool(self._selected_models()))

    def validate(self) -> bool:  # noqa: N802 - qfw API name
        if not self.url_edit.text().strip():
            self.status.setText("Enter an endpoint URL.")
            return False
        try:
            ai_client.normalise_endpoint(self.url_edit.text())
        except ai_client.AiError as exc:
            self.status.setText(str(exc))
            return False
        if not self._selected_models():
            self.status.setText("Tick at least one model, or type a name.")
            return False
        return True

    def result_endpoint(self) -> ai_client.Endpoint:
        """The endpoint to persist, holding *only* the models the user picked."""
        return ai_client.Endpoint(
            url=self.url_edit.text().strip(),
            api_key=self.key_edit.text().strip(),
            models=self._selected_models(),
        )
