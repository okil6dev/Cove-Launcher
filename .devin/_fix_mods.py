import pathlib

p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launcher/pages/mods_page.py')
s = p.read_text(encoding='utf-8')

old = '''    def _on_review_confirm(self) -> None:
        from PyQt6.QtCore import QTimer
        from qfluentwidgets import MessageBox

        rows = self._checked_rows()
        if not rows:
            self._toast("warning", "No mods chosen",
                        "Tick checkboxes or press 'Select mod for download'.",
                        5000)
            return
        inst = self._instance()
        if inst is None:
            self._toast("warning", "No instance", "Select an instance first.")
            return
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

        QTimer.singleShot(0, _perform)'''

new = '''    def _on_review_confirm(self) -> None:
        from PyQt6.QtCore import QTimer
        from qfluentwidgets import MessageBox

        rows = self._checked_rows()
        if not rows:
            self._toast("warning", "No mods chosen",
                        "Tick checkboxes or press 'Select mod for download'.",
                        5000)
            return
        inst = self._instance()
        if inst is None:
            self._toast("warning", "No instance", "Select an instance first.")
            return
        names = "\\n".join(f"• {r.get('title', '?')}" for r in rows)

        def _perform() -> None:
            # Hide the card area so the dialog's Fluent fade effect can't grab
            # a pixmap of it mid-repaint (which causes "QPainter not active").
            if self._cards_scroll_area is not None:
                self._cards_scroll_area.setVisible(False)
            try:
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
            finally:
                if self._cards_scroll_area is not None:
                    self._cards_scroll_area.setVisible(True)

        QTimer.singleShot(0, _perform)'''

if old in s:
    s = s.replace(old, new)
    p.write_text(s, encoding='utf-8')
    print("FIXED: mods_page._on_review_confirm")
else:
    print("SKIP: mods_page")
