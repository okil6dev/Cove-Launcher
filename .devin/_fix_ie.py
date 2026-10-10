import pathlib

p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launcher/instance_editor.py')
s = p.read_text(encoding='utf-8')

old = '''    def _delete_selected_mod(self) -> None:
        from PyQt6.QtCore import QTimer

        item = self.mod_list.currentItem()
        if item is None or not item.data(Qt.ItemDataRole.UserRole):
            return
        name = item.data(Qt.ItemDataRole.UserRole)

        def _perform() -> None:
            # Parent to self.window() (top-level) so the dialog is a sibling of
            # the editor, not a child of the mod list page.  This keeps the
            # Fluent fade-in (QWidgetEffectSourcePrivate) from grabbing a pixmap
            # of the list view mid-repaint -> "QPainter not active" spam.
            box = MessageBox(
                "Delete mod",
                f"Delete '{name}' from this instance?",
                self.window(),
            )
            if not box.exec():
                return
            try:
                moddb.remove_mod(self._instance.mods_dir, name)
            except Exception as exc:  # noqa: BLE001
                self._toast("error", "Delete failed", str(exc))
                return
            self._toast("success", "Mod deleted", name)
            # Defer the list rebuild 60 ms so the dialog fade-out doesn't race
            # with the mod_list repaint.
            QTimer.singleShot(60, self.refresh_mods)

        # Defer the whole action to the next idle tick so the click/mouse-release
        # cycle completes before the modal opens.
        QTimer.singleShot(0, _perform)'''

new = '''    def _delete_selected_mod(self) -> None:
        from PyQt6.QtCore import QTimer

        item = self.mod_list.currentItem()
        if item is None or not item.data(Qt.ItemDataRole.UserRole):
            return
        name = item.data(Qt.ItemDataRole.UserRole)

        def _perform() -> None:
            # Hide the mod list so the dialog's Fluent fade effect can't grab
            # a pixmap of it mid-repaint (which causes "QPainter not active").
            self.mod_list.setVisible(False)
            try:
                # Parent to self.window() (top-level) so the dialog is a
                # sibling of the editor, not a child of the mod list page.
                box = MessageBox(
                    "Delete mod",
                    f"Delete '{name}' from this instance?",
                    self.window(),
                )
                if not box.exec():
                    return
                try:
                    moddb.remove_mod(self._instance.mods_dir, name)
                except Exception as exc:  # noqa: BLE001
                    self._toast("error", "Delete failed", str(exc))
                    return
                self._toast("success", "Mod deleted", name)
                self.refresh_mods()
            finally:
                # Always re-show the list, even if refresh_mods() raised.
                self.mod_list.setVisible(True)

        # Defer the whole action to the next idle tick so the click/mouse-release
        # cycle completes before the modal opens.
        QTimer.singleShot(0, _perform)'''

if old in s:
    s = s.replace(old, new)
    p.write_text(s, encoding='utf-8')
    print("FIXED: instance_editor._delete_selected_mod")
else:
    print("SKIP: instance_editor")
