"""Modal dialogs for Cove Launcher."""

from __future__ import annotations

from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    LineEdit,
    MessageBoxBase,
    SubtitleLabel,
)

from .constants import (
    DEFAULT_RAM_GB,
    LATEST_LOADER_BUILD_SENTINEL,
    MIN_RAM_GB,
    MODLOADERS,
)
from .instances import validate_name


class NewInstanceDialog(MessageBoxBase):
    """Create-instance wizard: name, version, loader, loader build, RAM."""

    def __init__(self, versions: list[str], max_ram_gb: int, parent) -> None:
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("New instance"))

        self.viewLayout.addWidget(SubtitleLabel("Name"))
        self.name_edit = LineEdit()
        self.name_edit.setPlaceholderText("My Instance")
        self.name_edit.setMaxLength(64)
        self.viewLayout.addWidget(self.name_edit)

        self.viewLayout.addWidget(SubtitleLabel("Game version"))
        self.version_combo = ComboBox()
        for version_id in versions:
            self.version_combo.addItem(version_id, userData=version_id)
        self.viewLayout.addWidget(self.version_combo)

        self.viewLayout.addWidget(SubtitleLabel("Modloader"))
        self.loader_combo = ComboBox()
        for loader_id, label in MODLOADERS:
            self.loader_combo.addItem(label, userData=loader_id)
        self.viewLayout.addWidget(self.loader_combo)

        self.viewLayout.addWidget(SubtitleLabel("Loader build"))
        self.build_combo = ComboBox()
        self._set_build_placeholder("Latest build (auto)")
        self.viewLayout.addWidget(self.build_combo)

        self.error_label = BodyLabel("")
        self.error_label.setVisible(False)
        self.viewLayout.addWidget(self.error_label)

        self.yesButton.setText("Create")
        self.cancelButton.setText("Cancel")

        self._versions = versions
        self._max_ram_gb = max_ram_gb
        # RAM selection via a simple combo (slider in a scroll dialog is fiddly).
        self.ram_combo = ComboBox()
        for gb in range(MIN_RAM_GB, max(MIN_RAM_GB + 1, max_ram_gb) + 1):
            self.ram_combo.addItem(f"{gb} GB", userData=gb)
            if gb == min(DEFAULT_RAM_GB, max_ram_gb):
                self.ram_combo.setCurrentIndex(self.ram_combo.count() - 1)
        self.viewLayout.addWidget(SubtitleLabel("Allocated RAM"))
        self.viewLayout.addWidget(self.ram_combo)

        self._fetch_key: tuple[str, str] | None = None
        self.loader_combo.currentIndexChanged.connect(self._on_loader_changed)
        # A version change must also re-fetch loader builds; without this the
        # build list showed stale results for the previously chosen version.
        self.version_combo.currentIndexChanged.connect(self._on_loader_changed)
        # Populate the build combo for the initial selection (vanilla -> N/A).
        self._on_loader_changed()

    # -- helpers -------------------------------------------------------------
    def _current_fetch_key(self) -> tuple[str, str]:
        """(loader, mc version) the visible combo selection maps to."""
        loader_id = str(self.loader_combo.currentData() or "vanilla")
        mc_version = str(
            self.version_combo.currentData() or self.version_combo.currentText()
        )
        return loader_id, mc_version

    def result_values(self) -> dict:
        build = self.build_combo.currentData()
        return {
            "name": self.name_edit.text().strip(),
            "mc_version": str(self.version_combo.currentData()
                              or self.version_combo.currentText()),
            "loader_id": str(self.loader_combo.currentData() or "vanilla"),
            "loader_build": None
            if build in (None, LATEST_LOADER_BUILD_SENTINEL, "")
            else str(build),
            "ram_gb": int(self.ram_combo.currentData() or DEFAULT_RAM_GB),
        }

    def set_builds(self, supported: bool, builds: list, latest: str) -> None:
        # A slow response for an older (loader, version) selection must not
        # overwrite the list shown for the current one.
        if self._fetch_key is not None and self._current_fetch_key() != self._fetch_key:
            return
        loader_id = str(self.loader_combo.currentData() or "vanilla")
        mc_version = str(
            self.version_combo.currentData() or self.version_combo.currentText()
        )
        self.build_combo.blockSignals(True)
        self.build_combo.clear()
        if loader_id == "vanilla":
            self.build_combo.addItem("N/A", userData="")
            self.build_combo.setEnabled(False)
            self.build_combo.blockSignals(False)
            return
        if not supported or not builds:
            self.build_combo.addItem("Not available", userData="")
            self.build_combo.setEnabled(False)
            self.build_combo.blockSignals(False)
            return
        latest_label = f"Latest ({latest})" if latest else "Latest (auto)"
        self.build_combo.addItem(latest_label, userData=LATEST_LOADER_BUILD_SENTINEL)
        for build in builds:
            self.build_combo.addItem(str(build), userData=str(build))
        self.build_combo.setCurrentIndex(0)
        self.build_combo.setEnabled(True)
        self.build_combo.blockSignals(False)
        del mc_version  # informational only

    def _set_build_placeholder(self, text: str) -> None:
        self.build_combo.addItem(text, userData=LATEST_LOADER_BUILD_SENTINEL)
        self.build_combo.setEnabled(False)

    def _on_loader_changed(self) -> None:
        """Vanilla needs no build; loader builds are fetched by the window."""
        loader_id = str(self.loader_combo.currentData() or "vanilla")
        self.build_combo.blockSignals(True)
        self.build_combo.clear()
        if loader_id == "vanilla":
            self._fetch_key = None
            self.build_combo.addItem("N/A", userData="")
            self.build_combo.setEnabled(False)
        else:
            self.build_combo.addItem("Loading builds...", userData=LATEST_LOADER_BUILD_SENTINEL)
            self.build_combo.setEnabled(False)
            # Remember which selection this fetch is for so a stale response
            # (user changed version/loader meanwhile) can be discarded.
            self._fetch_key = self._current_fetch_key()
            # Ask the window to fetch builds for the current selection.
            if hasattr(self, "on_fetch_builds"):
                self.on_fetch_builds(*self._fetch_key)
        self.build_combo.blockSignals(False)

    def validate(self) -> bool:  # noqa: N802 - qfw API
        try:
            validate_name(self.name_edit.text())
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)
            return False
        return True


class ImportNameDialog(MessageBoxBase):
    """Ask for an instance name when importing a modpack."""

    def __init__(self, suggested_name: str, parent) -> None:
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("Import modpack"))
        self.viewLayout.addWidget(SubtitleLabel("Instance name"))
        self.name_edit = LineEdit()
        self.name_edit.setText(suggested_name)
        self.viewLayout.addWidget(self.name_edit)
        self.yesButton.setText("Import")
        self.cancelButton.setText("Cancel")

    def validate(self) -> bool:  # noqa: N802 - qfw API
        try:
            validate_name(self.name_edit.text())
            return True
        except Exception:
            return False


class NewServerDialog(MessageBoxBase):
    """Ask for a server name; the folder is created under SERVERS_DIR."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        from .constants import SERVERS_DIR
        from .servers import validate_name as validate_server_name

        self._validate = validate_server_name
        self.viewLayout.addWidget(SubtitleLabel("New server"))
        hint = BodyLabel(
            "A folder will be created under:\n"
            f"{SERVERS_DIR}\\<name>\n"
            "Then import your own server jar into it."
        )
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        self.viewLayout.addWidget(SubtitleLabel("Server name"))
        self.name_edit = LineEdit()
        self.name_edit.setPlaceholderText("My Server")
        self.name_edit.setMaxLength(64)
        self.viewLayout.addWidget(self.name_edit)
        self.error_label = BodyLabel("")
        self.error_label.setVisible(False)
        self.viewLayout.addWidget(self.error_label)
        self.yesButton.setText("Create")
        self.cancelButton.setText("Cancel")

    def validate(self) -> bool:  # noqa: N802 - qfw API
        try:
            self._validate(self.name_edit.text())
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)
            return False
        self.error_label.setVisible(False)
        return True


