import pathlib, re


def fix_instance_editor():
    """Fix the delete-mod painter race in instance_editor.py."""
    p = pathlib.Path('launcher/instance_editor.py')
    s = p.read_text(encoding='utf-8')

    # Find the _delete_selected_mod method and rewrite it
    pattern = re.compile(
        r'    def _delete_selected_mod\(self\) -> None:.*?(?=\n    def _open_mods_folder)',
        re.DOTALL
    )
    new_method = '''    def _delete_selected_mod(self) -> None:
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
        QTimer.singleShot(0, _perform)

'''
    m = pattern.search(s)
    if not m:
        print("instance_editor: NO MATCH")
        return
    s = s[:m.start()] + new_method + s[m.end():]
    p.write_text(s, encoding='utf-8')
    print("instance_editor: FIXED")


def fix_mods_page():
    """Fix the install-mods painter race in mods_page.py."""
    p = pathlib.Path('launcher/pages/mods_page.py')
    s = p.read_text(encoding='utf-8')

    # Find the _on_review_and_confirm method (the one with "Review and confirm")
    pattern = re.compile(
        r'    def _on_review_and_confirm\(self\) -> None:.*?(?=\n    def _run_next_install)',
        re.DOTALL
    )
    new_method = '''    def _on_review_and_confirm(self) -> None:
        from PyQt6.QtCore import QTimer

        if not self._checked_mods:
            self._toast("warning", "No mods selected", "Tick at least one mod first.")
            return
        inst = self._instance()
        if inst is None:
            self._toast("warning", "No instance", "Select an instance first.")
            return
        rows = [c.mod_info for c in self._cards.values()
                if c.mod_info and c.checkbox.isChecked()]
        names = "\\n".join(f"• {r.get('title', '?')}" for r in rows)

        def _perform() -> None:
            box = MessageBox(
                "Review and confirm",
                f"Download {len(rows)} mod(s) into '{inst.name}'?\\n\\n{names}",
                self.window(),
            )
            if not box.exec():
                return
            self._install_queue = list(rows)
            self._install_total = len(rows)
            self._run_next_install()

        QTimer.singleShot(0, _perform)

'''
    m = pattern.search(s)
    if not m:
        print("mods_page: NO MATCH")
        return
    s = s[:m.start()] + new_method + s[m.end():]
    p.write_text(s, encoding='utf-8')
    print("mods_page: FIXED")


def fix_window_delete():
    """Fix the delete-instance painter race in window.py."""
    p = pathlib.Path('launcher/window.py')
    s = p.read_text(encoding='utf-8')

    # Find the _on_delete_instance method
    pattern = re.compile(
        r'    def _on_delete_instance\(self, name: str\) -> None:.*?(?=\n    def _on_open_folder)',
        re.DOTALL
    )
    new_method = '''    def _on_delete_instance(self, name: str) -> None:
        from PyQt6.QtCore import QTimer

        if not name or self._is_busy():
            return

        def _perform() -> None:
            confirmed = MessageBox(
                "Delete instance",
                f"Delete instance '{name}' and all its files?\\nThis cannot be undone.",
                self,
            ).exec()
            if not confirmed:
                return
            self.registry.delete(name)
            # Defer the refresh 60 ms so the dialog fade-out doesn't race with
            # the instances-page grid repaint.
            def _refresh():
                self.instances_page.refresh(self.registry.list())
                self.log(f"Instance '{name}' deleted.")
            QTimer.singleShot(60, _refresh)

        QTimer.singleShot(0, _perform)

'''
    m = pattern.search(s)
    if not m:
        print("window: NO MATCH")
        return
    s = s[:m.start()] + new_method + s[m.end():]
    p.write_text(s, encoding='utf-8')
    print("window: FIXED")


fix_instance_editor()
fix_mods_page()
fix_window_delete()
