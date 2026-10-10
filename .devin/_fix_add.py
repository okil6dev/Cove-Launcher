import pathlib

p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launcher/pages/accounts_page.py')
s = p.read_text(encoding='utf-8')

# Fix _on_add_offline - hide list during dialog, refresh after
old = '''    def _on_add_offline(self) -> None:
        from PyQt6.QtCore import QTimer

        def _perform() -> None:
            # Parent to self.window() (top-level sibling) so the Fluent fade-in
            # doesn't grab a pixmap of the list view mid-repaint.
            dialog = _OfflineDialog(self.window())
            if dialog.exec():
                account = self._store.add_offline(dialog.name_edit.text())
                self._log(f"Added offline account '{account.name}'.")
                # Defer the refresh 60 ms so the dialog fade-out doesn't race
                # with the list rebuild.
                QTimer.singleShot(60, self.refresh)

        # Defer to idle so the click cycle completes before the modal opens.
        QTimer.singleShot(0, _perform)'''

new = '''    def _on_add_offline(self) -> None:
        from PyQt6.QtCore import QTimer

        def _perform() -> None:
            # Hide the list widget so the dialog's Fluent fade effect can't
            # grab a pixmap of it mid-repaint (which causes "QPainter not
            # active" / "A paint device can only be painted by one painter").
            self.list_widget.setVisible(False)
            try:
                dialog = _OfflineDialog(self.window())
                if dialog.exec():
                    account = self._store.add_offline(dialog.name_edit.text())
                    self._log(f"Added offline account '{account.name}'.")
                    self.refresh()
            finally:
                # Always re-show the list, even if refresh() raised.
                self.list_widget.setVisible(True)

        # Defer to idle so the click cycle completes before the modal opens.
        QTimer.singleShot(0, _perform)'''

if old in s:
    s = s.replace(old, new)
    print("FIXED: _on_add_offline")
else:
    print("SKIP: _on_add_offline")

p.write_text(s, encoding='utf-8')
print("Done.")
