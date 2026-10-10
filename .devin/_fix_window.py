import pathlib

p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launcher/window.py')
s = p.read_text(encoding='utf-8')

old = '''    def _on_delete_instance(self, name: str) -> None:
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

        QTimer.singleShot(0, _perform)'''

new = '''    def _on_delete_instance(self, name: str) -> None:
        from PyQt6.QtCore import QTimer

        if not name or self._is_busy():
            return

        def _perform() -> None:
            # Hide the instances page so the dialog's Fluent fade effect can't
            # grab a pixmap of it mid-repaint (which causes "QPainter not
            # active").
            self.instances_page.setVisible(False)
            try:
                confirmed = MessageBox(
                    "Delete instance",
                    f"Delete instance '{name}' and all its files?\\nThis cannot be undone.",
                    self.window(),
                ).exec()
                if not confirmed:
                    return
                self.registry.delete(name)
                self.instances_page.refresh(self.registry.list())
                self.log(f"Instance '{name}' deleted.")
            finally:
                # Always re-show the page, even if refresh() raised.
                self.instances_page.setVisible(True)

        QTimer.singleShot(0, _perform)'''

if old in s:
    s = s.replace(old, new)
    p.write_text(s, encoding='utf-8')
    print("FIXED: window._on_delete_instance")
else:
    print("SKIP: window")
