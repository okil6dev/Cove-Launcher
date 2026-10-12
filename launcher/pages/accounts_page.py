"""Accounts page: Microsoft login + offline accounts management."""

from __future__ import annotations

import webbrowser
from datetime import datetime

from PyQt6.QtGui import QClipboard

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    ToolButton,
)

from ..accounts import Account, AccountStore, AuthError
from ..constants import ELY_CLIENT_ID
from ..cove_dialog import CoveConfirm, CoveFormDialog
from ..page_scroll import scrollable_page_root
from ..workers_net import ElyLoginThread, MsLoginThread


class _OfflineDialog(CoveFormDialog):
    """Small modal to enter an offline player name."""

    def __init__(self, parent) -> None:
        super().__init__("Add offline account", parent, width=440,
                         ok_text="Add", cancel_text="Cancel")
        self.name_edit = LineEdit()
        self.name_edit.setPlaceholderText("Player name")
        self.name_edit.setMaxLength(16)
        self.viewLayout.addWidget(self.name_edit)

    def validate(self) -> bool:  # noqa: N802 - qfw API
        if not self.name_edit.text().strip():
            self.set_error("Enter a player name.")
            return False
        self.set_error("")
        return True


class _ElyCodeDialog(CoveFormDialog):
    """Modal showing the one-time Ely.by device code + verification page."""

    def __init__(self, parent, verification_uri: str, user_code: str) -> None:
        super().__init__("Ely.by login", parent, width=480,
                         ok_text="Open in browser", cancel_text="Cancel login")
        self.viewLayout.addWidget(
            BodyLabel("Open the page below in your browser and enter the code:")
        )
        self.viewLayout.addWidget(BodyLabel(verification_uri))
        code_label = BodyLabel(user_code)
        code_label.setStyleSheet(
            "font-size: 26px; font-weight: 600; letter-spacing: 5px; "
            "padding: 4px;"
        )
        code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.viewLayout.addWidget(code_label)
        self.viewLayout.addWidget(
            BodyLabel(
                "The code is also on your clipboard. ely.by never learns your "
                "password - it only issues a limited token to the launcher."
            )
        )


class AccountsPage(QWidget):
    """Manage launcher accounts; Microsoft login happens in the browser."""

    def __init__(
        self,
        store: AccountStore,
        get_client_id,  # callable -> str (from vault/settings)
        log,
        toast,
        append_raw=None,  # callable(str) -> writes verbatim to the console
        get_ely_client_id=None,  # callable -> str; built-in default when omitted
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("accountsPage")
        self._store = store
        self._get_client_id = get_client_id
        self._get_ely_client_id = get_ely_client_id or (lambda: ELY_CLIENT_ID)
        self._log = log
        self._toast = toast
        self._append_raw = append_raw or (lambda text: log(text))
        self._login_thread: MsLoginThread | None = None
        self._ely_thread: ElyLoginThread | None = None
        self._ely_dialog: _ElyCodeDialog | None = None

        # Scrollable content area: the five-button action row can sum past the
        # available content width, which would otherwise clamp the boot window
        # wider than its intended size (see LauncherWindow.__init__).
        root = scrollable_page_root(self, margins=(24, 24, 24, 16), spacing=12)

        root.addWidget(SubtitleLabel("Accounts"))
        root.addWidget(
            BodyLabel(
                "Microsoft accounts are verified via browser login (PKCE).\n"
                "Only encrypted refresh tokens are stored on disk."
            )
        )

        buttons = QHBoxLayout()
        self.login_button = PrimaryPushButton(FluentIcon.GLOBE, "Login with Microsoft")
        self.ely_button = PushButton(
            getattr(FluentIcon, "CLOUD", FluentIcon.GLOBE),
            "Login with Ely.by",
        )
        self.offline_button = PushButton(FluentIcon.PERSON if hasattr(FluentIcon, "PERSON") else FluentIcon.EDIT, "Add offline account")
        self.current_button = ToolButton(FluentIcon.ACCEPT if hasattr(FluentIcon, "ACCEPT") else FluentIcon.SAVE)
        self.current_button.setToolTip("Set selected as active account")
        self.remove_button = ToolButton(FluentIcon.DELETE)
        self.remove_button.setToolTip("Remove selected account")
        for widget in (
            self.login_button,
            self.ely_button,
            self.offline_button,
            self.current_button,
            self.remove_button,
        ):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self.status_label = BodyLabel("")
        root.addWidget(self.status_label)

        self.list_widget = LineEditList = None  # placeholder, replaced below
        from qfluentwidgets import ListWidget

        self.list_widget = ListWidget()
        root.addWidget(self.list_widget, stretch=1)

        self.login_button.clicked.connect(self._on_login)
        self.ely_button.clicked.connect(self._on_ely_login)
        self.offline_button.clicked.connect(self._on_add_offline)
        self.current_button.clicked.connect(self._on_set_current)
        self.remove_button.clicked.connect(self._on_remove)

                # Override the qfluentwidgets ListWidget default stylesheet, which leaves
        # selection/alternate backgrounds transparent.  A transparent viewport
        # background collides with the view's own repaint while refresh() rebuilds
        # the list after a delete, producing QPainter "not active" warnings.  We
        # replicate the instances-page pattern: opaque widget bg + a real
        # selection colour from the current theme palette.
        from ..theme import item_sel_bg
        self.list_widget.setStyleSheet(
            "QListWidget { background: transparent; outline: none; border: none; }"
            f"QListWidget::item:selected {{ background: {item_sel_bg()}; }}"
        )

        # Render any accounts persisted from a previous session immediately;
        # without this the page looked "logged out" on every relaunch even
        # though the refresh tokens were still safely stored in the vault.
        self.refresh()

    # -- rendering ---------------------------------------------------------------
    def refresh(self) -> None:
        from PyQt6.QtWidgets import QListWidgetItem

        self.list_widget.clear()
        current = self._store.current()
        for acc in self._store.list():
            marker = "  [active]" if current and acc.uuid == current.uuid else ""
            kind = self._kind_label(acc.type)
            item = QListWidgetItem(f"{acc.name}   ({kind}){marker}")
            item.setData(Qt.ItemDataRole.UserRole, acc.uuid)
            self.list_widget.addItem(item)
        current = self._store.current()
        self.status_label.setText(
            f"Active account: {current.name} ({current.type})" if current
            else "No accounts yet."
        )

    def _selected_uuid(self) -> str | None:
        item = self.list_widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # -- actions -------------------------------------------------------------------
    def _on_set_current(self) -> None:
        uuid_str = self._selected_uuid()
        if not uuid_str:
            return
        try:
            self._store.set_current(uuid_str)
            self._log("Active account changed.")
            # Block list updates during refresh so the button's release
            # repaint can't race with the list rebuild.
            self.list_widget.setUpdatesEnabled(False)
            try:
                self.refresh()
            finally:
                self.list_widget.setUpdatesEnabled(True)
        except KeyError:
            pass

    def _on_remove_refresh_done(self) -> None:
        """Rebuild the accounts list after a remove."""
        self.refresh()
        self._log("Account removed.")

    def _on_remove(self) -> None:
        """Remove the selected account after user confirmation.

        The confirmation runs on the next idle tick so the click cycle
        finishes before the modal opens; the list rebuild happens directly
        afterwards (native Cove dialogs have no fade-out animation to wait
        for).
        """
        from PyQt6.QtCore import QTimer

        uuid_str = self._selected_uuid()
        if not uuid_str:
            return
        account = self._store.get(uuid_str)

        def _perform() -> None:
            # Parent the dialog to the top-level window, not this page, so the
            # modal mask covers the whole launcher.
            confirmed = CoveConfirm.ask(
                self.window(),
                "Remove account",
                f"Remove '{account.name}' from the launcher?" if account else "Remove?",
                ok="Remove",
                cancel="Cancel",
            )
            if confirmed:
                self._store.remove(uuid_str)
                self._on_remove_refresh_done()

        QTimer.singleShot(0, _perform)

    def _on_add_offline(self) -> None:
        from PyQt6.QtCore import QTimer

        def _perform() -> None:
            # Native Cove dialog: no Fluent fade effect, so the list can stay
            # visible behind the modal.
            dialog = _OfflineDialog(self.window())
            if dialog.exec():
                account = self._store.add_offline(dialog.name_edit.text())
                self._log(f"Added offline account '{account.name}'.")
                self.refresh()

        # Defer to idle so the click cycle completes before the modal opens.
        QTimer.singleShot(0, _perform)

    def _on_login(self) -> None:
        if self._login_thread is not None and self._login_thread.isRunning():
            return
        from ..threading_utils import retire
        retire(self._login_thread)
        client_id = (self._get_client_id() or "").strip()
        self.login_button.setEnabled(False)
        self.status_label.setText("Starting Microsoft login...")
        self._log("Starting Microsoft login flow.")
        self._login_thread = MsLoginThread(client_id, parent=self)
        self._login_thread.status.connect(self._on_login_status)
        self._login_thread.succeeded.connect(self._on_login_ok)
        self._login_thread.failed.connect(self._on_login_failed)
        self._login_thread.start()

    def _on_login_status(self, text: str) -> None:
        if text.startswith("LOGIN_URL:"):
            url = text[len("LOGIN_URL:"):]
            # Put the URL straight onto the clipboard: manual selection from
            # a wrapped console is how AADSTS900144 (truncated link) happens.
            QApplication.clipboard().setText(
                url, QClipboard.Mode.Clipboard
            )
            self._log("Manual login option: the login URL was COPIED to your "
                      "clipboard. Paste it into an INCOGNITO window "
                      "(Ctrl+V) to force a fresh account choice.")
            self._append_raw(url)
            webbrowser.open(url)
            self.status_label.setText(
                "Waiting for browser login... (URL also in clipboard)"
            )
        else:
            self.status_label.setText(text)

    def _on_login_ok(self, profile: dict) -> None:
        from ..threading_utils import retire
        retire(self._login_thread)
        self._login_thread = None
        self.login_button.setEnabled(True)
        try:
            account = self._store.add_microsoft(profile)
        except AuthError as exc:
            self._log(str(exc), "ERROR")
            self._toast("error", "Login failed", str(exc), 8000)
            return
        self._log(f"Logged in as {account.name} via Microsoft.")
        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)
        # Block list updates during refresh so the toast fade-out can't race
        # with the list rebuild.
        self.list_widget.setUpdatesEnabled(False)
        try:
            self.refresh()
        finally:
            self.list_widget.setUpdatesEnabled(True)

    def _on_login_failed(self, message: str) -> None:
        from ..threading_utils import retire
        retire(self._login_thread)
        self._login_thread = None
        self.login_button.setEnabled(True)
        self.status_label.setText("Login failed.")
        self._log(message, "ERROR")
        self._toast("error", "Microsoft login failed", message[:200], 10000)

    # -- Ely.by (OAuth2 device-code flow) ---------------------------------------
    def _on_ely_login(self) -> None:
        if self._ely_thread is not None and self._ely_thread.isRunning():
            return
        from ..threading_utils import retire
        retire(self._ely_thread)
        client_id = (self._get_ely_client_id() or "").strip()
        self.ely_button.setEnabled(False)
        self.status_label.setText("Starting Ely.by login...")
        self._log("Starting Ely.by login flow.")
        self._ely_thread = ElyLoginThread(client_id, parent=self)
        self._ely_thread.status.connect(self._on_ely_status)
        self._ely_thread.device_code.connect(self._on_ely_device_code)
        self._ely_thread.succeeded.connect(self._on_ely_ok)
        self._ely_thread.failed.connect(self._on_ely_failed)
        self._ely_thread.start()

    def _on_ely_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _on_ely_device_code(self, verification_uri: str, user_code: str) -> None:
        QApplication.clipboard().setText(user_code, QClipboard.Mode.Clipboard)
        self._log(
            f"Ely.by device code: {user_code} (copied to clipboard). "
            f"Open {verification_uri} to approve.",
            "INFO",
        )
        webbrowser.open(verification_uri)
        dialog = _ElyCodeDialog(self.window(), verification_uri, user_code)
        self._ely_dialog = dialog
        accepted = dialog.exec()
        self._ely_dialog = None
        if not accepted and self._ely_thread is not None:
            self._ely_thread.cancel()

    def _on_ely_ok(self, profile: dict) -> None:
        self.ely_button.setEnabled(True)
        if self._ely_dialog is not None:
            self._ely_dialog.accept()
        try:
            account = self._store.add_elyby(profile)
        except AuthError as exc:
            self._log(str(exc), "ERROR")
            self._toast("error", "Ely.by login failed", str(exc), 8000)
            return
        self._log(f"Logged in as {account.name} via Ely.by.")
        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)
        # Block list updates during refresh so the toast fade-out can't race
        # with the list rebuild.
        self.list_widget.setUpdatesEnabled(False)
        try:
            self.refresh()
        finally:
            self.list_widget.setUpdatesEnabled(True)

    def _on_ely_failed(self, message: str) -> None:
        self.ely_button.setEnabled(True)
        if self._ely_dialog is not None:
            self._ely_dialog.accept()
        self.status_label.setText("Ely.by login failed.")
        self._log(message, "ERROR")
        self._toast("error", "Ely.by login failed", message[:200], 10000)

    def set_busy(self, busy: bool) -> None:
        pass  # login button state is managed directly

    def describe_current(self) -> str:
        account = self._store.current()
        if account is None:
            return "No account selected"
        return f"{account.name} ({self._kind_label(account.type)})"

    @staticmethod
    def _kind_label(account_type: str) -> str:
        return {"msa": "Microsoft", "ely": "Ely.by"}.get(account_type, "Offline")

    @staticmethod
    def _now_iso() -> str:  # pragma: no cover - helper kept for future use
        return datetime.now().isoformat()

    def refresh_theme(self) -> None:
        """Re-apply theme-dependent stylesheet on live theme change."""
        from ..theme import item_sel_bg
        self.list_widget.setStyleSheet(
            "QListWidget { background: transparent; outline: none; border: none; }"
            f"QListWidget::item:selected {{ background: {item_sel_bg()}; }}"
        )


# __CHUNK2__
