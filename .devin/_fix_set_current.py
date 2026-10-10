import pathlib

p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launcher/pages/accounts_page.py')
s = p.read_text(encoding='utf-8')

# Fix _on_set_current - use setUpdatesEnabled to prevent repaint during refresh
old = '''            self._store.set_current(uuid_str)
            self._log("Active account changed.")
            # Defer the refresh so the button's click cycle completes first.
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(60, self.refresh)'''

new = '''            self._store.set_current(uuid_str)
            self._log("Active account changed.")
            # Block list updates during refresh so the button's release
            # repaint can't race with the list rebuild.
            self.list_widget.setUpdatesEnabled(False)
            try:
                self.refresh()
            finally:
                self.list_widget.setUpdatesEnabled(True)'''

if old in s:
    s = s.replace(old, new)
    print("FIXED: _on_set_current")
else:
    print("SKIP: _on_set_current")

p.write_text(s, encoding='utf-8')
print("Done.")
