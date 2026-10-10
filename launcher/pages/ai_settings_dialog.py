from __future__ import annotations

from PyQt6.QtCore import Qt
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
    SubtitleLabel,
)

from .. import ai_client
from ..cove_dialog import CoveFormDialog


class AiSettingsDialog(CoveFormDialog):
    """Manage saved endpoints and the models they offer.

    Deliberately just a viewer/editor for endpoints and their models:
    see what is saved, rename it, change its key, delete a model, delete a
    connection. Sampling knobs are not exposed here.
    """

    def __init__(self, config: ai_client.AiConfig, parent=None) -> None:
        super().__init__("AI endpoints", parent)
        self._config = config
        # Work on a copy so Cancel really cancels.
        self._endpoints = [ai_client.Endpoint(
            url=e.url, api_key=e.api_key, label=e.label,
            models=list(e.models)) for e in config.endpoints]
        self._api_keys: list[str] = [e.api_key for e in self._endpoints]

        self.widget.setMinimumSize(760, 430)
        self.viewLayout.setSpacing(10)
        self.yesButton.setText("Save")
        self.cancelButton.setText("Cancel")

        self.viewLayout.addWidget(SubtitleLabel("AI endpoints"))

        body = QHBoxLayout()
        body.setSpacing(18)
        left = QVBoxLayout()
        left.setSpacing(10)
        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(left, 5)
        body.addLayout(right, 6)
        self.viewLayout.addLayout(body, 1)

        # -- connections -------------------------------------------------------
        left.addWidget(BodyLabel("Connections"))
        self.endpoint_list = ListWidget()
        self.endpoint_list.setMinimumHeight(205)
        left.addWidget(self.endpoint_list, 1)

        self.label_edit = LineEdit()
        self.label_edit.setPlaceholderText("Name for the selected connection")
        left.addWidget(self.label_edit)

        self.key_edit = PasswordLineEdit()
        self.key_edit.setPlaceholderText("API key for the selected connection")
        left.addWidget(self.key_edit)

        forget_row = QHBoxLayout()
        forget_row.setSpacing(6)
        self.forget_button = PushButton(FluentIcon.DELETE, "Delete connection")
        self.forget_button.clicked.connect(self._on_forget)
        forget_row.addWidget(self.forget_button)
        forget_row.addStretch(1)
        left.addLayout(forget_row)

        # -- models of the selected connection ---------------------------------
        right.addWidget(BodyLabel("Models"))
        self.model_search = SearchLineEdit()
        self.model_search.setPlaceholderText("Search models")
        self.model_search.textChanged.connect(self._filter_models)
        right.addWidget(self.model_search)

        self.model_list = ListWidget()
        self.model_list.setMinimumHeight(250)
        self.model_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.model_list.itemSelectionChanged.connect(self._refresh_model_status)
        right.addWidget(self.model_list, 1)

        del_row = QHBoxLayout()
        del_row.setSpacing(6)
        self.delete_model_button = PushButton(FluentIcon.DELETE, "Delete model")
        self.delete_model_button.clicked.connect(self._on_delete_model)
        del_row.addWidget(self.delete_model_button)
        del_row.addStretch(1)
        right.addLayout(del_row)

        self.status = CaptionLabel("")
        self.status.setObjectName("aiHint")
        self.status.setWordWrap(True)
        right.addWidget(self.status)

        self.endpoint_list.currentItemChanged.connect(self._on_select)
        self.key_edit.editingFinished.connect(self._store_key)
        self.label_edit.editingFinished.connect(self._store_label)
        self.yesButton.clicked.connect(self.validate)
        self._refresh_list()

    # -- endpoint list --------------------------------------------------------
    def _refresh_list(self, select: int | None = None) -> None:
        # Rebuilding emits currentItemChanged, and _on_select saves whatever is
        # in the key box at that moment -- which would overwrite real keys with
        # a stale/blank value. Block signals and populate the fields afterwards.
        self.endpoint_list.blockSignals(True)
        self.endpoint_list.clear()
        active = self._current_endpoint_index()
        for idx, ep in enumerate(self._endpoints):
            marker = ">" if idx == active else "  "
            QListWidgetItem(f"{marker} {ep.name}", self.endpoint_list)
        if self._endpoints:
            row = 0 if select is None else max(0, min(select,
                                                     len(self._endpoints) - 1))
            self.endpoint_list.setCurrentRow(row)
        self.endpoint_list.blockSignals(False)
        self._load_selected()

    def _current_endpoint_index(self) -> int:
        for i, ep in enumerate(self._endpoints):
            if ep.url == self._config.endpoint:
                return i
        return -1

    def _on_select(self, _current=None, _previous=None) -> None:
        self._store_key()
        self._store_label()
        self._load_selected()

    def _load_selected(self) -> None:
        row = self.endpoint_list.currentRow()
        if 0 <= row < len(self._endpoints):
            while len(self._api_keys) < len(self._endpoints):
                self._api_keys.append("")
            self.key_edit.setText(self._api_keys[row])
            self.label_edit.setText(self._endpoints[row].label)
        else:
            self.key_edit.clear()
            self.label_edit.clear()
            self.key_edit.setEnabled(False)
            self.label_edit.setEnabled(False)
            self.forget_button.setEnabled(False)
            self.delete_model_button.setEnabled(False)
            return
        self.key_edit.setEnabled(True)
        self.label_edit.setEnabled(True)
        self.forget_button.setEnabled(True)
        self.delete_model_button.setEnabled(bool(self._endpoints[row].models))
        self._refresh_models()

    def _refresh_models(self) -> None:
        """Show the selected connection's models."""
        self.model_list.blockSignals(True)
        self.model_list.clear()
        row = self.endpoint_list.currentRow()
        if 0 <= row < len(self._endpoints):
            for name in self._endpoints[row].models:
                QListWidgetItem(name, self.model_list)
        self.model_list.blockSignals(False)
        self.model_search.setEnabled(bool(self.model_list.count()))
        self._filter_models(self.model_search.text())
        self._refresh_model_status()

    def _filter_models(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for row in range(self.model_list.count()):
            item = self.model_list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _refresh_model_status(self) -> None:
        items = self.model_list.selectedItems()
        self.delete_model_button.setEnabled(bool(items))
        if items:
            self.status.setText(f"'{items[0].text()}' selected.")
        else:
            count = self.model_list.count()
            self.status.setText(
                f"{count} model(s). Select one to delete." if count
                else "This connection has no models saved.")

    def _on_delete_model(self) -> None:
        """Remove a model from the selected connection's saved list."""
        erow = self.endpoint_list.currentRow()
        items = self.model_list.selectedItems()
        if not (0 <= erow < len(self._endpoints)) or not items:
            self.status.setText("Select a model to delete.")
            return
        name = items[0].text()
        ep = self._endpoints[erow]
        if name in ep.models:
            ep.models.remove(name)
        self._refresh_models()
        self.status.setText(f"Removed '{name}'. Save to apply.")

    def _store_key(self) -> None:
        row = self.endpoint_list.currentRow()
        if 0 <= row < len(self._endpoints):
            while len(self._api_keys) < len(self._endpoints):
                self._api_keys.append("")
            self._api_keys[row] = self.key_edit.text().strip()

    def _store_label(self) -> None:
        row = self.endpoint_list.currentRow()
        if 0 <= row < len(self._endpoints):
            self._endpoints[row].label = self.label_edit.text().strip()

    def _on_forget(self) -> None:
        row = self.endpoint_list.currentRow()
        if not (0 <= row < len(self._endpoints)):
            self.status.setText("Select a connection to delete.")
            return
        removed = self._endpoints.pop(row)
        if row < len(self._api_keys):
            self._api_keys.pop(row)
        self._refresh_list(select=min(row, len(self._endpoints) - 1))
        self.status.setText(f"Deleted {removed.name}. Save to apply.")

    # -- result ---------------------------------------------------------------
    def result_config(self) -> ai_client.AiConfig:
        """The edited configuration (callers persist it)."""
        self._store_key()
        self._store_label()
        while len(self._api_keys) < len(self._endpoints):
            self._api_keys.append("")
        for ep, key in zip(self._endpoints, self._api_keys):
            ep.api_key = key
        active = next(
            (e for e in self._endpoints if e.url == self._config.endpoint),
            self._endpoints[0] if self._endpoints else None,
        )
        endpoint = active.url if active else ""
        api_key = active.api_key if active else ""
        model = self._config.model
        if active is None:
            model = ""
        elif active.models and model not in active.models:
            model = active.models[0]
        return ai_client.AiConfig(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            # Sampling is not editable in this dialog; carry it through.
            temperature=self._config.temperature,
            max_results=self._config.max_results,
            recent_models=list(self._config.recent_models),
            endpoints=self._endpoints,
        )

    def validate(self) -> bool:  # noqa: N802 - qfw API name
        self._store_key()
        self._store_label()
        return True
